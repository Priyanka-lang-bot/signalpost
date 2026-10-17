from signalpost.freshness import decide, is_stale
from signalpost.offline_fixtures import BASE, ORG_A, REGN, enhet
from signalpost.schemas import CompanyProfile, Fact
from tests.conftest import make


def test_stale_by_retrieval_time():
    assert is_stale("2026-10-16T00:00:00+00:00", None, "2026-10-17T00:00:00+00:00", None)
    assert not is_stale("2026-10-18T00:00:00+00:00", None, "2026-10-17T00:00:00+00:00", None)


def test_stale_by_source_modification_http_date():
    assert is_stale("2026-10-18T00:00:00+00:00", "Mon, 12 Oct 2026 05:00:00 GMT",
                    "2026-10-17T00:00:00+00:00", "Thu, 15 Oct 2026 05:00:00 GMT")


def test_decide():
    row = {"value": 1, "retrieved_at": "2026-10-17T00:00:00+00:00", "source_modified_at": None}
    assert decide(None, 1, "2026-10-17T00:00:00+00:00", None) == "new"
    assert decide(row, 1, "2026-10-18T00:00:00+00:00", None) == "unchanged"
    assert decide(row, 2, "2026-10-18T00:00:00+00:00", None) == "changed"
    assert decide(row, 2, "2026-10-16T00:00:00+00:00", None) == "stale"


def test_changed_fact_detected_and_logged():
    agent, sess = make()
    first = agent.lookup(ORG_A)
    assert first.changes == [] and "first time" in first.explanation
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA", antallAnsatte=50, konkurs=True))
    second = agent.lookup(ORG_A)
    fields = {c.field: c for c in second.changes}
    assert fields["registered_employees"].old_value == 42 and fields["registered_employees"].new_value == 50
    assert fields["bankrupt"].new_value is True
    assert "is registered as bankrupt" in second.explanation
    kinds = [h["kind"] for h in agent.store.history(ORG_A) if h["field"] == "registered_employees"]
    assert kinds == ["added", "changed"]


def test_removed_field_detected():
    agent, sess = make()
    agent.lookup(ORG_A)
    body = enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA")
    del body["stiftelsesdato"]
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, body)
    r = agent.lookup(ORG_A)
    assert any(c.field == "founding_date" and c.kind == "removed" for c in r.changes)
    assert agent.store.load_profile(ORG_A).value("founding_date") is None


def test_failed_enrichment_does_not_delete_stored_financials():
    agent, sess = make()
    agent.lookup(ORG_A)
    sess.set(f"{REGN}/{ORG_A}", 503)
    r = agent.lookup(ORG_A)
    assert r.outcome == "ok"
    assert any("financials not retrieved" in w for w in r.warnings)
    assert agent.store.load_profile(ORG_A).value("financials.revenue") == 1_000_000


def test_stale_snapshot_never_overwrites_newer_fact():
    agent, _ = make()
    agent.lookup(ORG_A)
    stored = agent.store.load_profile(ORG_A)
    old = stored.fact("name")
    stale = Fact(**{**old.model_dump(), "value": "OLD NAME", "retrieved_at": "2000-01-01T00:00:00+00:00"})
    profile = CompanyProfile(orgnr=ORG_A, entity_kind="enhet", registry_state="registered", facts=[stale])
    changes = agent.store.save_profile(profile, {"entity"})
    assert agent.store.load_profile(ORG_A).value("name") == "SYNTETISK TESTSELSKAP ASA"
    assert all(c.kind != "removed" for c in changes)  # older snapshot cannot delete newer facts


def test_repeat_runs_do_not_duplicate():
    agent, _ = make()
    for _ in range(3):
        agent.lookup(ORG_A)
    assert agent.store.all_orgnrs() == [ORG_A]
    assert agent.store.conn.execute("SELECT COUNT(*) FROM facts WHERE orgnr=? AND field='name'", (ORG_A,)).fetchone()[0] == 1


def test_410_purges_stored_copy():
    agent, sess = make()
    agent.lookup(ORG_A)
    sess.set(f"{BASE}/enheter/{ORG_A}", 410)
    r = agent.lookup(ORG_A)
    assert r.outcome == "removed_from_registry"
    assert agent.store.load_profile(ORG_A) is None
