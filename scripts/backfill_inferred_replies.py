#!/usr/bin/env python3
"""Backfill REQUIREMENTS TURN-6 on mail logged before the rule existed.

For each LinkedIn "Message replied" notice with no outbound logged since the
recruiter's previous message, add the inferred "You replied on LinkedIn" row
one minute before it. Dry run by default; `--apply` writes.

  .venv/bin/python scripts/with_db_lock.py --lock var/comms_fast.lock --wait-seconds 180 -- \\
      .venv/bin/python scripts/backfill_inferred_replies.py [--apply]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from job_tracker.pipeline.store import connect, inferred_reply_due, insert_inferred_reply, _parse_iso  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=ROOT / "var" / "leads.db")
    ap.add_argument("--apply", action="store_true", help="write the rows (default: dry run)")
    args = ap.parse_args(argv)

    conn = connect(args.db)
    notices = conn.execute(
        """
        SELECT jc.id, jc.job_key, jc.contact_id, jc.message_id, jc.summary, jc.occurred_at,
               jl.company, jl.title, jl.status
        FROM job_conversations jc JOIN job_leads jl ON jl.normalized_key = jc.job_key
        WHERE jc.direction = 'inbound' AND jc.summary LIKE 'Message replied%'
        """
    ).fetchall()
    notices = sorted(notices, key=lambda r: (_parse_iso((r["occurred_at"] or "").replace("Z", "+00:00")) is None,
                                             _parse_iso((r["occurred_at"] or "").replace("Z", "+00:00")) or 0))
    added = 0
    for n in notices:
        when = inferred_reply_due(conn, n["job_key"], n["summary"], n["occurred_at"], exclude_id=n["id"])
        if when is None:
            continue
        added += 1
        print(f"{n['occurred_at'][:16]}  [{n['status']}] {n['company']} | {n['title']}  <- {n['summary'][:60]}")
        if args.apply:
            insert_inferred_reply(
                conn, job_key=n["job_key"], contact_id=n["contact_id"], message_id=n["message_id"], notice_at=when
            )
    if args.apply:
        conn.commit()
    print(f"\n{added} inferred repl{'y' if added == 1 else 'ies'} {'added' if args.apply else 'would be added (dry run)'} "
          f"of {len(notices)} 'Message replied' notices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
