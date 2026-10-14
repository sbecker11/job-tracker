"""CLI: collapse the unmatched-communications queue to one row per
conversation, and attach conversations that already belong to a lead.

Copies come from the Gmail-API and IMAP readers ingesting the same LinkedIn
notification under different ids, plus a new notification for every reply
in a LinkedIn conversation. `store.record_unmatched_message` keeps new
arrivals collapsed (2026-10-14); this command applies the same rule to rows
parked before that, and is safe to re-run.

Per conversation (same LinkedIn conversation link, or same sender + subject
within a few hours):
  1. the newest open row stays; older open rows are marked superseded by it;
  2. if the LinkedIn conversation is already on exactly one live lead, the
     newest row is resolved onto that lead;
  3. with --dismiss-quiet-days N, a conversation whose newest row is older
     than N days is dismissed.

Use --dry-run to see the counts without writing anything.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    _parse_iso,
    _same_conversation,
    connect,
    dismiss_unmatched_message,
    find_job_by_linkedin_thread,
    linkedin_thread_key,
    list_unmatched_messages,
    resolve_unmatched_message,
    supersede_unmatched_rows,
)


def group_conversations(rows: list) -> list[list]:
    """Partition rows into conversations, each sorted oldest → newest."""
    parent = list(range(len(rows)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if _same_conversation(rows[i], rows[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list] = {}
    for i, row in enumerate(rows):
        groups.setdefault(find(i), []).append(row)
    return [sorted(g, key=lambda r: r["detected_at"] or "") for g in groups.values()]


def dedupe_unmatched(
    conn,
    *,
    dismiss_quiet_days: int | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    rows = list_unmatched_messages(conn)
    groups = group_conversations(rows)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=dismiss_quiet_days or 0)
    counts = {"open_before": len(rows), "conversations": len(groups), "superseded": 0, "resolved": 0, "dismissed": 0}

    for group in groups:
        newest = group[-1]
        older = [r["message_id"] for r in group[:-1]]
        counts["superseded"] += len(older)
        if not dry_run:
            supersede_unmatched_rows(conn, older, by=newest["message_id"])

        job_key = find_job_by_linkedin_thread(conn, linkedin_thread_key(newest["body_text"]))
        if job_key:
            counts["resolved"] += 1
            if not dry_run:
                resolve_unmatched_message(conn, newest["message_id"], job_key)
            continue

        detected = _parse_iso(newest["detected_at"])
        if dismiss_quiet_days is not None and detected is not None and detected < cutoff:
            counts["dismissed"] += 1
            if not dry_run:
                dismiss_unmatched_message(conn, newest["message_id"])

    counts["open_after"] = counts["conversations"] - counts["resolved"] - counts["dismissed"]
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument(
        "--dismiss-quiet-days",
        type=int,
        default=None,
        help="Dismiss conversations whose newest message is older than this many days",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report counts without writing")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        c = dedupe_unmatched(conn, dismiss_quiet_days=args.dismiss_quiet_days, dry_run=args.dry_run)
    finally:
        conn.close()

    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}{c['open_before']} open unmatched row(s) → {c['conversations']} conversation(s): "
        f"{c['superseded']} older copies superseded, {c['resolved']} attached to an existing lead, "
        f"{c['dismissed']} dismissed as quiet; {c['open_after']} left to review."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
