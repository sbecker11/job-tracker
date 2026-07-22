"""CLI: interactively decide `job_leads.direct_recruiter_outreach` for every
lead that hasn't been reviewed yet.

Deliberately NOT set by the ingestion pipeline itself (see
models.JobLead.direct_recruiter_outreach's docstring for the 2026-07-21
design decision) — a human recruiter personally pitching a role, vs. a
cold job-board digest merely listing it, is genuinely worth prioritizing
differently, but auto-detecting that reliably enough to trust unattended
turned out messier than just asking. So this walks the review queue
(`store.list_undecided_direct_recruiter_outreach`) one lead at a time and
asks — pre-filling a *suggested* default from
`email/classifier.is_personal_recruiter_message()` (the same
personal-tone-vs-thin-JD signal `classify()` uses for
`Label.RECRUITER_OUTREACH`) plus the unambiguous `source_label ==
'linkedin_message'` case (every stub lead `scan_communications.py` creates
is, by construction, from a real LinkedIn InMail/reply, never a digest) —
so accepting the suggestion for the obvious cases is just hitting Enter.

Usage:
    python -m job_tracker.cli.review_direct_recruiter_outreach [--db PATH] [--limit N]

At each prompt:
    y / yes  -> direct recruiter outreach
    n / no   -> not direct outreach
    <enter>  -> accept the suggested default shown in the prompt
    s / skip -> leave undecided, move to the next one
    q / quit -> stop reviewing (remaining leads stay undecided for next time)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.email.classifier import is_personal_recruiter_message
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    connect,
    list_undecided_direct_recruiter_outreach,
    set_direct_recruiter_outreach,
)


def suggest(*, source_label: str, jd_text: str) -> bool:
    """The prompt's pre-filled default — never written without the human
    confirming it. `source_label == 'linkedin_message'` is unconditional
    (see this module's docstring); everything else falls back to the
    personal-pitch text heuristic against whatever `jd_text` was captured."""
    if source_label == "linkedin_message":
        return True
    return is_personal_recruiter_message(jd_text or "")


def _prompt(row, *, input_func) -> str:
    default = suggest(source_label=row["source_label"] or "", jd_text=row["jd_text"] or "")
    default_label = "Y" if default else "n"
    snippet = (row["jd_text"] or "").strip().replace("\n", " ")[:200]
    print(f"\n{row['title']!r} @ {row['company']!r}  (status={row['status']}, source={row['source_label']!r})")
    if snippet:
        print(f"  jd_text: {snippet}")
    prompt = f"  Direct recruiter outreach? [y/n/s/q] (suggested: {default_label}): "
    answer = input_func(prompt).strip().lower()
    if answer == "":
        return "y" if default else "n"
    return answer


def main(argv: list[str] | None = None, *, input_func=input) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--limit", type=int, default=None, help="Review at most N leads this run")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        rows = list_undecided_direct_recruiter_outreach(conn)
        if args.limit is not None:
            rows = rows[: args.limit]
        if not rows:
            print("Nothing to review — every lead already has a direct_recruiter_outreach decision.")
            return 0

        print(f"{len(rows)} lead(s) awaiting a direct_recruiter_outreach decision.")
        decided = 0
        for i, row in enumerate(rows, 1):
            print(f"\n[{i}/{len(rows)}]", end="")
            answer = _prompt(row, input_func=input_func)
            if answer in ("y", "yes"):
                set_direct_recruiter_outreach(conn, row["normalized_key"], True)
                decided += 1
            elif answer in ("n", "no"):
                set_direct_recruiter_outreach(conn, row["normalized_key"], False)
                decided += 1
            elif answer in ("q", "quit"):
                print("Stopping — remaining leads stay undecided.")
                break
            # else: s/skip, or anything unrecognized — leave undecided, move on.

        print(f"\nDecided: {decided}. Left undecided: {len(rows) - decided}.")
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
