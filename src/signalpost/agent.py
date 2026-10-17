"""The controlled Signalpost pipeline.

input -> validate identifier -> retrieve authoritative record -> verify identity
      -> extract facts -> validate evidence -> compare with store -> save updates
      -> explain -> JSON
"""

from __future__ import annotations

from .config import Settings
from .entity_resolution import IdentityMismatch, verify_identity
from .evidence import EvidenceReport, validate
from .explain import explain
from .fact_extraction import extract_entity, extract_financials, extract_roles
from .http import BudgetExceeded, BudgetedClient, SourceUnavailable, utcnow_iso
from .orgnr import InvalidOrgNumber, normalize
from .schemas import AgentResult, CompanyProfile
from .sources.base import SourceRecord
from .sources.brreg import BrregSource
from .storage import Store


class Agent:
    CIRCUIT_BREAKER = 5
    def __init__(self, settings: Settings, store: Store, client: BudgetedClient) -> None:
        self.settings = settings
        self.store = store
        self.client = client
        self.source = BrregSource(client)
        self.evidence = EvidenceReport()
        # Stop calling the (temporary, R&D) accounts API after this many consecutive failures,
        # so an outage there cannot burn the run's request budget.
        self._accounts_failures = 0

    def _run_stats(self) -> dict:
        s = self.client.stats
        return {
            "outbound_requests": s.outbound_requests,
            "cache_hits": s.cache_hits,
            "retries": s.retries,
            "elapsed_seconds": round(s.elapsed_seconds, 2),
            "declared_external_api_cost_usd": round(s.outbound_requests * self.settings.declared_cost_per_request_usd, 4),
        }

    def lookup(self, raw: str, enrich: bool = True, roles: bool | None = None) -> AgentResult:
        now = utcnow_iso()
        try:
            orgnr = normalize(raw)
        except InvalidOrgNumber as exc:
            return AgentResult(input=raw, outcome="invalid_input", warnings=[str(exc)], checked_at=now)
        try:
            found = self.source.lookup(orgnr)
        except BudgetExceeded as exc:
            return AgentResult(input=raw, orgnr=orgnr, outcome="budget_exceeded", warnings=[str(exc)], checked_at=now, run=self._run_stats())
        except SourceUnavailable as exc:
            return AgentResult(input=raw, orgnr=orgnr, outcome="source_unavailable", warnings=[str(exc)], checked_at=now, run=self._run_stats())
        if found.status == "removed":
            changes = self.store.mark_removed(orgnr, found.detail)
            return AgentResult(input=raw, orgnr=orgnr, outcome="removed_from_registry", changes=changes,
                               explanation=f"Org. no. {orgnr} has been removed from the register (HTTP 410). "
                                           "Signalpost publishes no facts for it and has purged any stored copy.",
                               warnings=[found.detail], checked_at=now, run=self._run_stats())
        if found.status == "not_found" or found.record is None:
            return AgentResult(input=raw, orgnr=orgnr, outcome="not_found",
                               explanation=f"No entity or sub-entity with org. no. {orgnr} exists in the register.",
                               warnings=[found.detail], checked_at=now, run=self._run_stats())
        return self.process(raw, orgnr, found.record, enrich=enrich, roles=roles)

    def process(self, raw: str, orgnr: str, rec: SourceRecord, enrich: bool = True,
                roles: bool | None = None) -> AgentResult:
        """Run identity, extraction, evidence, storage and explanation on an already-fetched record."""
        now = utcnow_iso()
        warnings: list[str] = []
        try:
            verify_identity(orgnr, rec)
        except IdentityMismatch as exc:
            return AgentResult(input=raw, orgnr=orgnr, outcome="identity_mismatch", warnings=[str(exc)],
                               explanation="The source returned a different entity; nothing was published.",
                               checked_at=now, run=self._run_stats())
        profile = extract_entity(rec)
        report = EvidenceReport()
        profile.facts = validate(profile.facts, orgnr, rec.payload, report)
        groups = {"entity"}

        if enrich and rec.kind == "enhet" and profile.registry_state == "registered":
            if self.settings.include_roles if roles is None else roles:
                self._add_roles(orgnr, profile, report, warnings, groups)
            if self.settings.include_financials:
                self._add_financials(orgnr, profile, report, warnings, groups)

        warnings.extend(report.failures)
        self.evidence.checked += report.checked
        self.evidence.passed += report.passed
        self.evidence.failures.extend(report.failures)

        first = not self.store.has_company(orgnr)
        changes = self.store.save_profile(profile, groups)
        result = AgentResult(input=raw, orgnr=orgnr, outcome="ok", profile=profile, changes=changes,
                             warnings=warnings, checked_at=now, run=self._run_stats())
        result.explanation = explain(result, first_observation=first)
        return result

    def _add_roles(self, orgnr: str, profile: CompanyProfile, report: EvidenceReport,
                   warnings: list[str], groups: set[str]) -> None:
        try:
            rec = self.source.roles(orgnr)
        except (SourceUnavailable, BudgetExceeded) as exc:
            warnings.append(f"roles not retrieved: {exc}")
            return
        groups.add("roles")
        if rec is None:
            return
        try:
            verify_identity(orgnr, rec)
        except IdentityMismatch as exc:
            warnings.append(f"roles discarded: {exc}")
            return
        facts, related = extract_roles(rec)
        profile.facts += validate(facts, orgnr, rec.payload, report)
        profile.related_entities += related

    def _add_financials(self, orgnr: str, profile: CompanyProfile, report: EvidenceReport,
                        warnings: list[str], groups: set[str]) -> None:
        if self._accounts_failures >= self.CIRCUIT_BREAKER:
            warnings.append("financials not retrieved: accounts source disabled for this run after repeated failures")
            return
        try:
            rec = self.source.accounts(orgnr)
        except (SourceUnavailable, BudgetExceeded) as exc:
            if isinstance(exc, SourceUnavailable):
                self._accounts_failures += 1
            warnings.append(f"financials not retrieved: {exc}")
            return
        self._accounts_failures = 0
        groups.add("financials")
        if rec is None:
            return
        facts, fin_warnings = extract_financials(orgnr, rec)
        warnings.extend(fin_warnings)
        profile.facts += validate(facts, orgnr, rec.payload, report)
