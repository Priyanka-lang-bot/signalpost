"""Replay of genuine, previously captured live HTTP responses.

Use case: the machine running Signalpost cannot reach data.brreg.no directly, but
responses were captured live elsewhere (e.g. a browser on another machine). The
capture file is verified by SHA-256 over a canonical JSON encoding, and each
response is served ONLY for the exact URL it was captured from, with its original
retrieval timestamp. Any URL that was not captured returns HTTP 503, so the
pipeline reports `source_unavailable` instead of ever inventing data.

Capture file format:
    {"captured_with": "...",
     "responses": [{"url": ..., "status": 200, "retrieved_at": ISO-8601,
                    "last_modified": str|null, "body": <parsed JSON>}]}
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical(obj: Any) -> str:
    """Same encoding the browser-side capture script hashes (sorted keys, no spaces)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_of_capture(obj: Any) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


class CaptureIntegrityError(ValueError):
    pass


class _Resp:
    def __init__(self, status: int, body: Any, retrieved_at: str | None, last_modified: str | None):
        self.status_code = status
        self.text = "" if body is None else json.dumps(body, ensure_ascii=False)
        self.headers = {"Last-Modified": last_modified} if last_modified else {}
        self.retrieved_at = retrieved_at


class ReplaySession:
    """Drop-in for requests.Session that serves only captured responses."""

    def __init__(self, capture: dict[str, Any]) -> None:
        self.capture = capture
        self.routes: dict[str, dict[str, Any]] = {}
        for r in capture.get("responses", []):
            if not str(r.get("url", "")).startswith("https://data.brreg.no/"):
                raise CaptureIntegrityError(f"capture contains a non-Brreg URL: {r.get('url')!r}")
            self.routes[r["url"]] = r
        self.calls: list[str] = []
        self.misses: list[str] = []

    @classmethod
    def from_file(cls, path: Path | str, expected_sha256: str | None = None) -> "ReplaySession":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        digest = sha256_of_capture(data)
        if expected_sha256 and digest != expected_sha256.lower():
            raise CaptureIntegrityError(f"capture SHA-256 {digest} != expected {expected_sha256}")
        session = cls(data)
        session.sha256 = digest  # type: ignore[attr-defined]
        return session

    def get(self, url: str, headers: Any = None, timeout: Any = None) -> _Resp:
        self.calls.append(url)
        r = self.routes.get(url)
        if r is None:
            self.misses.append(url)
            return _Resp(503, None, None, None)  # not captured: unavailable, never fabricated
        return _Resp(int(r["status"]), r.get("body"), r.get("retrieved_at"), r.get("last_modified"))
