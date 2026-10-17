"""Regressions from the final pre-submission audit (synthetic fixtures)."""

import json

from signalpost import cli, dataset
from signalpost.offline_fixtures import REGN
from tests.conftest import make
from tests.test_agent import _search_session
from tests.test_enrich_and_stats import _serve_enrichment


def _built(n, **kw):
    sess = _search_session(n, n)
    agent, _ = make(session=sess, **kw)
    dataset.build(agent, target=n, page_size=n, queries=[{"q": "1"}], enrich=False)
    return agent, sess


def _accounts_calls(sess, n):
    return [c for c in sess.calls if c == f"{REGN}/{n}"]


def test_persistently_failing_accounts_stop_being_requested():
    """Observed live: the accounts API answers HTTP 500 for banks and insurers on every call."""
    agent, sess = _built(3, max_retries=3)
    bank = agent.store.all_orgnrs()[0]
    _serve_enrichment(sess, failing_accounts={bank})
    dataset.enrich(agent, reserve=0)                       # run 1: normal retries
    assert len(_accounts_calls(sess, bank)) == 4
    for expected_calls in (5, 6):                          # runs 2 and 3: one attempt each
        dataset.enrich(agent, reserve=0)
        assert len(_accounts_calls(sess, bank)) == expected_calls
    chk = agent.store.group_check(bank, "financials")
    assert chk["outcome"] == "failed" and chk["consecutive_failed_runs"] == 3 and "HTTP 503" in chk["reason"]
    assert bank not in agent.store.orgnrs_needing("financials")
    dataset.enrich(agent, reserve=0)                       # run 4: not requested any more
    assert len(_accounts_calls(sess, bank)) == 6
    assert agent.store.group_status("financials")["source_error_persistent"] == 1
    assert agent.store.load_profile(bank).value("financials.revenue") is None


def test_known_failures_do_not_trip_breaker_for_other_companies():
    agent, sess = _built(12, max_retries=0)
    nums = agent.store.all_orgnrs()
    banks = set(nums[:8])
    _serve_enrichment(sess, failing_accounts=banks)
    dataset.enrich(agent, reserve=0)                       # run 1: breaker opens after 5 failures
    skipped = [n for n in nums if agent.store.group_check(n, "financials")["outcome"] == "skipped"]
    assert skipped, "breaker should have paused the accounts source in run 1"
    assert all(agent.store.group_check(n, "financials")["consecutive_failed_runs"] == 0 for n in skipped)
    dataset.enrich(agent, reserve=0)                       # run 2: skipped ones first, banks after, single attempts
    for n in set(nums) - banks:
        assert agent.store.load_profile(n).value("financials.revenue") == 1_000_000
    assert all(agent.store.group_check(n, "financials")["outcome"] == "failed" for n in banks)


def test_export_v2_compact_refs_and_explicit_availability(tmp_path):
    agent, sess = _built(4)
    nums = agent.store.all_orgnrs()
    _serve_enrichment(sess, no_accounts={nums[0]}, failing_accounts={nums[1]})
    dataset.enrich(agent, reserve=0)
    rep = dataset.export(agent, tmp_path, target=4)
    assert rep["target_met"] and rep["export_format"] == "signalpost.companies.v2"
    recs = {r["orgnr"]: r for r in map(json.loads, (tmp_path / "companies.jsonl").read_text(encoding="utf-8").splitlines())}
    for r in recs.values():
        ids = {x["id"] for x in r["retrievals"]}
        assert all(f["retrieval"] in ids and "retrieved_via" not in f for f in r["facts"])
        assert all(f["source_url"].startswith("https://") and f["retrieved_at"] and f["json_path"] for f in r["facts"])
    assert recs[nums[0]]["availability"]["financials"]["status"] == "source_has_none"
    assert "404" in recs[nums[0]]["availability"]["financials"]["reason"]
    fail = recs[nums[1]]["availability"]["financials"]
    assert fail["status"] == "source_error" and "HTTP 503" in fail["reason"]
    assert not any(f["field"].startswith("financials.") for f in recs[nums[1]]["facts"])  # nothing invented
    assert recs[nums[2]]["availability"] == {"roles": {"status": "available"}, "financials": {"status": "available"}}


def test_daily_writes_bom_free_report_with_stage_deltas(tmp_path, monkeypatch):
    sess = _search_session(10, 10)
    agent, _ = make(session=sess)
    _serve_enrichment(sess)
    monkeypatch.setattr(cli, "make_agent", lambda db=None, session=None: agent)
    code = cli.main(["daily", "--target", "10", "--out", str(tmp_path)])
    raw = (tmp_path / "daily_run.json").read_bytes()
    assert code == 0 and not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    rep = json.loads(raw)
    st = rep["stages"]
    assert sum(v["outbound_requests"] for v in st.values()) == rep["run"]["outbound_requests"]
    assert rep["run"]["outbound_requests"] <= rep["limits"]["max_outbound_requests"]
    assert rep["build"]["with_roles"] == 0 and rep["export"]["with_roles"] == 10  # snapshot vs final
    assert rep["run"]["external_api_cost_usd_measured"] == 0.0
