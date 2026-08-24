"""Phase 5 — jobs gone quiet (awaiting_response older than N days).

  python scripts/quiet_jobs_report.py
  python scripts/quiet_jobs_report.py --days 10
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, list_leads


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    now = datetime.now(timezone.utc)
    conn = connect(args.db)
    try:
        quiet = []
        for r in list_leads(conn):
            d = dict(r)
            status = (d.get("status") or "").strip()
            if status not in ("applied", "following_up", "interviewing", "package_generated"):
                continue
            since = d.get("awaiting_response_since")
            if not since:
                continue
            try:
                dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError:
                continue
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = (now - dt.astimezone(timezone.utc)).days
            if age_days >= args.days:
                quiet.append(
                    {
                        "company": d.get("company"),
                        "title": d.get("title"),
                        "status": status,
                        "waitingDays": age_days,
                        "since": since,
                        "normalizedKey": d.get("normalized_key"),
                    }
                )
        quiet.sort(key=lambda x: -x["waitingDays"])
    finally:
        conn.close()

    if args.json:
        print(json.dumps({"thresholdDays": args.days, "quiet": quiet}, indent=2))
        return 0

    print(f"=== Quiet jobs (waiting ≥ {args.days} days) ===")
    if not quiet:
        print("  (none)")
        return 0
    for q in quiet:
        print(f"  • {q['company']} — {q['title']}  [{q['status']} · {q['waitingDays']}d waiting]")
    print(f"\nFollow up: python scripts/generate_message.py --kind status_check_in --company \"...\" --title \"...\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
