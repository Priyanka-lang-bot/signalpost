"""Deterministic identity gate.

The organisation number is the only identity key. A record is accepted only when
the number inside the payload AND the record's canonical URL both equal the
requested number. Names are never used to match, merge or disambiguate.
"""

from __future__ import annotations

from typing import Any

from .sources.base import SourceRecord


class IdentityMismatch(Exception):
    """A source returned a record that is not provably the requested entity."""


def payload_orgnr(record: SourceRecord) -> str | None:
    p = record.payload
    if record.kind in ("enhet", "underenhet") and isinstance(p, dict):
        v = p.get("organisasjonsnummer")
        return str(v) if v is not None else None
    return None


def verify_identity(requested: str, record: SourceRecord) -> None:
    if record.kind in ("enhet", "underenhet"):
        found = payload_orgnr(record)
        if found != requested:
            raise IdentityMismatch(
                f"requested {requested} but {record.retrieved_via} returned a record for {found!r}"
            )
    suffix = f"/{requested}/roller" if record.kind == "roles" else f"/{requested}"
    if not record.canonical_url.rstrip("/").endswith(suffix):
        raise IdentityMismatch(f"canonical URL {record.canonical_url} does not identify {requested}")


def accounts_items_for(requested: str, items: Any) -> tuple[list[tuple[int, dict]], list[str]]:
    """Keep only annual-accounts items whose `virksomhet.organisasjonsnummer` matches.

    Returns (index_in_payload, item) pairs so evidence paths stay exact.
    """
    kept: list[tuple[int, dict]] = []
    rejected: list[str] = []
    if not isinstance(items, list):
        return kept, ["accounts payload is not a list"]
    for i, item in enumerate(items):
        org = ((item or {}).get("virksomhet") or {}).get("organisasjonsnummer") if isinstance(item, dict) else None
        if str(org) == requested:
            kept.append((i, item))
        else:
            rejected.append(f"accounts item {i} belongs to {org!r}, not {requested}; discarded")
    return kept, rejected
