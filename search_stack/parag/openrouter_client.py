"""Thin OpenRouter client — same shape as SyntheticClient.

Reads OPENROUTER_API_KEY from env (via .env loader). OpenRouter uses
an OpenAI-compatible /chat/completions endpoint plus two courtesy
headers that improve rate-limit tiers for identifiable apps.
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

from search_stack.parag.llm_client import (
    DEFAULT_BASE_URL as _SYN_URL,
    LLMResponse,
    RateLimiter,
    _load_env_file,
)

_load_env_file()


class OpenRouterNotConfigured(RuntimeError):
    """Raised when OPENROUTER_API_KEY is missing from the environment."""


DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "google/gemini-2.0-flash-001"
DEFAULT_FALLBACK_MODEL = "google/gemini-2.5-flash"


def _require_api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key or key.startswith("sk-or-v1-..."):
        raise OpenRouterNotConfigured(
            "OPENROUTER_API_KEY is not set in the environment. "
            "Add it to .env (never commit)."
        )
    return key


class OpenRouterClient:
    """Mirrors SyntheticClient's public API: .chat(system, user, ...) → LLMResponse.

    Concurrency: OpenRouter tolerates much higher parallelism than
    synthetic.new (no explicit 2-concurrent cap). Rate limiting is
    per-key across all models. We keep the token-bucket RateLimiter
    at a comfortable margin of whatever the user's plan allows.

    Retries on transient HTTP errors: 429 (rate limit) and 5xx. Honours
    the `Retry-After` header when present, otherwise exponential
    backoff 10s → 30s → 90s (OpenRouter resets quickly).
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        fallback_model: str | None = None,
        rate_limit_per_minute: int = 120,
        request_timeout_s: float = 180.0,
        app_title: str = "PA-RAG Phase 5",
        app_referer: str = "https://github.com/dacasine/caselaw-repo-1",
    ) -> None:
        self.base_url = (base_url or os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)
        self.fallback_model = (
            fallback_model
            or os.environ.get("OPENROUTER_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL)
        )
        self.timeout_s = request_timeout_s
        self.rate = RateLimiter(rate_limit_per_minute)
        self.quota = None  # OpenRouter has no quota monitor (unlike SyntheticClient)
        self._app_title = app_title
        self._app_referer = app_referer

    # ---- public API ----------------------------------------------------

    def chat(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 3500,
        temperature: float = 0.1,
        model: str | None = None,
    ) -> LLMResponse:
        primary = model or self.model
        try:
            return self._call(primary, system, user, max_tokens, temperature)
        except (urllib.error.URLError, TimeoutError, ValueError):
            if self.fallback_model == primary:
                raise
            resp = self._call(self.fallback_model, system, user, max_tokens, temperature)
            resp.fallback_used = True
            return resp

    # ---- internals -----------------------------------------------------

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

        def _build_req() -> urllib.request.Request:
            return urllib.request.Request(
                url, data=body, method="POST",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": self._app_referer,
                    "X-Title": self._app_title,
                },
            )

        backoffs = [10, 30, 90]
        attempt = 0
        t0 = time.monotonic()
        while True:
            try:
                with urllib.request.urlopen(_build_req(), timeout=self.timeout_s) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                transient = e.code == 429 or 500 <= e.code < 600
                if not transient or attempt >= len(backoffs):
                    raise
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = (
                    int(retry_after) if (retry_after and retry_after.isdigit())
                    else backoffs[attempt]
                )
                time.sleep(wait)
                attempt += 1
        dt = time.monotonic() - t0
        data = json.loads(raw.decode("utf-8"))

        if not data.get("choices"):
            raise ValueError(f"No choices in response: {data}")
        msg = data["choices"][0].get("message", {})
        content = (msg.get("content") or "").strip()
        usage = data.get("usage", {})
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            usage=usage,
            latency_s=dt,
            fallback_used=False,
        )
