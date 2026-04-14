"""Thin OpenAI-compatible client for synthetic.new.

Used by the SAC (Phase 3) and enrichment (Phase 5) pipelines. Reads
credentials from environment variables — NEVER hardcode a key here.

Required env vars (see .env.example):
    SYNTHETIC_API_KEY        e.g. "syn_..."
    SYNTHETIC_BASE_URL       defaults to https://api.synthetic.new/v1
    SYNTHETIC_MODEL          primary model id, e.g. "hf:moonshotai/Kimi-K2-Thinking"
    SYNTHETIC_FALLBACK_MODEL used on primary timeout/error

Design goals:
    - Zero third-party dependencies (stdlib only) so this can run inside
      the MCP subprocess without polluting the existing environment.
    - Explicit rate limiting with a monotonic token bucket.
    - Typed result (content + usage + latency).
    - Transparent fallback to a secondary model on primary failure.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Minimal .env loader (stdlib only)
# ---------------------------------------------------------------------------

def _load_env_file(path: Path | str = ".env") -> None:
    """Populate os.environ from a simple KEY=VALUE file. Silent no-op if absent.
    Existing env vars take precedence (so production / CI overrides work)."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env_file()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BASE_URL = "https://api.synthetic.new/v1"
DEFAULT_MODEL = "hf:moonshotai/Kimi-K2-Thinking"
DEFAULT_FALLBACK = "hf:Qwen/Qwen3-235B-A22B-Thinking-2507"


class SyntheticNotConfigured(RuntimeError):
    """Raised when SYNTHETIC_API_KEY is missing from the environment."""


def _require_api_key() -> str:
    key = os.environ.get("SYNTHETIC_API_KEY", "").strip()
    if not key or key.startswith("syn_..."):
        raise SyntheticNotConfigured(
            "SYNTHETIC_API_KEY is not set in the environment. "
            "Add it to .env (never commit the file)."
        )
    return key


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple monotonic token bucket, thread-safe.

    Enforces at most `max_per_minute` requests. `acquire()` blocks until the
    next slot is available. No exceptions — backpressure only.
    """

    def __init__(self, max_per_minute: int = 60) -> None:
        self.interval = 60.0 / max(1, max_per_minute)
        self._next_slot = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_slot = max(now, self._next_slot) + self.interval


# ---------------------------------------------------------------------------
# Response type
# ---------------------------------------------------------------------------

@dataclass
class LLMResponse:
    content: str                    # final assistant text (no reasoning trace)
    model: str                      # model id that actually served the request
    usage: dict                     # {prompt_tokens, completion_tokens, total_tokens}
    latency_s: float
    fallback_used: bool = False


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SyntheticClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        rate_limit_per_minute: int = 60,
        request_timeout_s: float = 90.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("SYNTHETIC_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.environ.get("SYNTHETIC_MODEL", DEFAULT_MODEL)
        self.fallback_model = (
            fallback_model
            or os.environ.get("SYNTHETIC_FALLBACK_MODEL", DEFAULT_FALLBACK)
        )
        self.timeout_s = request_timeout_s
        self.rate = RateLimiter(rate_limit_per_minute)

    # ---- public API ------------------------------------------------------

    def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> LLMResponse:
        """Single-turn chat completion. Returns the assistant content stripped
        of any reasoning trace that some models (Qwen3-Thinking) include in a
        separate field."""
        primary = model or self.model
        try:
            return self._call(primary, system, user, max_tokens, temperature)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            # Fallback once, on any transport or decode error.
            fallback = self.fallback_model
            if fallback == primary:
                raise
            resp = self._call(fallback, system, user, max_tokens, temperature)
            resp.fallback_used = True
            return resp

    # ---- internals -------------------------------------------------------

    def _call(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> LLMResponse:
        self.rate.acquire()
        api_key = _require_api_key()
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            raw = resp.read()
        dt = time.monotonic() - t0
        data = json.loads(raw.decode("utf-8"))

        if not data.get("choices"):
            raise ValueError(f"No choices in response: {data}")
        msg = data["choices"][0].get("message", {})
        content = (msg.get("content") or "").strip()
        # Some models put the final answer in "reasoning" when content is empty
        # and max_tokens was exhausted mid-thought; we don't return reasoning
        # as it's not a deterministic output channel.
        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            usage=usage,
            latency_s=dt,
            fallback_used=False,
        )
