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


def enrich(agent: Agent, reserve: int = 25, limit: int | None = None) -> dict[str, Any]:
    """Backfill roles and annual accounts for stored companies that lack them.

    Runs only while more than `reserve` requests remain in the run budget, so a daily
    run converges over several days without ever exceeding its limits. Companies whose
    source has no data ("none") are not re-requested; failures are retried next run.
    """
    started = utcnow_iso()
    store = agent.store
    agent._accounts_failures = 0  # each enrich run starts with the outage breaker closed
    need_roles = set(store.orgnrs_needing("roles")) if agent.settings.include_roles else set()
    need_fin = set(store.orgnrs_needing("financials")) if agent.settings.include_financials else set()
    def known_failing(n: str) -> bool:
        chk = store.group_check(n, "financials")
        return bool(chk and chk["outcome"] == "failed")

    # Order: companies never tried first, then retries of earlier failures (single attempt each).
    todo = sorted(need_fin | need_roles, key=lambda n: (known_failing(n), n not in need_fin, n))
    if limit is not None:
        todo = todo[:limit]
    outcomes: Counter[str] = Counter()
    stop_reason = "completed"
    for n in todo:
        cost = int(n in need_roles) + int(n in need_fin)
        if agent.client.remaining_requests - cost < reserve:
            stop_reason = f"request reserve reached ({agent.client.remaining_requests} left)"
            break
        if agent.client.stats.elapsed_seconds > agent.settings.max_seconds - 60:
            stop_reason = "time limit approaching"
            break
        try:
            res = agent.enrich(n, roles=n in need_roles, financials=n in need_fin)
        except BudgetExceeded as exc:
            stop_reason = f"budget: {exc}"
            break
        for grp, out in res.items():
            outcomes[f"{grp}_{out}"] += 1
    report = {"started_at": started, "finished_at": utcnow_iso(), "candidates": len(todo),
              "needing_roles": len(need_roles), "needing_financials": len(need_fin),
              "outcomes": dict(outcomes), "stop_reason": stop_reason, "run": agent._run_stats()}
    store.record_run("enrich", started, report)
    return report


def dataset_report(agent: Agent, target: int | None = None) -> dict[str, Any]:
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
    out = {
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
        "financials_status": agent.store.group_status("financials"),
        "roles_status": agent.store.group_status("roles"),
    }
    if target is not None:
        # Verified from the stored data itself, not copied from a run log.
        out["target"] = target
        out["target_met"] = total >= target and distinct == total and invalid == 0 and no_provenance == 0
    build = agent.store.last_run("build_dataset")
    if build:
        out["last_build"] = {k: build.get(k) for k in ("started_at", "finished_at", "target", "target_met",
                                                        "stop_reason", "run")}
    return out


EXPORT_FORMAT = "signalpost.companies.v2"


def export_record(agent: Agent, profile: Any) -> dict[str, Any]:
    """One exported company profile.

    Format v2 (documented in README section 7):
    * `retrievals` lists each distinct URL actually fetched (`url`) once per profile, and
      every fact references it with `retrieval` (e.g. "R1") instead of repeating the URL.
      A batch-refresh URL holds 100 organisation numbers, and repeating it on every fact
      made the file roughly 3x larger. `source_url` (the record that states the fact),
      `json_path` and `retrieved_at` stay on every fact.
    * `availability` says, per optional group (roles, financials), whether data was found,
      whether the source has none, or why it is unavailable. Missing values are never filled.
    """
    d = profile.model_dump(mode="json")
    refs: dict[str, str] = {}
    for f in d["facts"]:
        via = f.pop("retrieved_via")
        if via not in refs:
            refs[via] = f"R{len(refs) + 1}"
        f["retrieval"] = refs[via]
    d["retrievals"] = [{"id": rid, "url": url} for url, rid in refs.items()]
    present = {f["field"] for f in d["facts"]}
    availability: dict[str, Any] = {}
    for grp, fact in (("roles", "roles"), ("financials", "financials.period_end")):
        chk = agent.store.group_check(profile.orgnr, grp)
        if fact in present:
            status = "available"
        elif chk is None:
            status = "not_checked"
        else:
            status = {"none": "source_has_none", "failed": "source_error",
                      "skipped": "not_attempted", "found": "available"}.get(chk["outcome"], chk["outcome"])
        entry: dict[str, Any] = {"status": status}
        if status != "available" and chk is not None:
            entry.update(reason=chk.get("reason") or None, checked_at=chk.get("checked_at"),
                         consecutive_failed_runs=chk.get("consecutive_failed_runs", 0))
        availability[grp] = entry
    d["availability"] = availability
    d["format"] = EXPORT_FORMAT
    return d


def export(agent: Agent, out_dir: Path, target: int = 1000) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "companies.jsonl"
    n = 0
    with path.open("w", encoding="utf-8", newline="\n") as fh:  # identical bytes on every OS
        for orgnr in agent.store.all_orgnrs():
            p = agent.store.load_profile(orgnr)
            if p is None:
                continue
            fh.write(json.dumps(export_record(agent, p), ensure_ascii=False) + "\n")
            n += 1
    report = dataset_report(agent, target=target)
    report["exported_profiles"] = n
    report["target_met"] = report["target_met"] and n >= target
    report["exported_at"] = utcnow_iso()
    report["export_format"] = EXPORT_FORMAT
    report["export_bytes"] = path.stat().st_size
    (out_dir / "dataset_stats.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return {"path": str(path), **report}
