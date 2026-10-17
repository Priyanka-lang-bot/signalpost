"""Offline audit of a Signalpost database against the raw source responses it cached.

    python scripts/audit_store.py [data/signalpost.sqlite3]

For every stored fact it re-reads the cached HTTP response the fact was taken from and
checks: (1) the value at `json_path` equals the stored value (roles are re-derived),
(2) the source URL identifies the same organisation number, (3) the record inside the
payload carries that organisation number (identity), (4) a retrieval timestamp exists,
and (5) financial facts come from the latest SELSKAP item of the right company, with an
ISO currency and finite numbers. Makes no network requests. Exit code 1 on any failure.
"""

import json
import math
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from signalpost.fact_extraction import MISSING, derive_roles, get_path  # noqa: E402


def main(db: str) -> int:
    c = sqlite3.connect(db)
    cache = {u: (json.loads(b) if b else None) for u, b in c.execute("SELECT url, body_json FROM http_cache")}
    search_items = {}
    for u, b in cache.items():
        if isinstance(b, dict) and "_embedded" in b:
            for e in b["_embedded"].get("enheter", []):
                search_items[(u, e.get("organisasjonsnummer"))] = e
    fails: Counter = Counter()
    examples: list = []
    checked = uncached = 0
    for orgnr, field, vj, url, via, path_j, ret in c.execute(
            "SELECT orgnr, field, value_json, source_url, retrieved_via, json_path, retrieved_at FROM facts"):
        v, path = json.loads(vj), json.loads(path_j)
        payload = search_items.get((via, orgnr)) if "?" in via and "regnskap" not in via else cache.get(via)
        if payload is None:
            uncached += 1  # cache entry expired/absent: cannot audit offline, not a failure
            continue
        checked += 1
        problems = []
        if field == "roles":
            if not url.endswith(f"/{orgnr}/roller"):
                problems.append("url")
            if [r for r in derive_roles(payload) if not r.get("fratraadt") and not r.get("avregistrert")] != v:
                problems.append("value")
        else:
            if not url.endswith(f"/{orgnr}"):
                problems.append("url")
            found = get_path(payload, path)
            if found is MISSING or found != v or type(found) is not type(v):
                problems.append("value")
        if not ret:
            problems.append("retrieved_at")
        if field.startswith("financials."):
            item = payload[path[0]]
            same = [x for x in payload if x.get("regnskapstype") == "SELSKAP"
                    and (x.get("virksomhet") or {}).get("organisasjonsnummer") == orgnr]
            latest = max(same, key=lambda x: (x.get("regnskapsperiode") or {}).get("tilDato") or "") if same else None
            if item is not latest:
                problems.append("not latest SELSKAP item of this company")
            if not (isinstance(item.get("valuta"), str) and len(item["valuta"]) == 3):
                problems.append("currency")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isfinite(v):
                problems.append("non-finite number")
        elif field != "roles" and (not isinstance(payload, dict) or payload.get("organisasjonsnummer") != orgnr):
            problems.append("identity")  # the entity record itself must carry the requested number
        for p in problems:
            fails[p] += 1
        if problems and len(examples) < 10:
            examples.append({"orgnr": orgnr, "field": field, "problems": problems})
    # Related-entity consistency: every entity holding an active role must be linked as a
    # separate related entity (and nothing else may be linked from roles).
    for orgnr, rel_j in c.execute("SELECT orgnr, related_json FROM companies"):
        row = c.execute("SELECT value_json FROM facts WHERE orgnr=? AND field='roles'", (orgnr,)).fetchone()
        holders = sorted(r["holder_orgnr"] for r in (json.loads(row[0]) if row else [])
                         if r.get("holder_type") == "entity" and r.get("holder_orgnr"))
        linked = sorted(r["orgnr"] for r in json.loads(rel_j) if r["relation"] != "parent_unit")
        if holders != linked:
            fails["related entities inconsistent with roles"] += 1
            if len(examples) < 10:
                examples.append({"orgnr": orgnr, "role_holders": holders, "linked": linked})
    total = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    report = {"facts_total": total, "facts_audited": checked, "facts_not_auditable_offline": uncached,
              "failures": dict(fails), "examples": examples, "ok": not fails}
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "data/signalpost.sqlite3"))
