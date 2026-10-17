"""Centralised configuration. Every value can be overridden with an environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader (KEY=VALUE lines) so no extra dependency is needed."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(default_factory=lambda: Path(_env("SIGNALPOST_DB", "data/signalpost.sqlite3")))
    enhetsregisteret_base: str = field(
        default_factory=lambda: _env("SIGNALPOST_BRREG_BASE", "https://data.brreg.no/enhetsregisteret/api")
    )
    regnskap_base: str = field(
        default_factory=lambda: _env(
            "SIGNALPOST_REGNSKAP_BASE", "https://data.brreg.no/regnskapsregisteret/regnskap"
        )
    )
    # Competition limits per daily run: 45 min, 2,000 outbound requests, $10 API cost.
    max_requests: int = field(default_factory=lambda: int(_env("SIGNALPOST_MAX_REQUESTS", "2000")))
    max_seconds: float = field(default_factory=lambda: float(_env("SIGNALPOST_MAX_SECONDS", "2700")))
    request_timeout: float = field(default_factory=lambda: float(_env("SIGNALPOST_TIMEOUT", "20")))
    max_retries: int = field(default_factory=lambda: int(_env("SIGNALPOST_MAX_RETRIES", "3")))
    backoff_base: float = field(default_factory=lambda: float(_env("SIGNALPOST_BACKOFF", "1.0")))
    min_interval: float = field(default_factory=lambda: float(_env("SIGNALPOST_MIN_INTERVAL", "0.1")))
    cache_ttl_seconds: int = field(default_factory=lambda: int(_env("SIGNALPOST_CACHE_TTL", "3600")))
    include_financials: bool = field(
        default_factory=lambda: _env("SIGNALPOST_FINANCIALS", "1") not in ("0", "false", "no")
    )
    include_roles: bool = field(
        default_factory=lambda: _env("SIGNALPOST_ROLES", "1") not in ("0", "false", "no")
    )
    user_agent: str = field(
        default_factory=lambda: _env(
            "SIGNALPOST_USER_AGENT", "Signalpost/0.1 (company-intelligence hackathon agent)"
        )
    )
    # No external paid API is used. Declared cost per request is therefore 0.
    declared_cost_per_request_usd: float = 0.0


def load_settings() -> Settings:
    _load_dotenv()
    return Settings()
