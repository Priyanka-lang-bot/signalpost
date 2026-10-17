"""Plain-language explanations built ONLY from verified facts and recorded changes.

This is deliberately template-based: it cannot introduce a value that is not
already a verified fact, so it cannot hallucinate company data.
"""

from __future__ import annotations

from typing import Any

from .schemas import AgentResult, Change, CompanyProfile

_STATE_FLAGS = [
    ("bankrupt", "is registered as bankrupt"),
    ("under_liquidation", "is under liquidation"),
    ("under_forced_liquidation", "is under forced liquidation or dissolution"),
]


def _addr(v: Any) -> str | None:
    if not isinstance(v, dict):
        return None
    parts = [", ".join(v.get("adresse") or []), " ".join(x for x in [v.get("postnummer"), v.get("poststed")] if x)]
    if v.get("landkode") and v.get("landkode") != "NO":
        parts.append(v.get("land") or v["landkode"])
    text = ", ".join(p for p in parts if p)
    return text or None


def _money(v: Any, cur: str) -> str:
    return f"{v:,.0f} {cur}".replace(",", " ")


def describe_profile(p: CompanyProfile) -> str:
    v = p.value
    name = v("name") or "(name not stated)"
    kind = "sub-entity (branch/business unit)" if p.entity_kind == "underenhet" else "legal entity"
    lines = [f"{name} (org. no. {p.orgnr}) is a {kind} in the Brønnøysund Register Centre's Central Coordinating Register."]
    if p.registry_state == "deleted":
        lines.append(f"The register lists it as deleted (deletion date: {v('deletion_date') or 'not stated'}).")
    if v("legal_form"):
        lines.append(f"Legal form: {v('legal_form_description') or ''} ({v('legal_form')}).".replace(" ()", ""))
    if v("registration_date"):
        lines.append(f"Registered on {v('registration_date')}" + (f", founded {v('founding_date')}." if v("founding_date") else "."))
    if v("industry_code"):
        lines.append(f"Industry (NACE): {v('industry_code')} {v('industry_description') or ''}".rstrip() + ".")
    addr = _addr(v("business_address")) or _addr(v("location_address"))
    if addr:
        lines.append(f"Registered address: {addr}.")
    if v("parent_orgnr"):
        lines.append(f"Its parent unit is org. no. {v('parent_orgnr')}; that entity is kept as a separate record.")
    if v("registered_employees") is not None:
        lines.append(f"Registered employees: {v('registered_employees')}.")
    flags = [text for key, text in _STATE_FLAGS if v(key) is True]
    lines.append(("It " + " and ".join(flags) + ".") if flags else
                 "The register shows no bankruptcy or liquidation flags." if p.entity_kind == "enhet" and p.registry_state == "registered" else "")
    roles = v("roles") or []
    leaders = [r for r in roles if r.get("role_code") in ("DAGL", "LEDE")]
    if leaders:
        lines.append("Leadership: " + "; ".join(f"{r.get('role')}: {r.get('name')}" for r in leaders) + ".")
    if v("financials.period_end"):
        cur = v("financials.currency")
        figs = [(lbl, v(f"financials.{k}")) for k, lbl in
                [("revenue", "revenue"), ("operating_profit", "operating profit"), ("net_income", "net income"),
                 ("total_assets", "total assets"), ("equity", "equity")]]
        figs_txt = ", ".join(f"{lbl} {_money(x, cur)}" for lbl, x in figs if x is not None)
        lines.append(f"Latest approved company accounts (period ending {v('financials.period_end')}): {figs_txt}.")
    if p.unknown_fields:
        lines.append("Not stated by the source: " + ", ".join(p.unknown_fields) + ".")
    return " ".join(x for x in lines if x)


def describe_changes(changes: list[Change], first_observation: bool) -> str:
    if first_observation:
        return "This is the first time Signalpost has stored this company; later runs will report changes."
    if not changes:
        return "No changes since the previous check."
    parts = []
    for c in changes[:12]:
        if c.kind == "changed":
            parts.append(f"{c.field} changed from {c.old_value!r} to {c.new_value!r}")
        elif c.kind == "added":
            parts.append(f"{c.field} is newly reported as {c.new_value!r}")
        else:
            parts.append(f"{c.field} is no longer reported (was {c.old_value!r})")
    more = f" (+{len(changes) - 12} more)" if len(changes) > 12 else ""
    return "Changes since the previous check: " + "; ".join(parts) + more + "."


def explain(result: AgentResult, first_observation: bool) -> str:
    if result.profile is None:
        return ""
    return describe_profile(result.profile) + " " + describe_changes(result.changes, first_observation)
