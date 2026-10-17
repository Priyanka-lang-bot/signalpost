import pytest

from signalpost.evidence import EvidenceReport, check_fact, validate
from signalpost.fact_extraction import extract_financials
from signalpost.offline_fixtures import BASE, ORG_A, ORG_B, REGN, accounts, enhet
from signalpost.schemas import Fact
from signalpost.sources.base import SourceRecord
from tests.conftest import make


def _fact(**kw):
    base = dict(field="name", value="X", source="brreg:enhetsregisteret", source_url=f"{BASE}/enheter/{ORG_A}",
                retrieved_via=f"{BASE}/enheter/{ORG_A}", json_path=["navn"], retrieved_at="2026-10-17T00:00:00+00:00")
    base.update(kw)
    return Fact(**base)


def test_every_published_fact_has_complete_provenance(agent_and_session):
    agent, _ = agent_and_session
    r = agent.lookup(ORG_A)
    assert r.profile.facts
    for f in r.profile.facts:
        assert f.source_url.startswith("https://") and ORG_A in f.source_url
        assert f.retrieved_at and f.json_path and f.verification
    assert agent.evidence.completeness == 1.0


def test_value_not_in_payload_fails():
    payload = {"navn": "REAL NAME"}
    assert "!=" in check_fact(_fact(value="INVENTED NAME"), ORG_A, payload)


def test_missing_path_fails():
    assert "not present" in check_fact(_fact(json_path=["doesNotExist"]), ORG_A, {"navn": "X"})


def test_url_for_other_company_fails():
    f = _fact(source_url=f"{BASE}/enheter/{ORG_B}")
    assert "does not identify" in check_fact(f, ORG_A, {"navn": "X"})


def test_type_mismatch_fails():
    # "42" (string) must not verify against 42 (int)
    assert check_fact(_fact(field="registered_employees", value="42", json_path=["antallAnsatte"]), ORG_A, {"antallAnsatte": 42})


def test_failed_facts_are_dropped():
    rep = EvidenceReport()
    kept = validate([_fact(), _fact(value="WRONG")], ORG_A, {"navn": "X"}, rep)
    assert len(kept) == 1 and rep.checked == 2 and rep.passed == 1 and rep.failures


def test_non_https_source_rejected_by_schema():
    with pytest.raises(ValueError):
        _fact(source_url="http://example.com/923609016")


def _acc_rec(payload):
    return SourceRecord("brreg:regnskapsregisteret", "accounts", ORG_A, payload, f"{REGN}/{ORG_A}",
                        f"{REGN}/{ORG_A}", "2026-10-17T00:00:00+00:00")


@pytest.mark.parametrize("bad", ["1 000 000", None, True, float("nan")])
def test_unsupported_financial_values_not_published(bad):
    facts, warnings = extract_financials(ORG_A, _acc_rec(accounts(ORG_A, revenue=bad)))
    assert not any(f.field == "financials.revenue" for f in facts)


def test_financials_of_other_company_discarded():
    facts, warnings = extract_financials(ORG_A, _acc_rec(accounts(ORG_A, belongs_to=ORG_B)))
    assert facts == [] and any("discarded" in w for w in warnings)


def test_group_accounts_not_presented_as_company_accounts():
    payload = accounts(ORG_A)
    payload[0]["regnskapstype"] = "KONSERN"
    facts, warnings = extract_financials(ORG_A, _acc_rec(payload))
    assert facts == [] and warnings


def test_accounts_without_currency_not_published():
    payload = accounts(ORG_A)
    del payload[0]["valuta"]
    facts, _ = extract_financials(ORG_A, _acc_rec(payload))
    assert facts == []


def test_missing_fields_reported_as_unknown():
    agent, sess = make()
    body = enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA")
    del body["naeringskode1"]
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, body)
    r = agent.lookup(ORG_A)
    assert "industry_code" in r.profile.unknown_fields
    assert r.profile.value("industry_code") is None
    assert "Not stated by the source: industry_code" in r.explanation


def test_explanation_only_uses_verified_values(agent_and_session):
    from signalpost.benchmark import grounding
    agent, _ = agent_and_session
    assert grounding(agent.lookup(ORG_A)) == 1.0
