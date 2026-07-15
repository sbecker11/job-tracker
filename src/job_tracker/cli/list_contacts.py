"""CLI: list every tracked contact (recruiter, hiring manager, referral)
across all jobs — name, company, role, phone, email — without re-running
the pipeline.
"""

from __future__ import annotations

import csv
import json
from argparse import ArgumentParser
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, list_all_contacts

_COLUMNS = ["name", "job_company", "job_title", "role", "phone", "email", "first_contacted_at", "last_contacted_at"]


def main(argv: list[str] | None = None) -> int:
    ap = ArgumentParser(description="List tracked contacts (name, company, role, phone, email).")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--company", help="Filter to companies containing this text (case-insensitive)")
    ap.add_argument("--csv", type=Path, help="Write results to this CSV path instead of printing")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    try:
        rows = [dict(r) for r in list_all_contacts(conn, company=args.company)]
    finally:
        conn.close()

    if not rows:
        print("No matching contacts.")
        return 0

    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_COLUMNS)
            writer.writeheader()
            for r in rows:
                writer.writerow({k: r.get(k) for k in _COLUMNS})
        print(f"Wrote {len(rows)} contact(s) to {args.csv}")
        return 0

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    header = f"{'NAME':<24} {'COMPANY':<22} {'TITLE':<32} {'ROLE':<14} {'PHONE':<16} {'EMAIL':<30}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{(r['name'] or '')[:24]:<24} {r['job_company'][:22]:<22} {r['job_title'][:32]:<32} "
            f"{r['role']:<14} {(r['phone'] or '')[:16]:<16} {(r['email'] or '')[:30]:<30}"
        )
    print(f"\n{len(rows)} contact(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
