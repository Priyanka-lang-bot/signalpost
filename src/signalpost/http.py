"""Outbound HTTP with a hard request budget, a run deadline, retries with backoff and caching.

Every network call in Signalpost goes through `BudgetedClient`, so the request
counter is the single source of truth for the competition's 2,000-request limit.
Cache hits do not count as outbound requests.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlencode

import requests

from .config import Settings


class BudgetExceeded(RuntimeError):
    """Raised before a request would exceed the request budget or the run deadline."""


class SourceUnavailable(RuntimeError):
    """Raised when a source keeps failing after retries (network error, 5xx, 429)."""


@dataclass
class HttpResult:
    url: str  # full URL actually requested (including query string)
    status: int
    body: Any  # parsed JSON, or None
    retrieved_at: str  # ISO-8601 UTC
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False


class Cache(Protocol):
    def get(self, url: str, max_age_seconds: int) -> HttpResult | None: ...
    def put(self, result: HttpResult) -> None: ...


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class RunStats:
    outbound_requests: int = 0
    cache_hits: int = 0
    retries: int = 0
    failures: int = 0
    started_monotonic: float = field(default_factory=time.monotonic)

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started_monotonic


class BudgetedClient:
    _RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(
        self,
        settings: Settings,
        session: Any | None = None,
        cache: Cache | None = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.settings = settings
        self.session = session if session is not None else requests.Session()
        self.cache = cache
        self.stats = RunStats()
        self._sleep = sleep
        self._last_request = 0.0

    # -- budget -------------------------------------------------------------
    @property
    def remaining_requests(self) -> int:
        return self.settings.max_requests - self.stats.outbound_requests

    def _check_budget(self) -> None:
        if self.stats.outbound_requests >= self.settings.max_requests:
            raise BudgetExceeded(
                f"request budget of {self.settings.max_requests} outbound requests reached"
            )
        if self.stats.elapsed_seconds >= self.settings.max_seconds:
            raise BudgetExceeded(f"run deadline of {self.settings.max_seconds:.0f}s reached")

    # -- requests -----------------------------------------------------------
    def get_json(
        self, url: str, params: dict[str, Any] | None = None, use_cache: bool = True
    ) -> HttpResult:
        full_url = f"{url}?{urlencode(params, doseq=True)}" if params else url
        if use_cache and self.cache is not None:
            hit = self.cache.get(full_url, self.settings.cache_ttl_seconds)
            if hit is not None:
                self.stats.cache_hits += 1
                return hit

        attempt = 0
        while True:
            self._check_budget()
            wait = self.settings.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                self._sleep(wait)
            self.stats.outbound_requests += 1
            self._last_request = time.monotonic()
            error: str
            try:
                resp = self.session.get(
                    full_url,
                    headers={"Accept": "application/json", "User-Agent": self.settings.user_agent},
                    timeout=self.settings.request_timeout,
                )
            except requests.RequestException as exc:
                error = f"network error: {exc.__class__.__name__}: {exc}"
                resp = None
            if resp is not None:
                status = int(resp.status_code)
                if status not in self._RETRY_STATUS:
                    result = HttpResult(
                        url=full_url,
                        status=status,
                        body=_parse_json(resp),
                        retrieved_at=utcnow_iso(),
                        headers={k.lower(): v for k, v in dict(resp.headers or {}).items()},
                    )
                    if use_cache and self.cache is not None and status in (200, 404, 410):
                        self.cache.put(result)
                    return result
                error = f"HTTP {status}"
            attempt += 1
            if attempt > self.settings.max_retries:
                self.stats.failures += 1
                raise SourceUnavailable(f"{full_url}: {error} after {attempt} attempts")
            self.stats.retries += 1
            delay = self._retry_after(resp) or self.settings.backoff_base * (2 ** (attempt - 1))
            delay += random.uniform(0, 0.25 * self.settings.backoff_base)
            remaining = self.settings.max_seconds - self.stats.elapsed_seconds
            self._sleep(max(0.0, min(delay, 60.0, remaining)))

    @staticmethod
    def _retry_after(resp: Any) -> float | None:
        if resp is None:
            return None
        value = (resp.headers or {}).get("Retry-After") or (resp.headers or {}).get("retry-after")
        try:
            return float(value) if value is not None else None
        except ValueError:
            return None


def _parse_json(resp: Any) -> Any:
    text = getattr(resp, "text", None)
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None
