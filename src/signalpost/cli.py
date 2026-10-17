"""Command-line entry point.

    python -m signalpost.cli company 923609016            # human-readable
    python -m signalpost.cli company 923609016 --json     # machine-readable
    python -m signalpost.cli daily                        # build/refresh/export within budget
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .agent import Agent
from .config import load_settings
from .http import BudgetedClient, utcnow_iso
from .schemas import AgentResult
from .storage import Store

EXIT = {"ok": 0, "invalid_input": 2, "not_found": 3, "removed_from_registry": 3,
        "identity_mismatch": 4, "source_unavailable": 5, "budget_exceeded": 6}


def make_agent(db: str | None = None, session: Any = None) -> Agent:
    settings = load_settings()
    if session is not None:  # replay: retrying an uncaptured URL can never succeed
        from dataclasses import replace
        settings = replace(settings, max_retries=0, min_interval=0.0)
    store = Store(db or settings.db_path)
    # a replay session must not read from or write to the HTTP cache
    client = BudgetedClient(settings, session=session, cache=None if session is not None else store)
    return Agent(settings, store, client)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _print_human(r: AgentResult) -> None:
    print(f"Signalpost  |  input {r.input!r}  ->  {r.outcome.upper()}")
    if r.explanation:
        print("\n" + r.explanation)
    if r.profile:
        print("\nVerified facts (field = value  [source, retrieved]):")
        for f in r.profile.facts:
            val = f.value
            if f.field == "roles":
                val = f"{len(val)} active roles"
            elif isinstance(val, dict):
                val = json.dumps(val, ensure_ascii=False)
            print(f"  {f.field:32} = {val}\n  {'':32}   {f.source_url}  @ {f.retrieved_at}")
        if r.profile.related_entities:
            print("\nRelated but separate entities (not merged):")
            for e in r.profile.related_entities:
                print(f"  {e.relation:20} {e.orgnr}  {e.name or ''}")
    for w in r.warnings:
        print(f"! {w}")
    print(f"\nrun: {r.run}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="signalpost", description="Evidence-backed Norwegian company intelligence")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--db", help="SQLite path (default: $SIGNALPOST_DB or data/signalpost.sqlite3)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("company", help="look up one organisation number")
    c.add_argument("orgnr")
    c.add_argument("--json", action="store_true", help="machine-readable JSON output")
    c.add_argument("--no-enrich", action="store_true", help="skip roles and annual-accounts lookups")
    c.add_argument("--history", action="store_true", help="include the stored change history")

    b = sub.add_parser("build-dataset", help="build/resume the company dataset")
    b.add_argument("--target", type=int, default=1000)
    b.add_argument("--page-size", type=int, default=100)
    b.add_argument("--no-enrich", action="store_true", help="registry facts only (≈1 request per 100 companies)")

    r = sub.add_parser("refresh", help="re-check stored companies and record changes")
    r.add_argument("--limit", type=int)
    r.add_argument("--enrich", action="store_true", help="also re-fetch roles and accounts (1-2 requests/company)")

    e = sub.add_parser("export", help="write companies.jsonl and dataset_stats.json")
    e.add_argument("--out", default="data/export")
    e.add_argument("--target", type=int, default=1000)

    st = sub.add_parser("stats", help="print dataset statistics")
    st.add_argument("--target", type=int, default=1000)

    en = sub.add_parser("enrich", help="backfill roles/accounts for stored companies within the request budget")
    en.add_argument("--reserve", type=int, default=25, help="requests to keep unused")
    en.add_argument("--limit", type=int)

    ig = sub.add_parser("ingest", help="process a verified capture of genuine live API responses")
    ig.add_argument("capture", help="capture JSON file (see sources/replay.py)")
    ig.add_argument("orgnr")
    ig.add_argument("--sha256", help="expected SHA-256 of the canonical capture; refuse if different")
    ig.add_argument("--json", action="store_true")

    d = sub.add_parser("daily", help="one-command daily run: build to target, refresh, enrich, export")
    d.add_argument("--target", type=int, default=1000)
    d.add_argument("--out", default="data/export")

    bm = sub.add_parser("benchmark", help="measure coverage, identity, evidence, updates, cost")
    bm.add_argument("--sample", type=int, default=50, help="live sample size (0 = offline checks only)")
    bm.add_argument("--out", default="data/export/benchmark.json")

    args = ap.parse_args(argv)
    from . import dataset  # local import keeps `company` startup light

    session = None
    if args.cmd == "ingest":
        from .sources.replay import CaptureIntegrityError, ReplaySession
        try:
            session = ReplaySession.from_file(args.capture, args.sha256)
        except (CaptureIntegrityError, OSError, ValueError) as exc:
            print(f"capture rejected: {exc}", file=sys.stderr)
            return 4
    agent = make_agent(args.db, session=session)
    try:
        if args.cmd == "ingest":
            res = agent.lookup(args.orgnr)
            res.run["transport"] = {"mode": "replay of live capture", "file": args.capture,
                                    "sha256": session.sha256, "captured_with": session.capture.get("captured_with"),
                                    "uncaptured_urls_requested": session.misses}
            res.run["replayed_requests"] = res.run.pop("outbound_requests", 0)
            res.run["outbound_requests"] = 0  # replay makes no network calls
            if args.json:
                _print_json(res.model_dump(mode="json"))
            else:
                _print_human(res)
            return EXIT[res.outcome]
        if args.cmd == "company":
            res = agent.lookup(args.orgnr, enrich=not args.no_enrich)
            if args.json:
                out = res.model_dump(mode="json")
                if args.history and res.orgnr:
                    out["history"] = agent.store.history(res.orgnr)
                _print_json(out)
            else:
                _print_human(res)
            return EXIT[res.outcome]
        if args.cmd == "build-dataset":
            rep = dataset.build(agent, target=args.target, page_size=args.page_size, enrich=not args.no_enrich)
            _print_json(rep)
            return 0 if rep["target_met"] else 1
        if args.cmd == "refresh":
            _print_json(dataset.refresh(agent, limit=args.limit, enrich=args.enrich))
            return 0
        if args.cmd == "export":
            rep = dataset.export(agent, Path(args.out), target=args.target)
            _print_json(rep)
            return 0 if rep["target_met"] else 1
        if args.cmd == "stats":
            rep = dataset.dataset_report(agent, target=args.target)
            _print_json(rep)
            return 0 if rep["target_met"] else 1
        if args.cmd == "enrich":
            _print_json(dataset.enrich(agent, reserve=args.reserve, limit=args.limit))
            return 0
        if args.cmd == "daily":
            run_start = utcnow_iso()
            stages: dict[str, dict[str, Any]] = {}

            def stage(name: str, fn: Any) -> Any:
                st = agent.client.stats
                r0, t0, x0 = st.outbound_requests, st.elapsed_seconds, st.retries
                out = fn()
                stages[name] = {"outbound_requests": st.outbound_requests - r0, "retries": st.retries - x0,
                                "elapsed_seconds": round(st.elapsed_seconds - t0, 2)}
                return out

            built = stage("build", lambda: dataset.build(agent, target=args.target))
            refreshed = stage("refresh", lambda: dataset.refresh(agent, checked_before=run_start)
                              if agent.client.remaining_requests > 50 else {"skipped": "low request budget"})
            enriched = stage("enrich", lambda: dataset.enrich(agent))
            exported = stage("export", lambda: dataset.export(agent, Path(args.out), target=args.target))
            report = {
                "started_at": run_start, "finished_at": utcnow_iso(),
                "note": ("'build' statistics are a snapshot taken right after the build stage, before refresh "
                         "and enrich; 'export' is the final state of the dataset."),
                "limits": {"max_outbound_requests": agent.settings.max_requests,
                           "max_seconds": agent.settings.max_seconds,
                           "external_api_cost_limit_usd": 10.0},
                "stages": stages,
                "build": built,
                "refresh": {k: v for k, v in refreshed.items() if k != "changes"},
                "enrich": enriched,
                "export": exported,
                # measured: counted by the HTTP client; cost: no paid API is called, so 0 by construction
                "run": {**agent._run_stats(), "external_api_cost_usd_measured": 0.0},
            }
            out_file = Path(args.out) / "daily_run.json"
            out_file.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
                                encoding="utf-8", newline="\n")  # UTF-8 without BOM on every OS
            _print_json(report)
            print(f"report written to {out_file}", file=sys.stderr)
            return 0 if exported["target_met"] else 1
        if args.cmd == "benchmark":
            from .benchmark import run_benchmark
            _print_json(run_benchmark(agent, sample=args.sample, out=Path(args.out)))
            return 0
    finally:
        agent.store.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
