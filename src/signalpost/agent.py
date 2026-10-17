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
        # reason for the most recent roles/financials outcome, stored in group_checks
        self.last_detail: dict[str, str] = {}

    def _run_stats(self) -> dict:
        s = self.client.stats
        return {
            "outbound_requests": s.outbound_requests,
            "cache_hits": s.cache_hits,
            "retries": s.retries,
            "retry_reasons": dict(s.retry_reasons),
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
        checks: dict[str, str] = {}

        if enrich and rec.kind == "enhet" and profile.registry_state == "registered":
            if self.settings.include_roles if roles is None else roles:
                checks["roles"] = self._add_roles(orgnr, profile, report, warnings, groups)
            if self.settings.include_financials:
                checks["financials"] = self._add_financials(orgnr, profile, report, warnings, groups)

        warnings.extend(report.failures)
        self.evidence.checked += report.checked
        self.evidence.passed += report.passed
        self.evidence.failures.extend(report.failures)

        first = not self.store.has_company(orgnr)
        changes = self.store.save_profile(profile, groups)
        for grp, outcome in checks.items():
            self.store.mark_group(orgnr, grp, outcome, self.last_detail.get(grp))
        result = AgentResult(input=raw, orgnr=orgnr, outcome="ok", profile=profile, changes=changes,
                             warnings=warnings, checked_at=now, run=self._run_stats())
        result.explanation = explain(result, first_observation=first)
        return result

    def _add_roles(self, orgnr: str, profile: CompanyProfile, report: EvidenceReport,
                   warnings: list[str], groups: set[str]) -> str:
        """Returns found | none | failed (recorded in group_checks)."""
        try:
            rec = self.source.roles(orgnr)
        except BudgetExceeded as exc:
            warnings.append(f"roles not retrieved: {exc}")
            self.last_detail["roles"] = f"not attempted: {exc}"
            return "skipped"
        except SourceUnavailable as exc:
            warnings.append(f"roles not retrieved: {exc}")
            self.last_detail["roles"] = str(exc)
            return "failed"
        if rec is None:
            groups.add("roles")
            self.last_detail["roles"] = "source has no roles record for this entity (HTTP 404/410)"
            return "none"
        try:
            verify_identity(orgnr, rec)
        except IdentityMismatch as exc:
            warnings.append(f"roles discarded: {exc}")
            self.last_detail["roles"] = f"identity check failed: {exc}"
            return "failed"
        groups.add("roles")
        facts, related = extract_roles(rec)
        kept = validate(facts, orgnr, rec.payload, report)
        profile.facts += kept
        profile.related_entities += related
        self.last_detail["roles"] = "" if kept else "source lists no active roles"
        return "found" if kept else "none"

    def _add_financials(self, orgnr: str, profile: CompanyProfile, report: EvidenceReport,
                        warnings: list[str], groups: set[str]) -> str:
        """Returns found | none | failed (recorded in group_checks)."""
        if self._accounts_failures >= self.CIRCUIT_BREAKER:
            msg = "not attempted: accounts source paused for this run after repeated failures"
            warnings.append(f"financials not retrieved: {msg}")
            self.last_detail["financials"] = msg
            return "skipped"  # never requested, so not counted as a failure of this company
        # A company whose accounts already failed in an earlier run is re-checked with a single
        # attempt, and its failure does not count toward the outage circuit breaker: for banks
        # and insurers the accounts API answers HTTP 500 deterministically (no figures exist
        # in this API for them), which says nothing about the API's health.
        prev = self.store.group_check(orgnr, "financials")
        known_failing = bool(prev and prev["outcome"] == "failed")
        try:
            rec = self.source.accounts(orgnr, max_retries=0 if known_failing else None)
        except BudgetExceeded as exc:
            warnings.append(f"financials not retrieved: {exc}")
            self.last_detail["financials"] = f"not attempted: {exc}"
            return "skipped"
        except SourceUnavailable as exc:
            if not known_failing:
                self._accounts_failures += 1
            warnings.append(f"financials not retrieved: {exc}")
            self.last_detail["financials"] = str(exc)
            return "failed"
        if not known_failing:
            self._accounts_failures = 0
        groups.add("financials")
        if rec is None:
            self.last_detail["financials"] = "source has no annual accounts for this company (HTTP 404)"
            return "none"
        facts, fin_warnings = extract_financials(orgnr, rec)
        warnings.extend(fin_warnings)
        kept = validate(facts, orgnr, rec.payload, report)
        profile.facts += kept
        self.last_detail["financials"] = "" if kept else "; ".join(fin_warnings) or "no usable company (SELSKAP) figures"
        return "found" if kept else "none"

    def enrich(self, orgnr: str, roles: bool, financials: bool) -> dict[str, str]:
        """Fetch only the roles and/or accounts of an already stored, registered main entity.

        Stored entity facts are kept as they are; only the fetched groups are compared and
        updated. Identity, evidence and freshness rules are the same as for a full lookup.
        """
        profile = self.store.load_profile(orgnr)
        if profile is None or profile.entity_kind != "enhet" or profile.registry_state != "registered":
            return {}
        warnings: list[str] = []
        report = EvidenceReport()
        groups: set[str] = set()
        checks: dict[str, str] = {}
        fetched_facts = CompanyProfile(orgnr=orgnr, entity_kind="enhet", registry_state="registered")
        if roles:
            checks["roles"] = self._add_roles(orgnr, fetched_facts, report, warnings, groups)
        if financials:
            checks["financials"] = self._add_financials(orgnr, fetched_facts, report, warnings, groups)
        role_rel = {"auditor", "accountant", "role_holder_entity"}
        # Only the fetched groups are written; stored entity facts are not re-saved, so their
        # retrieval/check timestamps keep reflecting when the entity record was really fetched.
        profile.facts = fetched_facts.facts
        profile.related_entities = [r for r in profile.related_entities
                                    if not ("roles" in groups and r.relation in role_rel)] + fetched_facts.related_entities
        self.evidence.checked += report.checked
        self.evidence.passed += report.passed
        self.evidence.failures.extend(report.failures)
        if groups:
            self.store.save_profile(profile, groups)
        for grp, outcome in checks.items():
            self.store.mark_group(orgnr, grp, outcome, self.last_detail.get(grp))
        return checks
