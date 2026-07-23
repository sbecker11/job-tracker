#!/usr/bin/env python3
"""CLI: generate a weekly job-search "contact attempts" report for
unemployment-claim filing (company, title, date, recruiter email, status).

Reusable version of the ad-hoc query built by hand on 2026-07-23 for the
Jul 5 / Jul 12 / Jul 19 weekly claims (see chat history) — same "genuine
contact attempt" definition: an application, follow-up, interview, offer,
or acceptance stamped on the lead, OR anything you personally sent/logged
(an outbound conversation, or a non-email channel like a call), NOT a
passive inbound job-alert digest. One row per job lead, using its earliest
qualifying date within the week.

Two things the ad-hoc version didn't have, both requested afterward:

1. A persistent cross-week registry (--state-file, default
   <output-dir>/.reported_job_keys.json) so the same company+title is never
   reported on more than one weekly claim, even if it has qualifying
   activity again in a later week (e.g. a second-round interview). Run this
   once a week and it remembers what's already been used; pass
   --force-include NORMALIZED_KEY to deliberately report one again anyway.
2. A `notes` column flagging system/no-reply "recruiter emails" (job-alert
   bots, ATS confirmation senders) and malformed email values (e.g. a
   LinkedIn profile URL stored where an email should be) for your own
   review before filing — never silently dropped or edited, just flagged.

Usage (run weekly, once you know that week's activity is fully logged):

    python scripts/unemployment_claim_report.py
    python scripts/unemployment_claim_report.py --week-start 2026-07-26
    python scripts/unemployment_claim_report.py --week-start 2026-07-26 --weeks 3
    python scripts/unemployment_claim_report.py --dry-run   # preview only
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402

# Shawn's filing timezone (Lehi, UT) — used only to pick a sensible default
# --week-start when none is given; an explicit --week-start is taken as-is.
LOCAL_TZ = ZoneInfo("America/Denver")

DEFAULT_OUTPUT_DIR = Path.home() / "Desktop" / "Unemployment_Claims"
DEFAULT_STATE_FILENAME = ".reported_job_keys.json"
DEFAULT_MIN_CONTACTS = 4

# job_leads timestamp columns that count as a "you did something" contact
# attempt, in addition to any outbound/non-email job_conversations row.
STAGE_DATE_COLUMNS = ["applied_at", "following_up_at", "interviewing_at", "offered_at", "accepted_at"]

# Local-part patterns for known bulk/automated senders (job-alert bots, ATS
# confirmation systems) — legitimate "how the lead was sourced" addresses,
# but not a human recruiter's own contact info.
_SYSTEM_SENDER_LOCAL_PART_RE = re.compile(
    r"no-?reply|jobalerts?|notifications?|digest|^alerts?$|^support$|^jobs$|^jobs-noreply$|^updates$",
    re.IGNORECASE,
)

# Known job-board/ATS domains that only ever send bulk/automated mail, even
# when the local part alone wouldn't trip the regex above.
_SYSTEM_SENDER_DOMAINS = frozenset(
    {
        "linkedin.com",
        "theladders.com",
        "my.theladders.com",
        "builtin.com",
        "jobcase.com",
        "pmail.jobcase.com",
        "jobs2web.com",
        "indeed.com",
        "ziprecruiter.com",
        "dice.com",
        "monster.com",
        "ashbyhq.com",
        "greenhouse-mail.io",
        "us.greenhouse-mail.io",
    }
)


def is_system_sender(email: str) -> bool:
    """Best-effort flag for the `notes` column, not authoritative — see
    module docstring point 2. Real recruiters occasionally use plain-looking
    addresses too; this only catches the common bulk-sender patterns seen
    live in this DB."""
    if not email or "@" not in email:
        return False
    local, _, domain = email.lower().partition("@")
    if _SYSTEM_SENDER_LOCAL_PART_RE.search(local):
        return True
    return any(domain == d or domain.endswith(f".{d}") for d in _SYSTEM_SENDER_DOMAINS)


def is_malformed_email(value: str) -> bool:
    """Flags obviously-not-an-email values seen in the wild in job_contacts
    (e.g. a LinkedIn profile URL stored where an email should be)."""
    if not value:
        return False
    return "@" not in value or "linkedin.com/in/" in value.lower()


def note_for(email: str) -> str:
    if is_malformed_email(email):
        return "malformed email value \u2014 verify before filing"
    if is_system_sender(email):
        return "system/no-reply address, not a named human contact"
    return ""


def floor_to_sunday(d: date) -> date:
    """Sunday on or before `d` (Python's Monday=0..Sunday=6 weekday scheme
    means Sunday itself needs 0 days subtracted, Monday needs 1, ... ,
    Saturday needs 6 — the `(weekday + 1) % 7` trick gets all seven cases
    right without a special case for Sunday)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def default_week_start(today: date | None = None) -> date:
    today = today or datetime.now(LOCAL_TZ).date()
    return floor_to_sunday(today)


def week_bounds(week_start: date) -> tuple[str, str]:
    """Inclusive Sun..Sat date-string bounds for one week."""
    end = week_start + timedelta(days=6)
    return week_start.isoformat(), end.isoformat()


def best_contact_email(conn, job_key: str) -> str:
    row = conn.execute(
        """
        SELECT email FROM job_contacts WHERE job_key = ? AND email IS NOT NULL AND email != ''
        ORDER BY CASE WHEN role = 'recruiter' THEN 0 ELSE 1 END, first_contacted_at ASC LIMIT 1
        """,
        (job_key,),
    ).fetchone()
    return row["email"] if row else ""


