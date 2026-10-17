"""Regression tests for issues found by auditing the real 1,000-company database (2026-10-17).

All payloads are SYNTHETIC (offline_fixtures) but reproduce the observed situations.
"""

import copy

import pytest

from signalpost.fact_extraction import extract_financials
from signalpost.offline_fixtures import ORG_A, ORG_B, REGN, accounts
from signalpost.sources.base import SourceRecord
from tests.conftest import make
from tests.test_agent import _search_session


def _rec(payload):
    return SourceRecord("brreg:regnskapsregisteret", "accounts", ORG_A, payload, f"{REGN}/{ORG_A}",
                        f"{REGN}/{ORG_A}", "2026-10-17T00:00:00+00:00")


def _filing(year, valuta="NOK", revenue=1_000_000, typ="SELSKAP", org=ORG_A):
    f = copy.deepcopy(accounts(org, revenue=revenue)[0])
    f.update(regnskapstype=typ, valuta=valuta,
             regnskapsperiode={"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"})
    return f


def _vals(facts):
    return {f.field: f.value for f in facts}


def test_related_entities_survive_a_later_save_without_roles():
    """Observed: lookup stored Equinor's auditor link; the dataset build (roles skipped) erased it."""
    agent, _ = make()
    first = agent.lookup(ORG_A)  # full lookup: auditor = ORG_B
    assert [r.orgnr for r in first.profile.related_entities if r.relation == "auditor"] == [ORG_B]
    agent.lookup(ORG_A, roles=False)  # like the bulk build
    stored = agent.store.load_profile(ORG_A)
    assert [r.orgnr for r in stored.related_entities if r.relation == "auditor"] == [ORG_B]
    assert stored.value("roles")  # roles fact also kept


def test_related_entities_replaced_when_roles_are_refetched():
    agent, sess = make()
    agent.lookup(ORG_A)
    from signalpost.offline_fixtures import BASE, roles
    new_roles = roles(ORG_A)
    new_roles["rollegrupper"][1]["roller"][0]["enhet"]["organisasjonsnummer"] = "800000041"
    sess.set(f"{BASE}/enheter/{ORG_A}/roller", 200, new_roles)
    agent.lookup(ORG_A)
    rel = agent.store.load_profile(ORG_A).related_entities
    assert [r.orgnr for r in rel if r.relation == "auditor"] == ["800000041"]  # old auditor not kept


def test_currency_comes_from_the_same_filing_as_the_figures():
    """Observed: 6 companies changed reporting currency between years."""
    payload = [_filing(2023, "NOK", 500), _filing(2025, "USD", 900), _filing(2024, "NOK", 700)]
    facts, _ = extract_financials(ORG_A, _rec(payload))
    v = _vals(facts)
    assert v["financials.currency"] == "USD" and v["financials.revenue"] == 900
    assert v["financials.period_end"] == "2025-12-31"
    assert len({tuple(f.json_path[:1]) for f in facts}) == 1  # every figure from one filing


def test_newer_group_accounts_do_not_replace_company_accounts():
    payload = [_filing(2025, "USD", 999, typ="KONSERN"), _filing(2024, "NOK", 700)]
    v = _vals(extract_financials(ORG_A, _rec(payload))[0])
    assert v["financials.currency"] == "NOK" and v["financials.revenue"] == 700
    assert v["financials.period_end"] == "2024-12-31"


def test_conflicting_filings_for_same_period_publish_nothing():
    payload = [_filing(2025, "NOK", 700), _filing(2025, "NOK", 701)]
    facts, warnings = extract_financials(ORG_A, _rec(payload))
    assert facts == [] and any("conflicting" in w for w in warnings)


def test_identical_duplicate_filings_are_published_once():
    payload = [_filing(2025, "NOK", 700), _filing(2025, "NOK", 700)]
    v = _vals(extract_financials(ORG_A, _rec(payload))[0])
    assert v["financials.revenue"] == 700


@pytest.mark.parametrize("bad", ["nok", "NO", "NOKK", "", None, 578])
def test_malformed_currency_means_no_figures(bad):
    f = _filing(2025)
    f["valuta"] = bad
    facts, warnings = extract_financials(ORG_A, _rec([f]))
    assert facts == [] and warnings


