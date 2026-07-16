#!/usr/bin/env python3
"""Batch: force the paid full-LLM-review step for every stored lead whose
DB `verdict` is currently 'pursue' — regardless of whether the free
no-LLM-review score clears the normal 70% gate (via
`force_llm_review=True` on `generate_two_tier_package`).

For each such lead:
  - Runs the LLM evaluation -> writes `full-LLM-review.docx`.
  - Persists the `llm_*` columns via `update_llm_evaluation`.
  - If the fresh LLM verdict == 'pursue': also generates a résumé + cover
    letter (`generate_two_tier_package`'s normal behavior) — restoring the
    application package that `move_packages_without_full_llm_review.py`
    quarantined earlier for genuinely-still-good leads.
  - If the fresh LLM verdict != 'pursue': downgrades `job_leads.verdict`
    in the DB to match, since a real review now disagrees with the
    earlier rule-based/triage call.

Anthropic 529 / overloaded_error is retried on the same lead with exponential
backoff (`--pause-seconds` * 2^(attempt-1)). After retries are exhausted, or
on any other per-lead error, the script aborts immediately (no further leads,
no batch pause) and exits non-zero.

Usage:
    python scripts/run_full_llm_review_for_pursue_leads.py [--limit N] [--dry-run]
    python scripts/run_full_llm_review_for_pursue_leads.py --loop [--pause-seconds N]
    python scripts/run_full_llm_review_for_pursue_leads.py --loop --retry-on-overloaded 3
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT, generate_two_tier_package  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, get_sibling_titles, update_llm_evaluation  # noqa: E402

_LOOP_BATCH_SIZE = 3
_DEFAULT_OVERLOADED_RETRIES = 3


def _is_overloaded_error(exc: BaseException) -> bool:
    """True for Anthropic capacity pressure (HTTP 529 / overloaded_error)."""
    status = getattr(exc, "status_code", None)
    if status == 529:
        return True
    text = str(exc).lower()
    return "529" in text or "overloaded_error" in text or "overloaded" in text


def _fetch_pursue_leads(conn, *, limit: int | None) -> list[dict]:
    rows = conn.execute(
        "SELECT normalized_key, company, title, jd_text FROM job_leads "
        "WHERE verdict = 'pursue' AND jd_text IS NOT NULL AND jd_text != '' "
        "ORDER BY company, title"
    ).fetchall()
    leads = [dict(r) for r in rows]
    if limit:
        leads = leads[:limit]
    return leads


def _process_lead(
    conn,
    lead: dict,
    *,
    model: str,
    output_root: Path,
    index: int,
    total: int,
    pause_seconds: float,
    retry_on_overloaded: int,
) -> tuple[str, float]:
    """Process one lead. Returns (outcome, cost_usd) where outcome is
    'confirmed', 'downgraded', 'error', or 'no_evaluation'.

    On 529/overloaded, sleeps with exponential backoff and retries the same
    lead up to `retry_on_overloaded` times before returning 'error'."""
    company, title, key, jd_text = lead["company"], lead["title"], lead["normalized_key"], lead["jd_text"]
    sibling_titles = tuple(get_sibling_titles(conn, company, exclude_title=title))

    print(f"[{index}/{total}] {company} / {title} ...", flush=True)

    attempt = 0
    while True:
        attempt += 1
        # attempt 1 = initial call (no backoff yet); attempt 2+ = after a 529 sleep.
        # sleep for retry N uses pause * 2^(N-1) where N is the failing attempt number.
        if attempt > 1:
            applied_backoff_s = pause_seconds * (2 ** (attempt - 2))
            next_backoff_s = pause_seconds * (2 ** (attempt - 1))
            print(
                f"    [529 retry {attempt - 1}/{retry_on_overloaded}] "
                f"backoff sleep was {applied_backoff_s:g}s; "
                f"calling LLM now"
                + (
                    f" (next backoff if 529 again: {next_backoff_s:g}s)"
                    if attempt <= retry_on_overloaded
                    else ""
                )
                + "...",
                flush=True,
            )
        try:
            result = generate_two_tier_package(
                jd_text,
                company=company,
                title=title,
                model=model,
                output_root=output_root,
                force_llm_review=True,
                multi_lead=len(sibling_titles) > 0,
                sibling_titles=sibling_titles,
            )
            break
        except Exception as exc:  # noqa: BLE001 - classify overloaded vs fatal
            if _is_overloaded_error(exc) and attempt <= retry_on_overloaded:
                sleep_s = pause_seconds * (2 ** (attempt - 1))
                print(
                    f"    OVERLOADED (attempt {attempt}/{retry_on_overloaded}): {exc}",
                    flush=True,
                )
                print(
                    f"    Sleeping {sleep_s:g}s before next LLM call "
                    f"(exponential backoff from {pause_seconds:g}s)...",
                    flush=True,
                )
                time.sleep(sleep_s)
                continue
            print(f"    ERROR: {exc}", flush=True)
            if _is_overloaded_error(exc):
                print(
                    f"    Exhausted {retry_on_overloaded} overloaded retry(ies); aborting.",
                    flush=True,
                )
            return "error", 0.0

    if result.evaluation is None:
        print("    -> no evaluation produced (unexpected)", flush=True)
        return "no_evaluation", 0.0

    update_llm_evaluation(conn, key, result.evaluation)
    cost = result.total_cost_usd or 0.0

    if result.evaluation.verdict == "pursue":
        suffix = " ; résumé + cover letter generated" if result.resume_path else ""
        print(
            f"    -> pursue confirmed (match {result.evaluation.match_pct:.0f}%)"
            f"{suffix}  (lead ~${cost:.4f})",
            flush=True,
        )
        conn.commit()
        return "confirmed", cost

    conn.execute(
        "UPDATE job_leads SET verdict = ? WHERE normalized_key = ?",
        (result.evaluation.verdict, key),
    )
    print(
        f"    -> DOWNGRADED to '{result.evaluation.verdict}' (match {result.evaluation.match_pct:.0f}%); "
        f"DB verdict updated  (lead ~${cost:.4f})",
        flush=True,
    )
    conn.commit()
    return "downgraded", cost


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    ap.add_argument("--limit", type=int, default=None, help="Process only the first N leads (for testing)")
    ap.add_argument("--dry-run", action="store_true", help="List leads that would be processed, make no LLM calls")
    ap.add_argument(
        "--loop",
        action="store_true",
        help=(
            f"Process {_LOOP_BATCH_SIZE} leads at a time with a pause between batches; "
            "continue while more than 1 lead remains, then finish the last lead"
        ),
    )
    ap.add_argument(
        "--pause-seconds",
        type=float,
        default=60.0,
        help=(
            "Base seconds for --loop batch pauses and for 529 exponential backoff "
            "(sleep = pause-seconds * 2^(attempt-1); default: 60)"
        ),
    )
    ap.add_argument(
        "--retry-on-overloaded",
        type=int,
        default=_DEFAULT_OVERLOADED_RETRIES,
        metavar="N",
        help=(
            f"On Anthropic 529/overloaded, retry the same lead up to N times with "
            f"exponential backoff (default: {_DEFAULT_OVERLOADED_RETRIES}; 0 = no retries)"
        ),
    )
    args = ap.parse_args(argv)
    if args.retry_on_overloaded < 0:
        ap.error("--retry-on-overloaded must be >= 0")

    conn = connect(args.db)
    leads = _fetch_pursue_leads(conn, limit=args.limit)
    total = len(leads)

    print(f"{total} pursue leads with stored JD text to process.")
    if args.dry_run:
        if args.loop:
            for batch_start in range(0, total, _LOOP_BATCH_SIZE):
                batch = leads[batch_start : batch_start + _LOOP_BATCH_SIZE]
                print(f"  batch {batch_start // _LOOP_BATCH_SIZE + 1}:")
                for lead in batch:
                    print(f"    - {lead['company']} / {lead['title']}")
                remaining_after = total - (batch_start + len(batch))
                if remaining_after > 1:
                    print(f"    (would pause {args.pause_seconds:g}s before next batch)")
        else:
            for lead in leads:
                print(f"  - {lead['company']} / {lead['title']}")
        conn.close()
        return 0

    confirmed = 0
    downgraded = 0
    errors = 0
    total_cost = 0.0
    processed = 0
    aborted = False

    def _tally(outcome: str, cost: float) -> bool:
        """Update counters. Returns False when the caller should abort the run."""
        nonlocal confirmed, downgraded, errors, total_cost, processed
        processed += 1
        total_cost += cost
        if outcome == "confirmed":
            confirmed += 1
        elif outcome == "downgraded":
            downgraded += 1
        elif outcome == "error":
            errors += 1
            return False
        return True

    def _run_one(lead: dict) -> bool:
        outcome, cost = _process_lead(
            conn,
            lead,
            model=args.model,
            output_root=args.output_root,
            index=processed + 1,
            total=total,
            pause_seconds=args.pause_seconds,
            retry_on_overloaded=args.retry_on_overloaded,
        )
        return _tally(outcome, cost)

    if args.loop:
        # Drain in batches of 3 while more than 1 remain; pause between batches.
        # After the loop, process the final leftover lead (if any) so nothing
        # is stranded by the "> 1" continue condition.
        queue = list(leads)
        batch_num = 0
        while len(queue) > 1:
            batch_num += 1
            batch = queue[:_LOOP_BATCH_SIZE]
            queue = queue[_LOOP_BATCH_SIZE:]
            print(
                f"\n--- batch {batch_num}: {len(batch)} lead(s), {len(queue)} remaining after ---",
                flush=True,
            )
            for lead in batch:
                if not _run_one(lead):
                    aborted = True
                    print(
                        "Aborting on error — remaining leads in this batch and later batches left unprocessed.",
                        flush=True,
                    )
                    break
            if aborted:
                break
            if len(queue) > 1:
                print(f"Pausing {args.pause_seconds:g}s before next batch...", flush=True)
                time.sleep(args.pause_seconds)
        if queue and not aborted:
            print(f"\n--- final lead ({len(queue)} remaining) ---", flush=True)
            for lead in queue:
                if not _run_one(lead):
                    aborted = True
                    print("Aborting on error.", flush=True)
                    break
    else:
        for lead in leads:
            if not _run_one(lead):
                aborted = True
                print("Aborting on error — remaining leads left unprocessed.", flush=True)
                break

    conn.close()
    print()
    status = "Aborted" if aborted else "Done"
    print(
        f"{status}. confirmed pursue: {confirmed}  downgraded: {downgraded}  errors: {errors}  "
        f"total LLM cost: ${total_cost:.2f}"
    )
    return 1 if aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
