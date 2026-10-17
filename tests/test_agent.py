import json

from signalpost import dataset
from signalpost.cli import EXIT
from signalpost.offline_fixtures import BASE, ORG_A, ORG_B, FakeResponse, FakeSession, enhet
from tests.conftest import make


def test_invalid_input_makes_no_requests():
    agent, sess = make()
    r = agent.lookup("12345")
    assert r.outcome == "invalid_input" and sess.calls == []
    assert EXIT[r.outcome] == 2


def test_server_errors_retry_then_report_unavailable():
    agent, sess = make(max_retries=2)
    sess.set(f"{BASE}/enheter/{ORG_A}", 503)
    r = agent.lookup(ORG_A)
    assert r.outcome == "source_unavailable"
    assert len(sess.calls) == 3  # 1 try + 2 retries
    assert agent.client.stats.retries == 2


def test_rate_limit_then_success():
    agent, sess = make(max_retries=3)
    url = f"{BASE}/enheter/{ORG_A}"
    attempts = {"n": 0}

    def flaky(u):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return FakeResponse(429, None, {"Retry-After": "1"})
        return FakeResponse(200, enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA"))

    sess.routes[url] = flaky
    assert agent.lookup(ORG_A, enrich=False).outcome == "ok"


def test_malformed_json_is_not_published():
    agent, sess = make()
    sess.routes[f"{BASE}/enheter/{ORG_A}"] = (200, None, {"raw_text": "<html>oops</html>"})
    r = agent.lookup(ORG_A)
    assert r.outcome == "source_unavailable" and r.profile is None


def test_payload_without_orgnr_is_rejected():
    agent, sess = make()
    body = enhet(ORG_A, "X")
    del body["organisasjonsnummer"]
    sess.set(f"{BASE}/enheter/{ORG_A}", 200, body)
    assert agent.lookup(ORG_A).outcome == "identity_mismatch"


def test_request_budget_is_hard():
    agent, sess = make(max_requests=2)
    r1 = agent.lookup(ORG_A)  # entity + roles = 2 requests; accounts must be refused
    assert r1.outcome == "ok"
    assert agent.client.stats.outbound_requests == 2
    assert any("financials not retrieved" in w and "budget" in w for w in r1.warnings)
    assert agent.lookup(ORG_B).outcome == "budget_exceeded"
    assert agent.client.stats.outbound_requests == 2


def test_time_budget_is_hard():
    agent, _ = make(max_seconds=0)
    assert agent.lookup(ORG_A).outcome == "budget_exceeded"


def test_json_output_is_serialisable(agent_and_session):
    agent, _ = agent_and_session
    out = agent.lookup(ORG_A).model_dump(mode="json")
    json.dumps(out)
    assert out["explanation_method"] == "deterministic_template"


# -- dataset pipeline ---------------------------------------------------------

def _search_session(total: int, page_size: int, dup_every: int = 0) -> FakeSession:
    from signalpost.orgnr import check_digit
    nums = []
    i = 0
    while len(nums) < total:
        p = f"8{i:07d}"
        i += 1
        c = check_digit(p)
        if c is not None:
            nums.append(p + str(c))
    sess = FakeSession(routes={})

    def search(url):
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(url).query)
        page, size = int(q["page"][0]), int(q["size"][0])
        chunk = nums[page * size:(page + 1) * size]
        if dup_every and chunk:
            chunk = chunk + [chunk[0]]  # duplicate inside the page
        pages = (len(nums) + size - 1) // size
        return FakeResponse(200, {"_embedded": {"enheter": [enhet(n, f"SYNTETISK {n}") for n in chunk]},
                                  "page": {"size": size, "totalElements": len(nums), "totalPages": pages, "number": page}})

    sess.routes[f"{BASE}/enheter"] = search
    return sess


def test_dataset_build_reaches_target_with_unique_numbers():
    sess = _search_session(250, 50, dup_every=1)
    agent, _ = make(session=sess)
    rep = dataset.build(agent, target=200, page_size=50, queries=[{"organisasjonsform": "AS"}], enrich=False)
    assert rep["target_met"] and rep["companies_stored"] == 200
    assert rep["distinct_orgnrs"] == 200 and rep["invalid_orgnrs"] == 0
    assert rep["this_run"]["duplicates"] >= 1
    assert rep["facts_without_provenance"] == 0
    assert agent.client.stats.outbound_requests == 4  # 4 pages of 50, no per-company calls


def test_dataset_build_is_resumable():
    sess = _search_session(300, 50)
    agent, _ = make(session=sess, max_requests=2)
    first = dataset.build(agent, target=300, page_size=50, queries=[{"x": "1"}], enrich=False)
    assert not first["target_met"] and first["stop_reason"].startswith("budget")
    agent.client.settings = agent.client.settings.__class__(min_interval=0.0, max_requests=100)
    agent.client.stats.outbound_requests = 0
    second = dataset.build(agent, target=300, page_size=50, queries=[{"x": "1"}], enrich=False)
    assert second["target_met"] and second["this_run"]["pages_skipped_resume"] == 2


def test_refresh_ignores_unrequested_batch_results():
    agent, sess = make()
    agent.lookup(ORG_A, enrich=False)
    # batch search returns the requested company AND an unrequested one
    sess.routes[f"{BASE}/enheter"] = lambda u: FakeResponse(200, {"_embedded": {"enheter": [
        enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA", antallAnsatte=7), enhet(ORG_B, "INTRUDER")]}})
    rep = dataset.refresh(agent)
    assert rep["outcomes"] == {"ok": 1} and rep["checked"] == 1
    assert rep["batch"] == {"unrequested_results_dropped": 1}
    assert rep["companies_changed"] == 1
    assert not agent.store.has_company(ORG_B)


def test_accounts_outage_trips_circuit_breaker():
    from signalpost.offline_fixtures import REGN
    sess = _search_session(20, 20)
    agent, _ = make(session=sess)
    orig_get = sess.get  # every accounts call below fails with 503

    def get(url, headers=None, timeout=None):
        if url.startswith(REGN):
            sess.calls.append(url)
            return FakeResponse(503, None)
        return orig_get(url, headers, timeout)

    sess.get = get
    dataset.build(agent, target=20, page_size=20, queries=[{"q": "1"}], enrich=True)
    accounts_calls = [c for c in sess.calls if c.startswith(REGN)]
    # max_retries=1 -> 2 calls per failing company, breaker opens after 5 companies
    assert len(accounts_calls) == 10
    assert len(agent.store.all_orgnrs()) == 20
