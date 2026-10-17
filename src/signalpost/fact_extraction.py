"""Deterministic mapping from source payloads to facts. No LLM is involved.

Each fact records the JSON path of its value so `evidence.py` can re-read the
payload and confirm the value really is stated there.
"""

from __future__ import annotations

import math
from typing import Any

from .entity_resolution import accounts_items_for
from .schemas import CompanyProfile, Fact, RelatedEntity
from .sources.base import SourceRecord

# (output field, json path) for values copied verbatim from an entity record.
ENTITY_FIELDS: list[tuple[str, list[str]]] = [
    ("name", ["navn"]),
    ("legal_form", ["organisasjonsform", "kode"]),
    ("legal_form_description", ["organisasjonsform", "beskrivelse"]),
    ("business_address", ["forretningsadresse"]),
    ("location_address", ["beliggenhetsadresse"]),  # sub-entities
    ("postal_address", ["postadresse"]),
    ("industry_code", ["naeringskode1", "kode"]),
    ("industry_description", ["naeringskode1", "beskrivelse"]),
    ("industry_code_2", ["naeringskode2", "kode"]),
    ("industry_code_3", ["naeringskode3", "kode"]),
    ("institutional_sector_code", ["institusjonellSektorkode", "kode"]),
    ("registered_employees", ["antallAnsatte"]),
    ("registration_date", ["registreringsdatoEnhetsregisteret"]),
    ("founding_date", ["stiftelsesdato"]),
    ("deletion_date", ["slettedato"]),
    ("website", ["hjemmeside"]),
    ("in_business_register", ["registrertIForetaksregisteret"]),
    ("vat_registered", ["registrertIMvaregisteret"]),
    ("bankrupt", ["konkurs"]),
    ("under_liquidation", ["underAvvikling"]),
    ("under_forced_liquidation", ["underTvangsavviklingEllerTvangsopplosning"]),
    ("last_submitted_accounts_year", ["sisteInnsendteAarsregnskap"]),
    ("parent_orgnr", ["overordnetEnhet"]),
]

# Fields we expect for an ordinary registered main entity; missing ones are reported as unknown.
CORE_FIELDS = ["name", "legal_form", "industry_code", "registration_date"]

# Key figures from the latest approved company (SELSKAP) annual accounts.
FINANCIAL_FIELDS: list[tuple[str, list[str]]] = [
    ("financials.revenue", ["resultatregnskapResultat", "driftsresultat", "driftsinntekter", "sumDriftsinntekter"]),
    ("financials.operating_profit", ["resultatregnskapResultat", "driftsresultat", "driftsresultat"]),
    ("financials.net_income", ["resultatregnskapResultat", "aarsresultat"]),
    ("financials.total_assets", ["eiendeler", "sumEiendeler"]),
    ("financials.equity", ["egenkapitalGjeld", "egenkapital", "sumEgenkapital"]),
    ("financials.total_debt", ["egenkapitalGjeld", "gjeldOversikt", "sumGjeld"]),
]

MISSING = object()


def get_path(obj: Any, path: list[str | int]) -> Any:
    cur = obj
    for key in path:
        if isinstance(key, int):
            if not isinstance(cur, list) or key >= len(cur):
                return MISSING
            cur = cur[key]
        else:
            if not isinstance(cur, dict) or key not in cur:
                return MISSING
            cur = cur[key]
    return cur


def _fact(rec: SourceRecord, field: str, value: Any, path: list, verification: str = "authoritative_registry_field") -> Fact:
    return Fact(
        field=field,
        value=value,
        source=rec.source,
        source_url=rec.canonical_url,
        retrieved_via=rec.retrieved_via,
        json_path=path,
        retrieved_at=rec.retrieved_at,
        source_modified_at=rec.source_modified_at,
        verification=verification,
    )


def extract_entity(rec: SourceRecord) -> CompanyProfile:
    p = rec.payload
    facts: list[Fact] = [_fact(rec, "orgnr", p["organisasjonsnummer"], ["organisasjonsnummer"])]
    for field, path in ENTITY_FIELDS:
        v = get_path(p, path)
        if v is MISSING or v is None or v == "":
            continue
        facts.append(_fact(rec, field, v, path))
    related: list[RelatedEntity] = []
    parent = p.get("overordnetEnhet")
    if parent:
        related.append(RelatedEntity(relation="parent_unit", orgnr=str(parent),
                                     source_url=rec.canonical_url))
    present = {f.field for f in facts}
    deleted = "slettedato" in p and bool(p.get("slettedato"))
    unknown = [f for f in CORE_FIELDS if f not in present] if not deleted else []
    return CompanyProfile(
        orgnr=str(p["organisasjonsnummer"]),
        entity_kind=rec.kind,  # type: ignore[arg-type]
        registry_state="deleted" if deleted else "registered",
        facts=facts,
        related_entities=related,
        unknown_fields=unknown,
    )