def earliest_qualifying_date(conn, lead_row, job_key: str, start: str, end: str) -> str | None:
    candidates: list[str] = []
    for col in STAGE_DATE_COLUMNS:
        value = lead_row[col]
        if value:
            d = value[:10]
            if start <= d <= end:
                candidates.append(d)
    conv_rows = conn.execute(
        """
        SELECT date(occurred_at) AS d FROM job_conversations
        WHERE job_key = ? AND (direction = 'outbound' OR channel != 'email')
          AND date(occurred_at) BETWEEN ? AND ?
        """,
        (job_key, start, end),
    ).fetchall()
    candidates.extend(r["d"] for r in conv_rows if r["d"])
    return min(candidates) if candidates else None


def build_week_rows(conn, start: str, end: str, *, excluded_job_keys: set[str] | None = None) -> list[dict]:
    """One row per job lead with >=1 genuine contact-attempt date inside
    [start, end] (inclusive, both YYYY-MM-DD), skipping any job_key already
    in `excluded_job_keys` (leads already reported on a prior week's
    claim)."""
    excluded_job_keys = excluded_job_keys or set()

    per_column_clause = " OR ".join(
        f"({col} IS NOT NULL AND date({col}) BETWEEN ? AND ?)" for col in STAGE_DATE_COLUMNS
    )
    params: list[str] = []
    for _ in STAGE_DATE_COLUMNS:
        params.extend([start, end])
    params.extend([start, end])

    rows = conn.execute(
        f"""
        SELECT normalized_key, company, title, status, {", ".join(STAGE_DATE_COLUMNS)}
        FROM job_leads
        WHERE ({per_column_clause})
           OR normalized_key IN (
              SELECT job_key FROM job_conversations
              WHERE (direction = 'outbound' OR channel != 'email')
                AND date(occurred_at) BETWEEN ? AND ?
           )
        """,
        params,
    ).fetchall()

    out = []
    for r in rows:
        job_key = r["normalized_key"]
        if job_key in excluded_job_keys:
            continue
        d = earliest_qualifying_date(conn, r, job_key, start, end)
        if d is None:
            continue
        email = best_contact_email(conn, job_key)
        out.append(
            {
                "job_key": job_key,
                "company": r["company"],
                "title": r["title"],
                "date_of_communication": d,
                "recruiter_email": email,
                "job_status": r["status"],
                "notes": note_for(email),
            }
        )
    out.sort(key=lambda row: row["date_of_communication"])
    return out


def load_registry(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_registry(path: Path, registry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, sort_keys=True))


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["company", "title", "date_of_communication", "recruiter_email", "job_status", "notes"])
        for row in rows:
            writer.writerow(
                [
                    row["company"],
                    row["title"],
                    row["date_of_communication"],
                    row["recruiter_email"],
                    row["job_status"],
                    row["notes"],
                ]
            )


def print_table(week_start: str, week_end: str, rows: list[dict], *, min_contacts: int) -> None:
    print(f"=== Week of {week_start} \u2013 {week_end} ({len(rows)} contact attempt(s)) ===")
    if not rows:
        print("  (none)")
    for row in rows:
        flag = f"  \u26a0\ufe0f {row['notes']}" if row["notes"] else ""
        print(
            f"  {row['date_of_communication']} | {row['company']} | {row['title']} | "
            f"{row['recruiter_email'] or '(no contact on file)'} | {row['job_status']}{flag}"
        )
    if len(rows) < min_contacts:
        print(
            f"  WARNING: only {len(rows)} contact attempt(s) this week, below the "
            f"--min-contacts threshold of {min_contacts}."
        )
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument(
        "--week-start",
        type=str,
        default=None,
        help="Sunday to start from (YYYY-MM-DD). Defaults to the current week (today's most recent Sunday, "
        "America/Denver). Any date given is floored back to its own week's Sunday.",
    )
    ap.add_argument(
        "--weeks", type=int, default=1, help="Number of consecutive weeks to generate, starting at --week-start."
    )
    ap.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    ap.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="Registry of already-reported job_keys, so each company+title lands on only one weekly claim "
        f"ever. Defaults to <output-dir>/{DEFAULT_STATE_FILENAME}.",
    )
    ap.add_argument("--min-contacts", type=int, default=DEFAULT_MIN_CONTACTS)
    ap.add_argument(
        "--force-include",
        action="append",
        default=[],
        metavar="NORMALIZED_KEY",
        help="Allow this job_key to be reported again even if already in the state file. Repeatable.",
    )
    ap.add_argument("--dry-run", action="store_true", help="Print only; don't write CSVs or update the state file.")
    args = ap.parse_args(argv)

    output_dir: Path = args.output_dir
    state_path: Path = args.state_file or (output_dir / DEFAULT_STATE_FILENAME)

    if args.week_start:
        start_date = floor_to_sunday(date.fromisoformat(args.week_start))
    else:
        start_date = default_week_start()

    conn = connect(args.db)
    try:
        for i in range(args.weeks):
            week_start_date = start_date + timedelta(weeks=i)
            start, end = week_bounds(week_start_date)

            registry = load_registry(state_path)
            excluded = set(registry.keys()) - set(args.force_include)

            rows = build_week_rows(conn, start, end, excluded_job_keys=excluded)
            print_table(start, end, rows, min_contacts=args.min_contacts)

            if args.dry_run:
                continue

            csv_path = output_dir / f"Weekly_Claim_ContactAttempts_{start}.csv"
            write_csv(csv_path, rows)
            print(f"  Wrote {csv_path}")

            for row in rows:
                registry[row["job_key"]] = {
                    "company": row["company"],
                    "title": row["title"],
                    "week_start": start,
                    "date_used": row["date_of_communication"],
                }
            save_registry(state_path, registry)
            print(f"  Updated {state_path} ({len(rows)} newly registered)")
            print()
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
