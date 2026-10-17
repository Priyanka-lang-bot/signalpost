"""Schema behaviour observed in the official v2.0.0 spec and live responses (2026-10-17).

Payloads here are SYNTHETIC (offline_fixtures) but use the field names and shapes seen live.
The last test replays the genuine live capture if it exists locally (it is git-ignored
because the roles payload contains birth dates); otherwise it is skipped.
"""

import json
from pathlib import Path

import pytest

from signalpost.fact_extraction import derive_roles
from signalpost.offline_fixtures import BASE, ORG_A, enhet, roles
from signalpost.sources.replay import CaptureIntegrityError, ReplaySession, canonical, sha256_of_capture
from tests.conftest import make

CAPTURE = Path(__file__).resolve().parents[1] / "data" / "live_captures" / "923609016.json"


def test_deleted_record_detected_by_respons_klasse():
    agent, sess = make()
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, {
        "organisasjonsnummer": ORG_A, "navn": "SYNTETISK SLETTET AS", "respons_klasse": "SlettetEnhet",
        "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"}, "slettedato": "2020-01-01",
        "_links": {"self": {"href": f"{BASE}/enheter/{ORG_A}"}}})
    r = agent.lookup(ORG_A)
    assert r.profile.registry_state == "deleted" and r.profile.value("deletion_date") == "2020-01-01"
    assert r.profile.unknown_fields == []  # deleted records legitimately lack most fields
    assert "deleted" in r.explanation


def test_employee_count_ignored_when_register_says_none_registered():
    agent, sess = make()
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, enhet(ORG_A, "SYNTETISK AS", harRegistrertAntallAnsatte=False, antallAnsatte=0))
    assert agent.lookup(ORG_A, enrich=False).profile.value("registered_employees") is None


def test_new_v2_fields_extracted():
    agent, sess = make()
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, enhet(ORG_A, "SYNTETISK AS", erIKonsern=True, konkursdato="2026-01-02",
                                                  kapital={"belop": 30000, "valuta": "NOK", "type": "Aksjekapital"}))
    p = agent.lookup(ORG_A, enrich=False).profile
    assert p.value("part_of_group") is True and p.value("bankruptcy_date") == "2026-01-02"
    assert p.value("share_capital_amount") == 30000 and p.value("share_capital_currency") == "NOK"


def test_roles_skip_deregistered_and_never_keep_birth_dates():
    payload = roles(ORG_A)
    payload["rollegrupper"][0]["sistEndret"] = "2020-11-02"
    payload["rollegrupper"][0]["roller"].append({
        "avregistrert": True, "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
        "person": {"fodselsdato": "1999-09-09", "navn": {"fornavn": "Gammel", "etternavn": "Leder"}}})
    all_roles = derive_roles(payload)
    assert "1970-01-01" not in json.dumps(all_roles) and "1999-09-09" not in json.dumps(all_roles)
    agent, sess = make()
    sess.set(f"{BASE}/enheter/{ORG_A}/roller", 200, payload)
    active = agent.lookup(ORG_A).profile.value("roles")
    assert [r["name"] for r in active if r["role_code"] == "DAGL"] == ["Test Person"]
    assert active[0]["group_last_changed"] == "2020-11-02"


def test_replay_serves_only_captured_urls_and_keeps_capture_time(tmp_path):
    cap = {"captured_with": "test", "responses": [{"url": f"{BASE}/enheter/{ORG_A}", "status": 200,
            "retrieved_at": "2026-10-17T12:18:22+00:00", "last_modified": None, "body": enhet(ORG_A, "SYNTETISK AS")}]}
    f = tmp_path / "c.json"
    f.write_text(canonical(cap), encoding="utf-8")
    sess = ReplaySession.from_file(f, sha256_of_capture(cap))
    agent, _ = make(session=sess, max_retries=0)
    r = agent.lookup(ORG_A)  # roles + accounts were not captured -> warnings, no invented values
    assert r.outcome == "ok" and r.profile.fact("name").retrieved_at == "2026-10-17T12:18:22+00:00"
    assert r.profile.value("roles") is None and r.profile.value("financials.revenue") is None
    assert any("roles not retrieved" in w for w in r.warnings)
    assert agent.lookup("800000017").outcome == "source_unavailable"


def test_replay_rejects_tampered_or_foreign_capture(tmp_path):
    cap = {"responses": [{"url": f"{BASE}/enheter/{ORG_A}", "status": 200, "body": {"navn": "X"}}]}
    f = tmp_path / "c.json"
    f.write_text(json.dumps(cap), encoding="utf-8")
    with pytest.raises(CaptureIntegrityError):
        ReplaySession.from_file(f, "0" * 64)
    with pytest.raises(CaptureIntegrityError):
        ReplaySession({"responses": [{"url": "https://evil.example/enheter/923609016", "status": 200, "body": {}}]})


@pytest.mark.skipif(not CAPTURE.exists(), reason="live capture not present (git-ignored)")
def test_genuine_live_capture_equinor():
    sess = ReplaySession.from_file(CAPTURE)
    agent, _ = make(session=sess, max_retries=0)
    r = agent.lookup("923609016")
    assert r.outcome == "ok" and r.profile.value("orgnr") == "923609016"
    assert r.profile.value("name") == "EQUINOR ASA" and r.profile.value("legal_form") == "ASA"
    assert r.profile.value("financials.accounts_type") == "SELSKAP"  # KONSERN figures not used
    assert r.profile.value("financials.currency") == "USD"
    assert agent.evidence.completeness == 1.0
    assert "fodselsdato" not in json.dumps(r.profile.model_dump(mode="json"))