# -- roles ----------------------------------------------------------------------

def derive_roles(payload: Any) -> list[dict[str, Any]]:
    """Flatten the roles payload. Birth dates are deliberately not copied."""
    out: list[dict[str, Any]] = []
    groups = payload.get("rollegrupper") if isinstance(payload, dict) else None
    for group in groups or []:
        for role in (group or {}).get("roller") or []:
            if not isinstance(role, dict):
                continue
            rtype = role.get("type") or {}
            entry: dict[str, Any] = {
                "role_code": rtype.get("kode"),
                "role": rtype.get("beskrivelse"),
            }
            person = role.get("person")
            entity = role.get("enhet")
            if isinstance(person, dict):
                n = person.get("navn") or {}
                parts = [n.get("fornavn"), n.get("mellomnavn"), n.get("etternavn")]
                entry["holder_type"] = "person"
                entry["name"] = " ".join(x for x in parts if x) or None
            elif isinstance(entity, dict):
                names = entity.get("navn")
                entry["holder_type"] = "entity"
                entry["holder_orgnr"] = entity.get("organisasjonsnummer")
                entry["name"] = " ".join(names) if isinstance(names, list) else names
            else:
                continue
            for flag in ("fratraadt", "avregistrert"):
                if flag in role:
                    entry[flag] = role[flag]
            if entry.get("role_code"):
                out.append(entry)
    return out


def extract_roles(rec: SourceRecord) -> tuple[list[Fact], list[RelatedEntity]]:
    roles = derive_roles(rec.payload)
    if not roles:
        return [], []
    active = [r for r in roles if not r.get("fratraadt") and not r.get("avregistrert")]
    facts = [_fact(rec, "roles", active, ["rollegrupper"], "derived_from_registry_fields")]
    related = []
    for r in active:
        if r.get("holder_type") == "entity" and r.get("holder_orgnr"):
            rel = {"REVI": "auditor", "REGN": "accountant"}.get(r.get("role_code") or "", "role_holder_entity")
            related.append(RelatedEntity(relation=rel, orgnr=str(r["holder_orgnr"]),  # type: ignore[arg-type]
                                         name=r.get("name"), source_url=rec.canonical_url))
    return facts, related


# -- financials -----------------------------------------------------------------

def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def extract_financials(requested: str, rec: SourceRecord) -> tuple[list[Fact], list[str]]:
    """Publish key figures only from an identity-matched, well-formed SELSKAP accounts item."""
    items, warnings = accounts_items_for(requested, rec.payload)
    company = [(i, it) for i, it in items if it.get("regnskapstype") == "SELSKAP"]
    if not company:
        if items:
            warnings.append("only group (KONSERN) or untyped accounts found; no figures published")
        return [], warnings

    def period_end(pair: tuple[int, dict]) -> str:
        return str(get_path(pair[1], ["regnskapsperiode", "tilDato"]) or "")

    idx, item = max(company, key=period_end)
    to_date = get_path(item, ["regnskapsperiode", "tilDato"])
    from_date = get_path(item, ["regnskapsperiode", "fraDato"])
    currency = item.get("valuta")
    if not isinstance(to_date, str) or not isinstance(currency, str) or len(currency) != 3:
        warnings.append("accounts lack a period end or ISO currency; no figures published")
        return [], warnings

    base: list[str | int] = [idx]
    facts = [
        _fact(rec, "financials.accounts_type", "SELSKAP", base + ["regnskapstype"]),
        _fact(rec, "financials.period_end", to_date, base + ["regnskapsperiode", "tilDato"]),
        _fact(rec, "financials.currency", currency, base + ["valuta"]),
    ]
    if isinstance(from_date, str):
        facts.append(_fact(rec, "financials.period_start", from_date, base + ["regnskapsperiode", "fraDato"]))
    for field, path in FINANCIAL_FIELDS:
        v = get_path(item, path)
        if v is MISSING or v is None:
            continue
        if not is_number(v):
            warnings.append(f"{field}: non-numeric value {v!r} rejected")
            continue
        facts.append(_fact(rec, field, v, base + path))
    return facts, warnings