def test_other_company_filing_ignored_even_if_newer():
    payload = [_filing(2026, "EUR", 1, org=ORG_B), _filing(2025, "NOK", 700)]
    v = _vals(extract_financials(ORG_A, _rec(payload))[0])
    assert v["financials.revenue"] == 700 and v["financials.currency"] == "NOK"


def test_build_then_lookup_keeps_everything_consistent():
    sess = _search_session(5, 5)
    agent, _ = make(session=sess)
    from signalpost import dataset
    dataset.build(agent, target=5, page_size=5, queries=[{"q": "1"}], enrich=False)
    n = agent.store.all_orgnrs()[0]
    before = agent.store.load_profile(n)
    agent.store.save_profile(before, {"entity"})  # re-saving unchanged data must be a no-op
    assert agent.store.load_profile(n).model_dump() == before.model_dump()


def test_daily_sequence_never_exceeds_shared_request_budget():
    """build + refresh + enrich share ONE client, so the hard cap covers the whole daily run."""
    from signalpost import dataset
    from tests.test_enrich_and_stats import _serve_enrichment
    sess = _search_session(60, 20)
    agent, _ = make(session=sess, max_requests=40)
    _serve_enrichment(sess)
    dataset.build(agent, target=60, page_size=20, queries=[{"q": "1"}], enrich=True)
    dataset.refresh(agent)
    dataset.enrich(agent, reserve=5)
    assert agent.client.stats.outbound_requests <= 40
    assert len(sess.calls) == agent.client.stats.outbound_requests  # every network call was counted


def test_default_limits_match_competition():
    from signalpost.config import Settings
    s = Settings()
    assert s.max_requests == 2000 and s.max_seconds + s.request_timeout <= 45 * 60
    assert s.declared_cost_per_request_usd == 0.0


def test_env_example_loads_cleanly(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from signalpost import config
    root = Path(__file__).resolve().parents[1]
    env = tmp_path / ".env"
    env.write_text((root / ".env.example").read_text(encoding="utf-8") + "\nSIGNALPOST_TIMEOUT=15 # comment\n",
                   encoding="utf-8")
    for k in list(os.environ):
        if k.startswith("SIGNALPOST_"):
            monkeypatch.delenv(k)
    monkeypatch.chdir(tmp_path)
    s = config.load_settings()
    assert s.max_requests == 2000 and s.max_seconds == 2640 and s.request_timeout == 20  # .env.example wins (setdefault)
    for k in list(os.environ):
        if k.startswith("SIGNALPOST_"):
            monkeypatch.delenv(k)


def test_refresh_responses_are_recorded_for_audit(tmp_path):
    """Batch refresh bypasses cache reads but its responses must be stored so facts stay auditable."""
    import json, subprocess, sys
    from pathlib import Path
    from signalpost import dataset
    from signalpost.agent import Agent
    from signalpost.config import Settings
    from signalpost.http import BudgetedClient
    from signalpost.offline_fixtures import BASE, FakeResponse, enhet
    from signalpost.storage import Store
    db = tmp_path / "s.sqlite3"
    sess = _search_session(5, 5)
    s = Settings(min_interval=0.0, backoff_base=0.0, max_retries=0)
    store = Store(db)
    agent = Agent(s, store, BudgetedClient(s, session=sess, cache=store, sleep=lambda _x: None))
    dataset.build(agent, target=5, page_size=5, queries=[{"q": "1"}], enrich=False)
    nums = store.all_orgnrs()
    sess.routes[f"{BASE}/enheter"] = lambda u: FakeResponse(200, {"_embedded": {"enheter": [
        enhet(n, f"SYNTETISK {n}", antallAnsatte=7) for n in nums]}})
    rep = dataset.refresh(agent)
    assert rep["companies_changed"] == 5
    store.close()
    root = Path(__file__).resolve().parents[1]
    out = json.loads(subprocess.run([sys.executable, str(root / "scripts" / "audit_store.py"), str(db)],
                                    capture_output=True, text=True).stdout)
    assert out["ok"] and out["facts_not_auditable_offline"] == 0
