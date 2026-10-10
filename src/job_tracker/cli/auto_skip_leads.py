"""CLI: auto-skip active leads that have gone quiet or are restricted to Eastern time.

Two standing rules from ~/CLAUDE.md §3 (added 2026-10-09), applied each
hourly cycle and safe to re-run:

1. **Stale** — a `new` or `pursued` lead whose last communication was more
   than 21 days ago. Last communication is the latest of: the newest
   logged conversation (inbound or outbound), `awaiting_response_since`,
   and `last_seen` (the most recent email carrying the lead — the only
   signal for digest leads with no conversation rows).
2. **Eastern time** — a `new` or `pursued` lead whose JD hits the
   `eastern_time_required` dealbreaker in config/framework.yaml (remote,
   but must live in / work Eastern time). Re-swept here because leads
   scored before the rule existed never saw it.

Every skip appends a reason line to `job_leads.notes`.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_tracker.pipeline.store import DEFAULT_DB_PATH, advance_status, append_lead_note, connect
from job_tracker.scoring.scorer import DEFAULT_FRAMEWORK_PATH, score_jd

STALE_DAYS = 21
ACTIVE_STATUSES = ("new", "pursued")
EASTERN_DEALBREAKER_ID = "eastern_time_required"


def find_stale(conn, *, stale_days: int = STALE_DAYS, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=stale_days)).isoformat()
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    rows = conn.execute(
        f"""
        SELECT normalized_key, company, title, status, last_communication FROM (
          SELECT l.normalized_key, l.company, l.title, l.status,
                 MAX(
                   julianday(COALESCE(l.last_seen, l.first_seen)),
                   COALESCE(julianday(l.awaiting_response_since), 0),
                   COALESCE((SELECT MAX(julianday(c.occurred_at)) FROM job_conversations c
                             WHERE c.job_key = l.normalized_key), 0)
                 ) AS last_communication
          FROM job_leads l
          WHERE l.status IN ({placeholders})
        )
        WHERE last_communication < julianday(?)
        ORDER BY last_communication
        """,
        (*ACTIVE_STATUSES, cutoff),
    ).fetchall()
    return [dict(r) for r in rows]


def find_eastern_time(conn, *, framework_path: Path = DEFAULT_FRAMEWORK_PATH) -> list[dict]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    rows = conn.execute(
        f"""
        SELECT normalized_key, company, title, status, jd_text
        FROM job_leads
        WHERE status IN ({placeholders}) AND COALESCE(jd_text, '') != ''
        """,
        ACTIVE_STATUSES,
    ).fetchall()
    hits = []
    for r in rows:
        score = score_jd(r["jd_text"], framework_path=framework_path)
        if any(h.id == EASTERN_DEALBREAKER_ID and h.load_bearing for h in score.dealbreaker_hits):
            hits.append({k: r[k] for k in ("normalized_key", "company", "title", "status")})
    return hits


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auto-skip quiet (>21 days) and Eastern-time-only leads.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    ap.add_argument("--framework", type=Path, default=DEFAULT_FRAMEWORK_PATH)
    ap.add_argument("--stale-days", type=int, default=STALE_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="List what would be skipped without changing anything")
    args = ap.parse_args(argv)

    if not Path(args.db).exists():
        print(f"No leads DB found at {args.db}.", file=sys.stderr)
        return 1

    conn = connect(args.db)
    eastern = find_eastern_time(conn, framework_path=args.framework)
    eastern_keys = {r["normalized_key"] for r in eastern}
    stale = [r for r in find_stale(conn, stale_days=args.stale_days) if r["normalized_key"] not in eastern_keys]

    plan = [(r, "remote but restricted to Eastern time zone / hours (CLAUDE.md §3)") for r in eastern]
    plan += [(r, f"stale — no communication in more than {args.stale_days} days (CLAUDE.md §3)") for r in stale]

    for r, reason in plan:
        print(f"{'would skip' if args.dry_run else 'skipped'}: [{r['status']}] {r['company']} — {r['title']} ({reason})")
        if not args.dry_run:
            prior = r["status"]
            advance_status(conn, r["normalized_key"], "skipped", force=True)
            append_lead_note(conn, r["normalized_key"], f"Auto-skipped {prior} -> skipped: {reason}")

    print(
        f"{'Dry run: ' if args.dry_run else ''}{len(eastern)} Eastern-time, {len(stale)} stale "
        f"lead(s) {'would be ' if args.dry_run else ''}skipped."
    )
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
