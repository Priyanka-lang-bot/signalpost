"""Enrichment backfill, target_met statistics and retry diagnostics (synthetic fixtures)."""

import json
import subprocess
import sys
from pathlib import Path

from signalpost import dataset
from signalpost.offline_fixtures import BASE, REGN, FakeResponse, accounts, roles
from tests.conftest import make
from tests.test_agent import _search_session

ROOT = Path(__file__).resolve().parents[1]


def _built(n=30, **settings):
    sess = _search_session(n, n)
    agent, _ = make(session=sess, **settings)
    dataset.build(agent, target=n, page_size=n, queries=[{"q": "1"}], enrich=False)
    return agent, sess


def _serve_enrichment(sess, no_accounts=(), failing_accounts=()):
    orig = sess.get

    def get(url, headers=None, timeout=None):
        if url.startswith(REGN):
            sess.calls.append(url)
            n = url.rsplit("/", 1)[1]
            if n in failing_accounts:
                return FakeResponse(503, None)
            if n in no_accounts:
                return FakeResponse(404, None)
            return FakeResponse(200, accounts(n))
        if url.endswith("/roller"):
            sess.calls.append(url)
            n = url.split("/")[-2]
            return FakeResponse(200, roles(n))
        return orig(url, headers, timeout)

    sess.get = get


def test_export_reports_verified_target_met(tmp_path):
    agent, _ = _built(30)
    rep = dataset.export(agent, tmp_path, target=30)
    assert rep["target_met"] is True and rep["target"] == 30
    stats = json.loads((tmp_path / "dataset_stats.json").read_text(encoding="utf-8"))
    assert stats["target_met"] is True and stats["last_build"]["target_met"] is True
    assert b"\r\n" not in (tmp_path / "companies.jsonl").read_bytes()
    assert dataset.export(agent, tmp_path, target=31)["target_met"] is False


def test_enrich_backfills_roles_and_accounts_without_touching_entity_facts():
    agent, sess = _built(30)
    n0 = agent.store.all_orgnrs()[0]
    before = {k: (v["value"], v["retrieved_at"]) for k, v in agent.store.stored_facts(n0).items()}
    _serve_enrichment(sess)
    rep = dataset.enrich(agent, reserve=0)
    assert rep["outcomes"] == {"roles_found": 30, "financials_found": 30}
    after = agent.store.stored_facts(n0)
    assert all(after[k]["value"] == v and after[k]["retrieved_at"] == t for k, (v, t) in before.items())
    assert after["roles"]["value"] and after["financials.revenue"]["value"] == 1_000_000
    stats = dataset.dataset_report(agent, target=30)
    assert stats["with_roles"] == 30 and stats["roles_status"]["not_yet_retrieved"] == 0
    # nothing left to do: a second enrich makes no requests
    calls = len(sess.calls)
    assert dataset.enrich(agent, reserve=0)["candidates"] == 0 and len(sess.calls) == calls


def test_enrich_respects_request_reserve():
    agent, sess = _built(30, max_requests=21)  # 1 request used by the build
    _serve_enrichment(sess)
    rep = dataset.enrich(agent, reserve=10)
    assert rep["stop_reason"].startswith("request reserve")
    assert agent.client.stats.outbound_requests <= 21 - 10
    assert agent.client.remaining_requests >= 10


def test_enrich_records_none_and_retries_failures_next_run():
    agent, sess = _built(10)
    nums = agent.store.all_orgnrs()
    _serve_enrichment(sess, no_accounts={nums[0]}, failing_accounts={nums[1]})
    rep = dataset.enrich(agent, reserve=0)
    assert rep["outcomes"]["financials_none"] == 1 and rep["outcomes"]["financials_failed"] == 1
    st = agent.store.group_status("financials")
    assert st["source_has_none"] == 1 and st["not_yet_retrieved"] == 1  # only the failure is retried
    assert agent.store.orgnrs_needing("financials") == [nums[1]]
    assert agent.store.load_profile(nums[0]).value("financials.revenue") is None  # not invented


def test_retry_reasons_are_recorded():
    agent, sess = make(max_retries=2)
    sess.set(f"{BASE}/enheter/800000009", 503)
    r = agent.lookup("800000009")
    assert r.run["retry_reasons"] == {"HTTP 503": 2}


def test_audit_script_passes_on_consistent_store(tmp_path):
    db = tmp_path / "s.sqlite3"
    sess = _search_session(20, 20)
    from signalpost.agent import Agent
    from signalpost.config import Settings
    from signalpost.http import BudgetedClient
    from signalpost.storage import Store
    s = Settings(min_interval=0.0, backoff_base=0.0, max_retries=0)
    store = Store(db)
    agent = Agent(s, store, BudgetedClient(s, session=sess, cache=store, sleep=lambda _x: None))
    _serve_enrichment(sess)
    dataset.build(agent, target=20, page_size=20, queries=[{"q": "1"}], enrich=True)
    dataset.enrich(agent, reserve=0)
    store.close()
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_store.py"), str(db)],
                         capture_output=True, text=True)
    rep = json.loads(out.stdout)
    assert out.returncode == 0 and rep["ok"] and rep["facts_audited"] == rep["facts_total"] > 0
    # tamper with one stored value: the audit must catch it
    import sqlite3
    con = sqlite3.connect(db)
    con.execute("UPDATE facts SET value_json='\"FORGED\"' WHERE field='name' AND rowid=(SELECT MIN(rowid) FROM facts WHERE field='name')")
    con.commit(); con.close()
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "audit_store.py"), str(db)],
                         capture_output=True, text=True)
    assert out.returncode == 1 and json.loads(out.stdout)["failures"]["value"] == 1
