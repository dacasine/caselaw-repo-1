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
# Quota monitor (synthetic.new /v2/quotas)
# ---------------------------------------------------------------------------

QUOTA_ENDPOINT = "/v2/quotas"


@dataclass
class QuotaSnapshot:
    remaining: float           # requests still available in the rolling 5h window
    max_: float                # capacity of that window
    limited: bool              # server-side hard block (flip true → sleep)
    next_tick_iso: str         # ISO 8601 time of the next refill tick
    fetched_at: float          # local monotonic clock

    @property
    def pct_remaining(self) -> float:
        return self.remaining / self.max_ if self.max_ else 0.0


class QuotaMonitor:
    """Periodically fetches rolling-5h quota and gates requests when low.

    Threading model:
      - one background-free implementation: every call first triggers a
        refresh if our snapshot is older than `refresh_every_s`.
      - cheap: a single /v2/quotas roundtrip (~100ms) amortised over many
        completions calls.

    Throttling behaviour, evaluated before each LLM call via `gate()`:
      - limited=true            → sleep until next_tick + 2s jitter
      - pct_remaining < critical → sleep (nextTick - now) + 5s, then re-check
      - pct_remaining < low      → sleep 30s, then re-check
      - else                     → proceed immediately
    """

    def __init__(
        self,
        *,
        base_url: str,
        refresh_every_s: float = 30.0,
        critical_pct: float = 0.05,
        low_pct: float = 0.15,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.refresh_every_s = refresh_every_s
        self.critical_pct = critical_pct
        self.low_pct = low_pct
        self._snap: QuotaSnapshot | None = None
        self._lock = threading.Lock()

    # ---- fetch ----------------------------------------------------------

    def _fetch(self) -> QuotaSnapshot | None:
        api_key = os.environ.get("SYNTHETIC_API_KEY", "").strip()
        if not api_key:
            return None
        req = urllib.request.Request(
            f"{self.base_url}{QUOTA_ENDPOINT}",
            method="GET",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return None
        r5 = data.get("rollingFiveHourLimit") or {}
        if not r5:
            return None
        try:
            return QuotaSnapshot(
                remaining=float(r5.get("remaining", 0.0)),
                max_=float(r5.get("max", 1.0)),
                limited=bool(r5.get("limited", False)),
                next_tick_iso=str(r5.get("nextTickAt", "")),
                fetched_at=time.monotonic(),
            )
        except (TypeError, ValueError):
            return None

    def refresh(self, force: bool = False) -> QuotaSnapshot | None:
        with self._lock:
            if (
                self._snap is not None
                and not force
                and (time.monotonic() - self._snap.fetched_at) < self.refresh_every_s
            ):
                return self._snap
            snap = self._fetch()
            if snap is not None:
                self._snap = snap
            return self._snap

    # ---- gate -----------------------------------------------------------

    def _seconds_until_tick(self, iso: str) -> float:
        # Parse ISO 8601 like "2026-04-15T09:49:56.000Z" defensively.
        if not iso:
            return 30.0
        try:
            # Python 3.11+ handles Z suffix; for 3.14 definitely works.
            from datetime import datetime, timezone
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            dt = datetime.fromisoformat(iso)
            now = datetime.now(timezone.utc)
            delta = (dt - now).total_seconds()
            return max(0.0, delta)
        except Exception:
            return 30.0

    def gate(self) -> None:
        """Block until it's safe to send an LLM request."""
        snap = self.refresh()
        if snap is None:
            return  # fail-open: if we can't read quota, let rate limiter handle it

        if snap.limited:
            wait = self._seconds_until_tick(snap.next_tick_iso) + 2.0
            time.sleep(min(wait, 300.0))
            self.refresh(force=True)
            return

        pct = snap.pct_remaining
        if pct < self.critical_pct:
            wait = self._seconds_until_tick(snap.next_tick_iso) + 5.0
            time.sleep(min(wait, 300.0))
            self.refresh(force=True)
        elif pct < self.low_pct:
            time.sleep(30.0)
            self.refresh(force=True)

    def describe(self) -> str:
        snap = self._snap
        if snap is None:
            return "quota: n/a"
        return (f"quota: {snap.remaining:.0f}/{snap.max_:.0f} "
                f"({snap.pct_remaining*100:.1f}%) "
                f"{'LIMITED' if snap.limited else 'ok'}")


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
        quota_aware: bool = True,
    ) -> None:
        self.base_url = (base_url or os.environ.get("SYNTHETIC_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.model = model or os.environ.get("SYNTHETIC_MODEL", DEFAULT_MODEL)
        self.fallback_model = (
            fallback_model
            or os.environ.get("SYNTHETIC_FALLBACK_MODEL", DEFAULT_FALLBACK)
        )
        self.timeout_s = request_timeout_s
        self.rate = RateLimiter(rate_limit_per_minute)
        # Strip /v1 etc from base_url for the quota endpoint.
        api_root = self.base_url.rsplit("/v", 1)[0]
        self.quota = QuotaMonitor(base_url=api_root) if quota_aware else None

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
        if self.quota is not None:
            self.quota.gate()
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
        # 429 / 5xx aware retry with exponential backoff.
        # We respect Retry-After when present, otherwise back off
        # 30s, 60s, 120s. After 4 tries we surface the error so the
        # caller can mark the decision and move on.
        backoffs = [30, 60, 120]
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    raw = resp.read()
                break
            except urllib.error.HTTPError as e:
                transient = e.code == 429 or 500 <= e.code < 600
                if not transient or attempt >= len(backoffs):
                    raise
                retry_after = e.headers.get("Retry-After") if e.headers else None
                wait = int(retry_after) if (retry_after and retry_after.isdigit()) else backoffs[attempt]
                time.sleep(wait)
                attempt += 1
                # We have to rebuild the request because HTTPError consumes it.
                req = urllib.request.Request(
                    url, data=body, method="POST",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                )
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
