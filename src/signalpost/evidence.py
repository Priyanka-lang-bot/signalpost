"""Evidence validation: re-check every fact against the payload it claims to come from.

A fact passes only if
  * its source URL identifies the same organisation number,
  * it has a retrieval timestamp, and
  * re-reading `json_path` in the fetched payload yields exactly the stated value
    (or, for derived facts, re-deriving from the payload yields the same value).
Facts that fail are marked UNVERIFIED and are never published.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fact_extraction import MISSING, derive_roles, get_path
from .schemas import Fact, FactStatus

DERIVERS = {"roles": lambda payload: [r for r in derive_roles(payload)
                                      if not r.get("fratraadt") and not r.get("avregistrert")]}


@dataclass
class EvidenceReport:
    checked: int = 0
    passed: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def completeness(self) -> float:
        return self.passed / self.checked if self.checked else 1.0


def check_fact(fact: Fact, orgnr: str, payload: Any) -> str | None:
    """Return None when the fact is supported, else a reason string."""
    if f"/{orgnr}" not in fact.source_url:
        return f"{fact.field}: source URL {fact.source_url} does not identify {orgnr}"
    if not fact.retrieved_at:
        return f"{fact.field}: missing retrieval timestamp"
    if fact.field in DERIVERS:
        if DERIVERS[fact.field](payload) != fact.value:
            return f"{fact.field}: value cannot be re-derived from the source payload"
        return None
    found = get_path(payload, fact.json_path)
    if found is MISSING:
        return f"{fact.field}: path {fact.json_path} not present in source payload"
    if found != fact.value or type(found) is not type(fact.value):
        return f"{fact.field}: stated value {fact.value!r} != source value {found!r}"
    return None


def validate(facts: list[Fact], orgnr: str, payload: Any, report: EvidenceReport) -> list[Fact]:
    """Return only supported facts; failures are recorded on the report."""
    ok: list[Fact] = []
    for f in facts:
        report.checked += 1
        reason = check_fact(f, orgnr, payload)
        if reason is None:
            report.passed += 1
            ok.append(f)
        else:
            f.status = FactStatus.UNVERIFIED
            report.failures.append(reason)
    return ok
