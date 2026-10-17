from signalpost.offline_fixtures import BASE, ORG_A, ORG_B, ORG_GONE, ORG_MISSING, ORG_SUB, enhet
from tests.conftest import make


def test_identifier_match_publishes(agent_and_session):
    agent, _ = agent_and_session
    r = agent.lookup(ORG_A)
    assert r.outcome == "ok"
    assert r.profile.orgnr == ORG_A and r.profile.value("orgnr") == ORG_A


def test_similar_names_stay_distinct(agent_and_session):
    agent, _ = agent_and_session
    a, b = agent.lookup(ORG_A), agent.lookup(ORG_B)
    assert a.profile.value("name").rstrip(".") == b.profile.value("name").rstrip(".")
    assert a.profile.orgnr != b.profile.orgnr
    assert agent.store.load_profile(ORG_A).value("industry_code") == "62.010"
    assert agent.store.load_profile(ORG_B).value("industry_code") == "70.100"


def test_payload_for_other_company_is_rejected():
    agent, sess = make()
    sess.set(f"{BASE}/enheter/{ORG_B}", 200, enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA"))
    r = agent.lookup(ORG_B)
    assert r.outcome == "identity_mismatch"
    assert r.profile is None
    assert not agent.store.has_company(ORG_B)


def test_self_link_pointing_elsewhere_is_rejected():
    agent, sess = make()
    body = enhet(ORG_A, "X")
    body["_links"]["self"]["href"] = f"{BASE}/enheter/{ORG_B}"
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, body)
    assert agent.lookup(ORG_A).outcome == "identity_mismatch"


def test_branch_is_separate_from_parent(agent_and_session):
    agent, _ = agent_and_session
    sub = agent.lookup(ORG_SUB)
    assert sub.profile.entity_kind == "underenhet"
    assert sub.profile.value("parent_orgnr") == ORG_A
    assert [e.relation for e in sub.profile.related_entities] == ["parent_unit"]
    assert not agent.store.has_company(ORG_A)  # parent not created from branch data


def test_role_holder_entity_not_merged(agent_and_session):
    agent, _ = agent_and_session
    r = agent.lookup(ORG_A)
    auditors = [e for e in r.profile.related_entities if e.relation == "auditor"]
    assert auditors and auditors[0].orgnr == ORG_B
    assert not agent.store.has_company(ORG_B)


def test_not_found_and_removed(agent_and_session):
    agent, _ = agent_and_session
    assert agent.lookup(ORG_MISSING).outcome == "not_found"
    gone = agent.lookup(ORG_GONE)
    assert gone.outcome == "removed_from_registry" and gone.profile is None
