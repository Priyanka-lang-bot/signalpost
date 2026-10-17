"""SQLite persistence: current facts, auditable change history, HTTP cache, dataset progress."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .freshness import decide, fact_group
from .http import HttpResult, utcnow_iso
from .schemas import Change, CompanyProfile, Fact, FactStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    orgnr TEXT PRIMARY KEY,
    entity_kind TEXT NOT NULL,
    registry_state TEXT NOT NULL,
    name TEXT,
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    last_changed_at TEXT,
    related_json TEXT NOT NULL DEFAULT '[]',
    unknown_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS facts (
    orgnr TEXT NOT NULL REFERENCES companies(orgnr),
    field TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_via TEXT NOT NULL,
    json_path TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_modified_at TEXT,
    verification TEXT NOT NULL,
    status TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    PRIMARY KEY (orgnr, field)
);
CREATE TABLE IF NOT EXISTS fact_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    orgnr TEXT NOT NULL,
    field TEXT NOT NULL,
    kind TEXT NOT NULL,
    old_value_json TEXT,
    new_value_json TEXT,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    source_modified_at TEXT,
    recorded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_org ON fact_history(orgnr);
CREATE TABLE IF NOT EXISTS removed_entities (
    orgnr TEXT PRIMARY KEY,
    detail TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS http_cache (
    url TEXT PRIMARY KEY,
    status INTEGER NOT NULL,
    body_json TEXT,
    headers_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    fetched_epoch REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dataset_progress (
    job TEXT NOT NULL,
    page INTEGER NOT NULL,
    completed_at TEXT NOT NULL,
    PRIMARY KEY (job, page)
);
CREATE TABLE IF NOT EXISTS group_checks (
    orgnr TEXT NOT NULL,
    grp TEXT NOT NULL,            -- roles | financials
    outcome TEXT NOT NULL,        -- found | none (source has no data) | failed
    detail TEXT,
    checked_at TEXT NOT NULL,
    PRIMARY KEY (orgnr, grp)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stats_json TEXT
);
"""


def _relation_group(relation: str) -> str:
    """Which fetched group a related-entity link comes from."""
    return "entity" if relation == "parent_unit" else "roles"


