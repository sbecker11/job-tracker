"""CLI: soft-delete, mark unavailable/hired, or hard-purge a job lead.

Soft-delete (default) sets `status='deleted'` (junk/duplicate you removed).
`--unavailable` sets `status='unavailable'` (req closed/filled/withdrawn).
`--already-hired` sets `status='hired'` (you took another offer, or this req
already hired someone else — distinct from `accepted`/`started` on *this*
lead's offer).
All three hide the lead from default `list_leads` / pending-actions views but
keep the row and CRM history. Hard-purge (`--purge`) permanently removes the
lead and all related contacts/conversations/documents/meetings/offers.

Usage:
    python scripts/delete_lead.py --company "Acme" --title "Software Engineer"
    python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --reason "duplicate"
    python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --unavailable
    python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --already-hired
    python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --purge --yes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    connect,
    find_similar_jobs,
    get_job,
    mark_lead_deleted,
    mark_lead_hired,
    mark_lead_unavailable,
    purge_lead,
)


def _resolve_job(conn, company: str, title: str):
    job = get_job(conn, company, title)
    if job is not None:
        return job
    candidates = find_similar_jobs(conn, company, title)
    print(f"No job found for {title!r} @ {company!r}.", file=sys.stderr)
    if candidates:
        print("Did you mean one of these (use the exact company/title shown)?", file=sys.stderr)
        for m in candidates[:5]:
            print(f"  {m.title} @ {m.company}  (score={m.combined_score:.2f})", file=sys.stderr)
    else:
        print("Nothing close in leads.db — check spelling with scripts/list_leads.py.", file=sys.stderr)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--company", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument(
        "--reason",
        default="",
        help="Optional note stored as a conversation summary (ignored with --purge). "
        "Defaults to 'no longer available' / 'already hired' for those flags.",
    )
    ap.add_argument(
        "--on",
        help="ISO date/timestamp for deleted_at / unavailable_at / hired_at (default: now)",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--unavailable",
        action="store_true",
        help="Mark req closed/filled/withdrawn (status='unavailable')",
    )
    mode.add_argument(
        "--already-hired",
        action="store_true",
        help="Mark already hired (status='hired') — you took another offer, or this req hired someone else",
    )
    mode.add_argument(
        "--purge",
        action="store_true",
        help="Hard-delete the lead row and all CRM children (irreversible)",
    )
    ap.add_argument(
        "--yes",
        action="store_true",
        help="Required with --purge to confirm irreversible hard-delete",
    )
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    if args.purge and not args.yes:
        print("--purge requires --yes to confirm irreversible hard-delete.", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        job = _resolve_job(conn, args.company, args.title)
        if job is None:
            return 1
        key = job["normalized_key"]
        company, title, status = job["company"], job["title"], job["status"]

        if args.purge:
            counts = purge_lead(conn, key)
            print(
                f"Purged {title} @ {company} "
                f"(leads={counts['leads']}, contacts={counts['contacts']}, "
                f"conversations={counts['conversations']}, documents={counts['documents']}, "
                f"meetings={counts['meetings']}, offers={counts['offers']})"
            )
            return 0

        if args.unavailable:
            if status == "unavailable":
                print(f"Already unavailable: {title} @ {company}")
                return 0
            mark_lead_unavailable(conn, key, when=args.on, reason=args.reason)
            note = f" ({args.reason or 'no longer available'})"
            print(f"Marked unavailable: {title} @ {company}{note}")
            print("Restore later with: list_leads.py --company ... --title ... --set-status <stage>")
            return 0

        if args.already_hired:
            if status == "hired":
                print(f"Already marked hired: {title} @ {company}")
                return 0
            mark_lead_hired(conn, key, when=args.on, reason=args.reason)
            note = f" ({args.reason or 'already hired'})"
            print(f"Marked hired: {title} @ {company}{note}")
            print("Restore later with: list_leads.py --company ... --title ... --set-status <stage>")
            return 0

        if status == "deleted":
            print(f"Already deleted: {title} @ {company}")
            return 0

        mark_lead_deleted(conn, key, when=args.on, reason=args.reason)
        note = f" ({args.reason})" if args.reason else ""
        print(f"Marked deleted: {title} @ {company}{note}")
        print("Restore later with: list_leads.py --company ... --title ... --set-status <stage>")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
