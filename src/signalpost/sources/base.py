"""Source-adapter contract. New sources implement the same small surface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class SourceRecord:
    """One payload from a source, describing exactly one legal entity (or its roles/accounts)."""

    source: str  # e.g. "brreg:enhetsregisteret"
    kind: Literal["enhet", "underenhet", "roles", "accounts"]
    requested_orgnr: str
    payload: Any
    canonical_url: str  # URL of the record itself
    retrieved_via: str  # URL that was actually fetched
    retrieved_at: str
    source_modified_at: str | None = None


@dataclass(frozen=True)
class LookupResult:
    status: Literal["found", "not_found", "removed"]
    record: SourceRecord | None = None
    detail: str = ""


class RegistrySource(Protocol):
    name: str

    def lookup(self, orgnr: str) -> LookupResult: ...
