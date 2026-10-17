"""Reproducible dataset build, refresh (update detection) and export."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .agent import Agent
from .fact_extraction import CORE_FIELDS
from .http import BudgetExceeded, SourceUnavailable, utcnow_iso
from .orgnr import is_valid

# Official search queries, run in order until the target is reached.
# ASA = public limited companies; then limited companies registered in Oslo.
# A fixed sort order makes page N stable between runs, so resuming is deterministic.
# Verified live 2026-10-17: these two queries yield 1,000 distinct, checksum-valid
# entities in 11 requests (ASA: 213 entities / 3 pages; Oslo AS: 86,174 / 862 pages).
DEFAULT_QUERIES: list[dict[str, Any]] = [
    {"organisasjonsform": "ASA", "sort": "organisasjonsnummer,ASC"},
    {"organisasjonsform": "AS", "kommunenummer": "0301", "sort": "organisasjonsnummer,ASC"},
]


def _job_id(query: dict[str, Any], page_size: int) -> str:
    return hashlib.sha1(json.dumps([query, page_size], sort_keys=True).encode()).hexdigest()[:12]


def build(agent: Agent, target: int = 1000, page_size: int = 100,
          queries: list[dict[str, Any]] | None = None, enrich: bool = True) -> dict[str, Any]:
    started = utcnow_iso()
    store = agent.store
    stats: Counter[str] = Counter()
    seen: set[str] = set()
    failures: list[str] = []
    stop_reason = "target reached"
    stored = len(store.all_orgnrs())
    for query in queries or DEFAULT_QUERIES:
        if stored >= target:
            break
        job = _job_id(query, page_size)
        start_page = 0
        while store.page_done(job, start_page):  # resume after the last completed page
            start_page += 1
        stats["pages_skipped_resume"] += start_page
        try:
            for page, records, info in agent.source.search_pages(query, page_size=page_size, start_page=start_page):
                stats["records_received"] += info.get("raw_items", len(records))
                stats["records_without_orgnr"] += info.get("raw_items", len(records)) - len(records)
                page_complete = True
                for rec in records:
                    n = rec.requested_orgnr
                    if n in seen:
                        stats["duplicates"] += 1
                        continue
                    seen.add(n)
                    if not is_valid(n):
                        stats["invalid_orgnr"] += 1
                        failures.append(f"{n}: invalid organisation number from source")
                        continue
                    already = store.has_company(n)
                    if stored >= target and not already:
                        page_complete = False
                        continue
                    result = agent.process(n, n, rec, enrich=enrich, roles=False)  # roles on demand; keeps build < 2,000 requests
                    stats[f"outcome_{result.outcome}"] += 1
                    if result.outcome == "ok" and not already:
                        stored += 1
                    elif result.outcome != "ok":
                        failures.append(f"{n}: {result.outcome}: {'; '.join(result.warnings)[:200]}")
                if page_complete:
                    store.mark_page(job, page)
                stats["pages_fetched"] += 1
                if stored >= target:
                    break
        except BudgetExceeded as exc:
            stop_reason = f"budget: {exc}"
            break
        except SourceUnavailable as exc:
            stop_reason = f"source unavailable: {exc}"
            failures.append(str(exc))
            continue
    else:
        if len(store.all_orgnrs()) < target:
            stop_reason = "queries exhausted before target"
    report = dataset_report(agent)
    report.update({
        "started_at": started,
        "finished_at": utcnow_iso(),
        "target": target,
        "target_met": report["companies_stored"] >= target,
        "stop_reason": stop_reason,
        "this_run": dict(stats),
        "failures_sample": failures[:50],
        "failure_count": len(failures),
        "queries": queries or DEFAULT_QUERIES,
        "run": agent._run_stats(),
    })
    store.record_run("build_dataset", started, report)
    return report


def refresh(agent: Agent, limit: int | None = None, enrich: bool = False,
            checked_before: str | None = None) -> dict[str, Any]:
    """Re-check stored companies in batches of 100 and record changes.

    `checked_before` skips companies already checked since that time (e.g. earlier in the same run).
    """
    started = utcnow_iso()
    orgnrs = agent.store.orgnrs_checked_before(checked_before) if checked_before else agent.store.all_orgnrs()
    orgnrs = orgnrs[: limit or None]
    counts: Counter[str] = Counter()
    batch: Counter[str] = Counter()
    changed: dict[str, list[dict]] = {}
    stop_reason = "completed"
    try:
        for i in range(0, len(orgnrs), 100):
            chunk = orgnrs[i:i + 100]
            wanted = set(chunk)
            got: dict[str, Any] = {}
            try:
                for rec in agent.source.batch_lookup(chunk):
                    if rec.requested_orgnr in wanted:  # identity gate: ignore anything not asked for
                        got[rec.requested_orgnr] = rec
                    else:
                        batch["unrequested_results_dropped"] += 1
            except SourceUnavailable as exc:
                batch["failed_batches"] += 1
                agent.store.record_run("refresh_batch_error", started, {"error": str(exc)})
            for n in chunk:
                res = agent.process(n, n, got[n], enrich=enrich) if n in got else agent.lookup(n, enrich=enrich)
                counts[res.outcome] += 1
                if res.changes:
                    changed[n] = [c.model_dump() for c in res.changes]
    except BudgetExceeded as exc:
        stop_reason = f"budget: {exc}"
    report = {"started_at": started, "finished_at": utcnow_iso(), "checked": sum(counts.values()),
              "outcomes": dict(counts), "batch": dict(batch), "companies_changed": len(changed), "changes": changed,
              "stop_reason": stop_reason, "run": agent._run_stats()}
    agent.store.record_run("refresh", started, {k: v for k, v in report.items() if k != "changes"})
    return report


def dataset_report(agent: Agent) -> dict[str, Any]:
    conn = agent.store.conn
    total = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    distinct = conn.execute("SELECT COUNT(DISTINCT orgnr) FROM companies").fetchone()[0]
    field_counts = {r[0]: r[1] for r in conn.execute("SELECT field, COUNT(*) FROM facts GROUP BY field")}
    missing_core = {f: total - field_counts.get(f, 0) for f in CORE_FIELDS}
    invalid = sum(1 for n in agent.store.all_orgnrs() if not is_valid(n))
    no_provenance = conn.execute(
        "SELECT COUNT(*) FROM facts WHERE source_url='' OR retrieved_at='' OR source_url NOT LIKE '%' || orgnr || '%'"
    ).fetchone()[0]
    states = {r[0]: r[1] for r in conn.execute("SELECT registry_state, COUNT(*) FROM companies GROUP BY 1")}
    return {
        "companies_stored": total,
        "distinct_orgnrs": distinct,
        "invalid_orgnrs": invalid,
        "registry_states": states,
        "facts_total": sum(field_counts.values()),
        "facts_without_provenance": no_provenance,
        "field_coverage": dict(sorted(field_counts.items())),
        "missing_core_fields": missing_core,
        "with_financials": field_counts.get("financials.period_end", 0),
        "with_roles": field_counts.get("roles", 0),
    }


def export(agent: Agent, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "companies.jsonl"
    n = 0
    with path.open("w", encoding="utf-8") as fh:
        for orgnr in agent.store.all_orgnrs():
            p = agent.store.load_profile(orgnr)
            if p is None:
                continue
            fh.write(json.dumps(p.model_dump(mode="json"), ensure_ascii=False) + "\n")
            n += 1
    report = dataset_report(agent)
    report["exported_profiles"] = n
    report["exported_at"] = utcnow_iso()
    (out_dir / "dataset_stats.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"path": str(path), **report}
