#!/usr/bin/env python3
"""Extract required skills for wheelhouse leads (target role, no load-bearing
dealbreaker, real JD) into `lead_skills`, and write `RequiredSkills.docx`
into each lead's package folder when that folder already exists.

Dry run by default: lists the leads it would run and the predicted cost.

Usage:
    python scripts/extract_required_skills.py                      # dry run, all wheelhouse leads
    python scripts/extract_required_skills.py --sample 10 --apply  # preview: 10 leads, mixed statuses
    python scripts/extract_required_skills.py --apply --max-cost 15
    python scripts/extract_required_skills.py --key "acme::senior software engineer" --apply --force

Long runs: pass --lock var/comms_fast.lock so each write takes the lock
briefly, instead of wrapping the whole run in with_db_lock.py (which would
stall the hourly mail cycle for the length of the run).
"""

from __future__ import annotations

import argparse
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline import db_lock  # noqa: E402
from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT  # noqa: E402
from job_tracker.pipeline.pending_workflow import _package_folder_abs  # noqa: E402
from job_tracker.pipeline.required_skills import (  # noqa: E402
    extract_required_skills,
    inbound_messages,
    input_hash,
    is_wheelhouse,
    predicted_cost,
    render_required_skills_docx,
    save_extraction,
    stored_hash,
)
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402
from job_tracker.scoring.scorer import load_framework  # noqa: E402

DOCX_NAME = "RequiredSkills.docx"
_OPEN = ("new", "pursued", "package_generated", "applied", "following_up", "interviewing", "offered")


def _candidates(conn, keys: list[str]) -> list[dict]:
    sql = (
        "SELECT normalized_key, company, title, status, jd_text FROM job_leads "
        "WHERE status != 'deleted' AND length(coalesce(jd_text, '')) > 400"
    )
    params: list[str] = []
    if keys:
        sql += f" AND normalized_key IN ({','.join('?' * len(keys))})"
        params = keys
    rows = conn.execute(sql + " ORDER BY normalized_key", params).fetchall()
    fw = load_framework()
    out = []
    for key, company, title, status, jd in rows:
        if keys or is_wheelhouse(title, jd, fw):
            out.append({"key": key, "company": company or "", "title": title or "", "status": status, "jd": jd})
    return out


def _sample(leads: list[dict], n: int) -> list[dict]:
    """Half open leads, half closed ones, spread evenly through the list."""
    open_ = [x for x in leads if x["status"] in _OPEN]
    closed = [x for x in leads if x["status"] not in _OPEN]

    def spread(items: list[dict], k: int) -> list[dict]:
        if k <= 0 or not items:
            return []
        step = max(1, len(items) // k)
        return items[::step][:k]

    first = spread(open_, (n + 1) // 2)
    return first + spread(closed, n - len(first))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--apply", action="store_true", help="Call the LLM and write results (default: dry run)")
    ap.add_argument("--key", action="append", default=[], help="Only this normalized_key (repeatable)")
    ap.add_argument("--sample", type=int, default=0, help="Pick N leads, mixed open and closed")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-cost", type=float, default=15.0, help="Stop once actual spend reaches this (USD)")
    ap.add_argument("--force", action="store_true", help="Re-extract even when the inputs are unchanged")
    ap.add_argument("--no-docx", action="store_true", help="Skip writing RequiredSkills.docx")
    ap.add_argument("--create-folders", action="store_true", help="Create the package folder when it's missing")
    ap.add_argument("--workers", type=int, default=4, help="Parallel LLM calls")
    ap.add_argument("--lock", type=Path, help="Take this DB lock around each write (e.g. var/comms_fast.lock)")
    ap.add_argument("--lock-wait", type=float, default=180.0)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    args = ap.parse_args(argv)

    conn = connect(args.db)
    leads = _candidates(conn, args.key)
    if args.sample:
        leads = _sample(leads, args.sample)
    todo = []
    for lead in leads:
        lead["messages"] = inbound_messages(conn, lead["key"])
        if not args.force and stored_hash(conn, lead["key"]) == input_hash(lead["jd"], lead["messages"]):
            continue
        todo.append(lead)
    if args.limit:
        todo = todo[: args.limit]

    predicted = sum(predicted_cost(x["jd"], x["messages"], model=args.model) or 0 for x in todo)
    print(f"{len(leads)} wheelhouse leads; {len(todo)} to extract; predicted ~${predicted:.2f} on {args.model}")
    if not args.apply:
        for lead in todo[:40]:
            print(f"  [{lead['status']}] {lead['company']} — {lead['title']}")
        if len(todo) > 40:
            print(f"  … and {len(todo) - 40} more")
        print("Dry run. Re-run with --apply to extract.")
        return 0

    def extract(lead: dict):
        return extract_required_skills(
            job_key=lead["key"],
            company=lead["company"],
            title=lead["title"],
            jd_text=lead["jd"],
            messages=lead["messages"],
            model=args.model,
        )

    def save(result) -> None:
        fh = db_lock.acquire(args.lock, wait_seconds=args.lock_wait) if args.lock else None
        if args.lock and fh is None:
            raise RuntimeError(f"could not acquire {args.lock}")
        try:
            save_extraction(conn, result)
        finally:
            if fh is not None:
                db_lock.release(fh)

    spent = 0.0
    docs = failed = 0
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {}
        for lead in todo:
            futures[pool.submit(lambda x: None if stop.is_set() else extract(x), lead)] = lead
        for i, fut in enumerate(as_completed(futures), 1):
            lead = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:  # noqa: BLE001 — one bad lead shouldn't end a long batch
                failed += 1
                print(f"[{i}/{len(todo)}] {lead['company']} — {lead['title']}: failed: {exc}", flush=True)
                continue
            if result is None:
                continue
            spent += result.cost_usd or 0
            save(result)
            req = sum(1 for s in result.skills if s.importance == "required")
            print(
                f"[{i}/{len(todo)}] {lead['company']} — {lead['title']}: "
                f"{req} required, {len(result.skills) - req} preferred (${spent:.2f} so far)",
                flush=True,
            )
            if not args.no_docx and result.skills:
                folder, _ = _package_folder_abs(
                    conn, company=lead["company"], title=lead["title"], output_root=DEFAULT_OUTPUT_ROOT
                )
                if args.create_folders or Path(folder).is_dir():
                    render_required_skills_docx(
                        result, company=lead["company"], title=lead["title"], out_path=Path(folder) / DOCX_NAME
                    )
                    docs += 1
            if spent >= args.max_cost and not stop.is_set():
                stop.set()
                print(f"Stopping: spend ${spent:.2f} reached --max-cost ${args.max_cost:.2f}", flush=True)
    print(f"Done. Spent ~${spent:.2f}; wrote {docs} {DOCX_NAME} files; {failed} failed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
