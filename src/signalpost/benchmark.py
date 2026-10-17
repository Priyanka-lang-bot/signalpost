"""Benchmark: an offline correctness suite plus an optional live sample.

* offline: labelled synthetic scenarios (see offline_fixtures.py) run against an
  in-memory store. Measures identity accuracy, evidence completeness, update
  correctness, rejection of unsupported financials, and explanation grounding.
* live: re-looks-up a sample of stored organisation numbers against the real
  registry (fresh in-memory store, no cache) and measures coverage, identity
  accuracy, evidence completeness, requests, time and cost.
Results are labelled by mode; nothing here is a competition score.
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import Any

from .agent import Agent
from .config import Settings, load_settings
from .fact_extraction import ENTITY_FIELDS
from .http import BudgetedClient, utcnow_iso
from .offline_fixtures import BASE, ORG_A, ORG_B, ORG_GONE, ORG_MISSING, ORG_SUB, REGN, FakeSession, enhet
from .schemas import AgentResult
from .storage import Store

COVERAGE_FIELDS = ["name", "legal_form", "business_address", "industry_code", "registration_date",
                   "registered_employees", "roles", "financials.revenue"]


def grounding(result: AgentResult) -> float | None:
    """Share of dates / org numbers / large numbers in the explanation that are verified fact values."""
    if not result.profile or not result.explanation:
        return None
    blob = json.dumps([f.value for f in result.profile.facts], ensure_ascii=False) + result.orgnr
    blob_compact = blob.replace(" ", "")
    tokens = re.findall(r"\d{4}-\d{2}-\d{2}|\b\d{9}\b", result.explanation)
    tokens += [t.replace(" ", "") for t in re.findall(r"\d{1,3}(?: \d{3})+", result.explanation)]
    if not tokens:
        return 1.0
    return sum(1 for t in tokens if t in blob_compact) / len(tokens)


def _offline_agent(session: FakeSession) -> Agent:
    s = Settings(min_interval=0.0, backoff_base=0.0, max_retries=1)
    store = Store(":memory:")
    return Agent(s, store, BudgetedClient(s, session=session, cache=None, sleep=lambda _x: None))


def offline_suite() -> dict[str, Any]:
    checks: dict[str, bool] = {}
    sess = FakeSession()
    agent = _offline_agent(sess)

    a = agent.lookup(ORG_A)
    b = agent.lookup(ORG_B)
    checks["identity: A resolves to A"] = a.outcome == "ok" and a.profile is not None and a.profile.orgnr == ORG_A
    checks["identity: similar name B kept separate"] = (
        b.profile is not None and b.profile.orgnr == ORG_B and b.profile.value("industry_code") != a.profile.value("industry_code"))
    sub = agent.lookup(ORG_SUB)
    checks["identity: branch not merged into parent"] = (
        sub.profile is not None and sub.profile.entity_kind == "underenhet"
        and any(r.relation == "parent_unit" and r.orgnr == ORG_A for r in sub.profile.related_entities)
        and agent.store.load_profile(ORG_A).value("name") == a.profile.value("name"))
    checks["roles: leadership extracted, auditor kept as separate entity"] = (
        bool(a.profile.value("roles"))
        and any(r.relation == "auditor" and r.orgnr == ORG_B for r in a.profile.related_entities)
        and not any("roles" in w for w in a.warnings))
    checks["not found handled"] = agent.lookup(ORG_MISSING).outcome == "not_found"
    checks["410 removal handled"] = agent.lookup(ORG_GONE).outcome == "removed_from_registry"
    checks["invalid input rejected"] = agent.lookup("123456789").outcome == "invalid_input"

    # Wrong-company response must not publish.
    sess.set(f"{BASE}/enheter/{ORG_B}", 200, enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA"))
    sess.set(f"{BASE}/enheter/{ORG_B}".replace("enheter", "underenheter"), 404)
    agent.client.cache = None
    checks["identity: mismatched payload rejected"] = agent.lookup(ORG_B).outcome == "identity_mismatch"

    # Update detection.
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA", antallAnsatte=50))
    upd = agent.lookup(ORG_A)
    checks["update: employee change detected"] = any(c.field == "registered_employees" and c.new_value == 50 for c in upd.changes)
    again = agent.lookup(ORG_A)
    checks["update: unchanged run reports no changes"] = again.outcome == "ok" and not again.changes

    # Unsupported financial values.
    sess.set(f"{REGN}/{ORG_A}", 200, [{**_acc(ORG_A), "virksomhet": {"organisasjonsnummer": ORG_B}}])
    wrong = agent.lookup(ORG_A)
    checks["financials: other company's accounts discarded"] = wrong.profile.value("financials.revenue") is None
    sess.set(f"{REGN}/{ORG_A}", 200, _acc_list(ORG_A, revenue="1 000 000"))
    bad = agent.lookup(ORG_A)
    checks["financials: non-numeric value rejected"] = bad.profile.value("financials.revenue") is None

    total_checked, total_passed = agent.evidence.checked, agent.evidence.passed
    ground = [g for g in (grounding(r) for r in (a, b, sub, upd)) if g is not None]
    return {
        "mode": "offline (synthetic fixtures, in-memory store)",
        "checks": checks,
        "passed": sum(checks.values()),
        "total": len(checks),
        "evidence_completeness": round(total_passed / total_checked, 4) if total_checked else None,
        "explanation_grounding": round(sum(ground) / len(ground), 4) if ground else None,
    }


def _acc(orgnr: str) -> dict:
    from .offline_fixtures import accounts
    return accounts(orgnr)[0]


def _acc_list(orgnr: str, revenue: Any) -> list:
    from .offline_fixtures import accounts
    return accounts(orgnr, revenue=revenue)


def live_sample(source_store: Store, sample: int, settings: Settings | None = None) -> dict[str, Any]:
    orgnrs = source_store.all_orgnrs()
    if not orgnrs:
        return {"mode": "live", "skipped": "no stored companies; run build-dataset first"}
    rng = random.Random(42)
    picked = rng.sample(orgnrs, min(sample, len(orgnrs)))
    s = settings or load_settings()
    agent = Agent(s, Store(":memory:"), BudgetedClient(s, cache=None))
    outcomes: dict[str, int] = {}
    identity_ok = coverage_sum = 0
    ground: list[float] = []
    for n in picked:
        r = agent.lookup(n)
        outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
        if r.outcome == "ok" and r.profile:
            identity_ok += int(r.profile.orgnr == n and r.profile.value("orgnr") == n)
            coverage_sum += sum(1 for f in COVERAGE_FIELDS if r.profile.value(f) is not None) / len(COVERAGE_FIELDS)
            g = grounding(r)
            if g is not None:
                ground.append(g)
    ok = outcomes.get("ok", 0)
    st = agent.client.stats
    return {
        "mode": "live (real registry, fresh in-memory store, no cache)",
        "sample_size": len(picked),
        "outcomes": outcomes,
        "identity_accuracy": round(identity_ok / ok, 4) if ok else None,
        "mean_field_coverage": round(coverage_sum / ok, 4) if ok else None,
        "coverage_fields": COVERAGE_FIELDS,
        "evidence_completeness": round(agent.evidence.completeness, 4),
        "explanation_grounding": round(sum(ground) / len(ground), 4) if ground else None,
        "outbound_requests": st.outbound_requests,
        "requests_per_company": round(st.outbound_requests / len(picked), 2),
        "elapsed_seconds": round(st.elapsed_seconds, 2),
        "seconds_per_company": round(st.elapsed_seconds / len(picked), 3),
        "external_api_cost_usd_measured": 0.0,
    }


def run_benchmark(agent: Agent, sample: int, out: Path) -> dict[str, Any]:
    result = {"generated_at": utcnow_iso(), "offline": offline_suite(),
              "entity_fields_mapped": len(ENTITY_FIELDS)}
    if sample > 0:
        result["live"] = live_sample(agent.store, sample, agent.settings)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result