def _dump(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- HTTP cache (implements http.Cache) ---------------------------------------
    def get(self, url: str, max_age_seconds: int) -> HttpResult | None:
        row = self.conn.execute("SELECT * FROM http_cache WHERE url=?", (url,)).fetchone()
        if row is None or time.time() - row["fetched_epoch"] > max_age_seconds:
            return None
        return HttpResult(url=url, status=row["status"],
                          body=json.loads(row["body_json"]) if row["body_json"] else None,
                          retrieved_at=row["retrieved_at"], headers=json.loads(row["headers_json"]),
                          from_cache=True)

    def put(self, r: HttpResult) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?)",
            (r.url, r.status, json.dumps(r.body) if r.body is not None else None,
             json.dumps(r.headers), r.retrieved_at, time.time()),
        )
        self.conn.commit()

    # -- facts -----------------------------------------------------------------
    def stored_facts(self, orgnr: str) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM facts WHERE orgnr=?", (orgnr,)).fetchall()
        return {r["field"]: {**dict(r), "value": json.loads(r["value_json"])} for r in rows}

    def has_company(self, orgnr: str) -> bool:
        return self.conn.execute("SELECT 1 FROM companies WHERE orgnr=?", (orgnr,)).fetchone() is not None

    def save_profile(self, profile: CompanyProfile, groups_fetched: Iterable[str]) -> list[Change]:
        """Merge a verified profile into the store and return the material changes.

        * new / changed facts are written and logged in fact_history;
        * an older observation never overwrites a newer stored one ("stale");
        * a stored fact that disappeared from a group that WAS fetched this time is removed.
        """
        groups = set(groups_fetched)
        now = utcnow_iso()
        stored = self.stored_facts(profile.orgnr)
        changes: list[Change] = []
        # Related entities follow the same rule as facts: those from a group that was not
        # fetched this time (e.g. auditor links when roles were skipped) are kept as stored.
        related = [r.model_dump() for r in profile.related_entities if _relation_group(r.relation) in groups]
        prev = self.conn.execute("SELECT related_json FROM companies WHERE orgnr=?", (profile.orgnr,)).fetchone()
        if prev is not None:
            related = [r for r in json.loads(prev[0]) if _relation_group(r["relation"]) not in groups] + related
        seen: set[tuple] = set()
        related = [r for r in related if not ((r["relation"], r["orgnr"]) in seen or seen.add((r["relation"], r["orgnr"])))]
        cur = self.conn.cursor()
        cur.execute(
            """INSERT INTO companies(orgnr, entity_kind, registry_state, name, first_seen_at, last_checked_at,
                                     related_json, unknown_json)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(orgnr) DO UPDATE SET entity_kind=excluded.entity_kind,
                 registry_state=excluded.registry_state, name=COALESCE(excluded.name, companies.name),
                 last_checked_at=excluded.last_checked_at, related_json=excluded.related_json,
                 unknown_json=excluded.unknown_json""",
            (profile.orgnr, profile.entity_kind, profile.registry_state, profile.value("name"), now, now,
             _dump(related), _dump(profile.unknown_fields)),
        )
        incoming = {f.field: f for f in profile.facts if f.status == FactStatus.VERIFIED}
        for name, fact in incoming.items():
            decision = decide(stored.get(name), fact.value, fact.retrieved_at, fact.source_modified_at)
            if decision == "stale":
                continue
            if decision == "unchanged":
                cur.execute("UPDATE facts SET last_checked_at=?, retrieved_at=?, retrieved_via=? WHERE orgnr=? AND field=?",
                            (now, fact.retrieved_at, fact.retrieved_via, profile.orgnr, name))
                continue
            self._write_fact(cur, profile.orgnr, fact, now)
            old = stored.get(name, {}).get("value")
            kind = "added" if decision == "new" else "changed"
            changes.append(Change(field=name, old_value=old, new_value=fact.value, kind=kind,
                                  source_url=fact.source_url, retrieved_at=fact.retrieved_at,
                                  source_modified_at=fact.source_modified_at))
        newest_retrieval = max((f.retrieved_at for f in incoming.values()), default=now)
        for name, row in stored.items():
            if name in incoming or fact_group(name) not in groups:
                continue
            if row["retrieved_at"] > newest_retrieval:
                continue  # stored data is newer than this snapshot; keep it
            cur.execute("DELETE FROM facts WHERE orgnr=? AND field=?", (profile.orgnr, name))
            changes.append(Change(field=name, old_value=row["value"], new_value=None, kind="removed",
                                  source_url=row["source_url"], retrieved_at=newest_retrieval))
        for ch in changes:
            cur.execute(
                "INSERT INTO fact_history(orgnr, field, kind, old_value_json, new_value_json, source_url,"
                " retrieved_at, source_modified_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (profile.orgnr, ch.field, ch.kind, _dump(ch.old_value), _dump(ch.new_value),
                 ch.source_url, ch.retrieved_at, ch.source_modified_at, now),
            )
        is_first_snapshot = not stored
        if changes and not is_first_snapshot:
            cur.execute("UPDATE companies SET last_changed_at=? WHERE orgnr=?", (now, profile.orgnr))
        self.conn.commit()
        return [] if is_first_snapshot else changes

    def _write_fact(self, cur: sqlite3.Cursor, orgnr: str, f: Fact, now: str) -> None:
        cur.execute(
            "INSERT OR REPLACE INTO facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (orgnr, f.field, _dump(f.value), f.source, f.source_url, f.retrieved_via, _dump(f.json_path),
             f.retrieved_at, f.source_modified_at, f.verification, f.status.value, now),
        )

    def mark_removed(self, orgnr: str, detail: str) -> list[Change]:
        """Entity returned 410 Gone: Brreg asks copies to delete it, so facts are purged."""
        now = utcnow_iso()
        stored = self.stored_facts(orgnr)
        self.conn.execute("DELETE FROM facts WHERE orgnr=?", (orgnr,))
        self.conn.execute("DELETE FROM companies WHERE orgnr=?", (orgnr,))
        self.conn.execute("INSERT OR REPLACE INTO removed_entities VALUES (?,?,?)", (orgnr, detail, now))
        self.conn.execute(
            "INSERT INTO fact_history(orgnr, field, kind, old_value_json, new_value_json, source_url,"
            " retrieved_at, source_modified_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (orgnr, "*", "removed", None, None, "https://data.brreg.no/enhetsregisteret/api/enheter/" + orgnr,
             now, None, now),
        )
        self.conn.commit()
        return [Change(field=k, old_value=v["value"], new_value=None, kind="removed",
                       source_url=v["source_url"], retrieved_at=now) for k, v in stored.items()]

    def history(self, orgnr: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM fact_history WHERE orgnr=? ORDER BY id", (orgnr,)).fetchall()
        return [dict(r) for r in rows]

    def load_profile(self, orgnr: str) -> CompanyProfile | None:
        c = self.conn.execute("SELECT * FROM companies WHERE orgnr=?", (orgnr,)).fetchone()
        if c is None:
            return None
        facts = [
            Fact(field=r["field"], value=json.loads(r["value_json"]), source=r["source"],
                 source_url=r["source_url"], retrieved_via=r["retrieved_via"],
                 json_path=json.loads(r["json_path"]), retrieved_at=r["retrieved_at"],
                 source_modified_at=r["source_modified_at"], verification=r["verification"],
                 status=FactStatus(r["status"]))
            for r in self.conn.execute("SELECT * FROM facts WHERE orgnr=? ORDER BY field", (orgnr,))
        ]
        return CompanyProfile(orgnr=orgnr, entity_kind=c["entity_kind"], registry_state=c["registry_state"],
                              facts=facts, related_entities=json.loads(c["related_json"]),
                              unknown_fields=json.loads(c["unknown_json"]))

    def all_orgnrs(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT orgnr FROM companies ORDER BY orgnr")]

    def orgnrs_checked_before(self, iso_time: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT orgnr FROM companies WHERE last_checked_at < ? ORDER BY last_checked_at, orgnr", (iso_time,))]

    # -- enrichment bookkeeping ----------------------------------------------------
    _GROUP_FACT = {"roles": "roles", "financials": "financials.period_end"}

    # A company whose source keeps failing (e.g. the accounts API answers HTTP 500 for banks
    # and insurers) is no longer re-requested after this many consecutive failed runs.
    PERSISTENT_FAILURE_RUNS = 3

    def mark_group(self, orgnr: str, grp: str, outcome: str, detail: str | None = None) -> None:
        """Record found | none | failed | skipped for one company's roles/financials group."""
        prev = self.conn.execute("SELECT outcome, detail FROM group_checks WHERE orgnr=? AND grp=?",
                                 (orgnr, grp)).fetchone()
        prev_n = 0
        if prev is not None and prev[0] in ("failed", "skipped") and prev[1]:
            try:
                prev_n = int(json.loads(prev[1]).get("consecutive_failed_runs", 0))
            except (ValueError, AttributeError):
                prev_n = 0
        n = prev_n + 1 if outcome == "failed" else prev_n if outcome == "skipped" else 0
        info = {"reason": detail or "", "consecutive_failed_runs": n}
        self.conn.execute("INSERT OR REPLACE INTO group_checks VALUES (?,?,?,?,?)",
                          (orgnr, grp, outcome, json.dumps(info, ensure_ascii=False), utcnow_iso()))
        self.conn.commit()

    def group_check(self, orgnr: str, grp: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT outcome, detail, checked_at FROM group_checks WHERE orgnr=? AND grp=?",
                                (orgnr, grp)).fetchone()
        if row is None:
            return None
        try:
            info = json.loads(row[1]) if row[1] else {}
        except ValueError:
            info = {"reason": row[1]}  # rows written before details were JSON
        if not isinstance(info, dict):
            info = {"reason": str(info)}
        return {"outcome": row[0], "checked_at": row[2], **info}

    def _persistent_failures(self, grp: str) -> set[str]:
        out = set()
        for orgnr, detail in self.conn.execute(
                "SELECT orgnr, detail FROM group_checks WHERE grp=? AND outcome='failed'", (grp,)):
            try:
                if int(json.loads(detail or "{}").get("consecutive_failed_runs", 0)) >= self.PERSISTENT_FAILURE_RUNS:
                    out.add(orgnr)
            except (ValueError, AttributeError):
                pass
        return out

    def orgnrs_needing(self, grp: str) -> list[str]:
        """Registered main entities with no stored facts for `grp`, no conclusive check of it,
        and fewer than PERSISTENT_FAILURE_RUNS consecutive failed runs."""
        fact = self._GROUP_FACT[grp]
        persistent = self._persistent_failures(grp)
        return [r[0] for r in self.conn.execute(
            """SELECT c.orgnr FROM companies c
               WHERE c.entity_kind='enhet' AND c.registry_state='registered'
                 AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.orgnr=c.orgnr AND f.field=?)
                 AND NOT EXISTS (SELECT 1 FROM group_checks g WHERE g.orgnr=c.orgnr AND g.grp=?
                                 AND g.outcome IN ('found','none'))
               ORDER BY c.orgnr""", (fact, grp)) if r[0] not in persistent]

    def group_status(self, grp: str) -> dict[str, int]:
        fact = self._GROUP_FACT[grp]
        q = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]  # noqa: E731
        eligible = q("SELECT COUNT(*) FROM companies WHERE entity_kind='enhet' AND registry_state='registered'")
        with_data = q("SELECT COUNT(DISTINCT orgnr) FROM facts WHERE field=?", fact)
        no_data = "AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.orgnr=g.orgnr AND f.field=?)"
        count = lambda outcome: q(f"SELECT COUNT(*) FROM group_checks g WHERE grp=? AND outcome=? {no_data}",  # noqa: E731
                                  grp, outcome, fact)
        return {"eligible": eligible, "with_data": with_data, "source_has_none": count("none"),
                "source_error_last_run": count("failed"), "not_attempted_last_run": count("skipped"),
                "source_error_persistent": len(self._persistent_failures(grp)),
                "not_yet_retrieved": len(self.orgnrs_needing(grp))}

    def last_run(self, kind: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT started_at, finished_at, stats_json FROM runs WHERE kind=? ORDER BY id DESC LIMIT 1",
                                (kind,)).fetchone()
        return None if row is None else {"started_at": row[0], "finished_at": row[1], **json.loads(row[2] or "{}")}

    # -- dataset progress / runs ---------------------------------------------------
    def page_done(self, job: str, page: int) -> bool:
        return self.conn.execute("SELECT 1 FROM dataset_progress WHERE job=? AND page=?", (job, page)).fetchone() is not None

    def mark_page(self, job: str, page: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO dataset_progress VALUES (?,?,?)", (job, page, utcnow_iso()))
        self.conn.commit()

    def record_run(self, kind: str, started_at: str, stats: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO runs(kind, started_at, finished_at, stats_json) VALUES (?,?,?,?)",
                          (kind, started_at, utcnow_iso(), _dump(stats)))
        self.conn.commit()
