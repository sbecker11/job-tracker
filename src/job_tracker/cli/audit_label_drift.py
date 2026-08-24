"""Report Gmail JobTracker/* label drift vs current job_leads verdicts.

Dry-run by default (no Gmail writes). Use resync_labels.py --dry-run for the
same check with apply available via resync_labels.py without --dry-run.

  python scripts/audit_label_drift.py
  python scripts/audit_label_drift.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from job_tracker.email import gmail_writer
from job_tracker.email.gmail_reader import (
    KNOWN_ACCOUNTS,
    default_credentials_path,
    default_token_path,
    get_gmail_service,
    list_message_ids,
)
from job_tracker.pipeline.label_drift import compute_label_drift
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect
from job_tracker.pipeline.triage import NEEDS_REVIEW, PURSUE, SKIP

_OUTCOME_LABEL_NAMES = {
    PURSUE: gmail_writer.PURSUE_LABEL,
    SKIP: gmail_writer.SKIP_LABEL,
    NEEDS_REVIEW: gmail_writer.NEEDS_REVIEW_LABEL,
}


def _current_label_map(service) -> dict[str, str]:
    current: dict[str, str] = {}
    for outcome, label_name in _OUTCOME_LABEL_NAMES.items():
        for message_id in list_message_ids(service, query=f"label:{label_name}"):
            current[message_id] = outcome
    return current


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--account", choices=KNOWN_ACCOUNTS, default=None)
    ap.add_argument("--credentials", type=Path, default=None)
    ap.add_argument("--token", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=20, help="Max drift rows to print in text mode")
    args = ap.parse_args(argv)

    credentials_path = args.credentials or default_credentials_path(args.account)
    token_path = args.token or default_token_path(args.account)
    service = get_gmail_service(credentials_path, token_path, account=args.account)

    conn = connect(args.db)
    try:
        processed = conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]
        if not processed:
            print("No triaged messages in DB.", file=sys.stderr)
            return 0

        current_labels = _current_label_map(service)
        entries, checked, skipped_imap = compute_label_drift(conn, current_labels)

        payload = {
            "checked": checked,
            "wouldRelabel": len(entries),
            "skippedImap": skipped_imap,
            "drift": [
                {
                    "messageId": e.message_id,
                    "current": e.current_outcome,
                    "desired": e.desired_outcome,
                    "reason": e.reason,
                }
                for e in entries
            ],
        }

        if args.json:
            print(json.dumps(payload, indent=2))
            return 0

        print(f"Checked {checked} triaged message(s): {len(entries)} would need relabeling")
        if skipped_imap:
            print(f"  ({skipped_imap} IMAP-sourced message(s) skipped — not Gmail-labeled)")
        for e in entries[: args.limit]:
            print(f"  {e.message_id}: {e.current_outcome} -> {e.desired_outcome} ({e.reason})")
        if len(entries) > args.limit:
            print(f"  ... and {len(entries) - args.limit} more (use --json for full list)")
        if entries:
            print("\nFix: python scripts/resync_labels.py   # or --dry-run to preview")
        return 1 if entries else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
