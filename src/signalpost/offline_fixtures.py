"""SYNTHETIC TEST FIXTURES — NOT REAL COMPANY DATA.

These payloads imitate the documented Brreg response structure so the pipeline
can be tested without network access. Names are prefixed "SYNTETISK TESTSELSKAP"
and must never be stored in the production database or presented as real.
They are used only by the unit tests and by the offline part of the benchmark,
both of which run against an in-memory SQLite store.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from urllib.parse import urlsplit

BASE = "https://data.brreg.no/enhetsregisteret/api"
REGN = "https://data.brreg.no/regnskapsregisteret/regnskap"

# Checksum-valid numbers used only for synthetic records.
ORG_A = "800000009"
ORG_B = "800000017"   # "similar name" twin of A with a different number
ORG_SUB = "800000025"  # sub-entity (branch) whose parent is A
ORG_GONE = "811111112"  # returns 410
ORG_MISSING = "800000033"  # returns 404 everywhere


def enhet(orgnr: str, name: str, **extra: Any) -> dict[str, Any]:
    body = {
        "organisasjonsnummer": orgnr,
        "navn": name,
        "organisasjonsform": {"kode": "ASA", "beskrivelse": "Allmennaksjeselskap"},
        "forretningsadresse": {"land": "Norge", "landkode": "NO", "postnummer": "0150",
                               "poststed": "OSLO", "adresse": ["Testveien 1"], "kommune": "OSLO",
                               "kommunenummer": "0301"},
        "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmeringstjenester"},
        "antallAnsatte": 42,
        "registreringsdatoEnhetsregisteret": "1995-03-12",
        "stiftelsesdato": "1995-01-01",
        "registrertIForetaksregisteret": True,
        "registrertIMvaregisteret": True,
        "konkurs": False,
        "underAvvikling": False,
        "underTvangsavviklingEllerTvangsopplosning": False,
        "sisteInnsendteAarsregnskap": "2024",
        "_links": {"self": {"href": f"{BASE}/enheter/{orgnr}"}},
    }
    body.update(extra)
    return body


def roles(orgnr: str) -> dict[str, Any]:
    return {"rollegrupper": [
        {"type": {"kode": "DAGL", "beskrivelse": "Daglig leder/ adm.direktør"},
         "roller": [{"type": {"kode": "DAGL", "beskrivelse": "Daglig leder/ adm.direktør"},
                     "person": {"fodselsdato": "1970-01-01", "navn": {"fornavn": "Test", "etternavn": "Person"}},
                     "fratraadt": False}]},
        {"type": {"kode": "REVI", "beskrivelse": "Revisor"},
         "roller": [{"type": {"kode": "REVI", "beskrivelse": "Revisor"},
                     "enhet": {"organisasjonsnummer": ORG_B, "navn": ["SYNTETISK REVISOR AS"]},
                     "fratraadt": False}]},
    ], "_links": {"self": {"href": f"{BASE}/enheter/{orgnr}/roller"}}}


def accounts(orgnr: str, revenue: Any = 1_000_000, belongs_to: str | None = None) -> list[dict[str, Any]]:
    return [{
        "id": 1, "regnskapstype": "SELSKAP", "valuta": "NOK",
        "virksomhet": {"organisasjonsnummer": belongs_to or orgnr, "organisasjonsform": "ASA"},
        "regnskapsperiode": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"},
        "resultatregnskapResultat": {"aarsresultat": 120_000,
                                     "driftsresultat": {"driftsresultat": 150_000,
                                                        "driftsinntekter": {"sumDriftsinntekter": revenue}}},
        "eiendeler": {"sumEiendeler": 2_500_000},
        "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 900_000}, "gjeldOversikt": {"sumGjeld": 1_600_000}},
    }]


def default_routes() -> dict[str, tuple[int, Any]]:
    return {
        f"{BASE}/enheter/{ORG_A}": (200, enhet(ORG_A, "SYNTETISK TESTSELSKAP ASA")),
        f"{BASE}/enheter/{ORG_B}": (200, enhet(ORG_B, "SYNTETISK TESTSELSKAP ASA.", naeringskode1={"kode": "70.100", "beskrivelse": "Hovedkontortjenester"})),
        f"{BASE}/enheter/{ORG_A}/roller": (200, roles(ORG_A)),
        f"{REGN}/{ORG_A}": (200, accounts(ORG_A)),
        f"{BASE}/enheter/{ORG_SUB}": (404, None),
        f"{BASE}/underenheter/{ORG_SUB}": (200, {
            "organisasjonsnummer": ORG_SUB, "navn": "SYNTETISK TESTSELSKAP ASA AVD BERGEN",
            "organisasjonsform": {"kode": "BEDR", "beskrivelse": "Underenhet til næringsdrivende og offentlig forvaltning"},
            "overordnetEnhet": ORG_A,
            "beliggenhetsadresse": {"land": "Norge", "landkode": "NO", "postnummer": "5003", "poststed": "BERGEN",
                                    "adresse": ["Testgata 2"], "kommune": "BERGEN", "kommunenummer": "4601"},
            "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmeringstjenester"},
            "registreringsdatoEnhetsregisteret": "2001-05-05",
            "_links": {"self": {"href": f"{BASE}/underenheter/{ORG_SUB}"}}}),
        f"{BASE}/enheter/{ORG_GONE}": (410, None),
        f"{BASE}/enheter/{ORG_MISSING}": (404, None),
        f"{BASE}/underenheter/{ORG_MISSING}": (404, None),
    }


class FakeResponse:
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None, raw_text: str | None = None):
        self.status_code = status
        self.headers = headers or {}
        self.text = raw_text if raw_text is not None else ("" if body is None else json.dumps(body))


class FakeSession:
    """Offline stand-in for requests.Session. Routes on URL path (query ignored unless given)."""

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes = default_routes() if routes is None else routes
        self.calls: list[str] = []

    def set(self, url: str, status: int, body: Any = None, **kw: Any) -> None:
        self.routes[url] = (status, copy.deepcopy(body), kw) if kw else (status, copy.deepcopy(body))

    def get(self, url: str, headers: Any = None, timeout: Any = None) -> FakeResponse:
        self.calls.append(url)
        route = self.routes.get(url) or self.routes.get(url.split("?")[0]) or self.routes.get(urlsplit(url)._replace(query="").geturl())
        if route is None:
            return FakeResponse(404, None)
        if callable(route):
            return route(url)
        status, body, *rest = route
        extra = rest[0] if rest else {}
        return FakeResponse(status, copy.deepcopy(body), extra.get("headers"), extra.get("raw_text"))
