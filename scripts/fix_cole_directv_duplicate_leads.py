#!/usr/bin/env python3
"""One-off fix (2026-07-22): the DIRECTV / Cole Keener recruiter thread got
split across THREE separate `job_leads` rows instead of one, because
`triage_recruiter_inbox.py`'s fresh classify/extract/LLM-score pipeline ran
on plain-text replies before the existing-lead short-circuit (see that
file's "Existing-lead short-circuit" docstring section) existed. Two of the
three rows are pure extraction garbage — sentence fragments from a reply
mistaken for a job title:

  - directv::senior data engineer                                    (real, correct lead)
  - directv::first 30 min video with hiring manager                  (garbage)
  - directv::this role is not just a traditional data engineer he...  (garbage)

Both garbage rows were fanned out of ONE real message (`19f8602fda591978`,
"Remote Senior Data Engineer Description and Details - DIRECTV") that was
ALSO already correctly logged as a conversation on the real lead — so
nothing usable is lost by removing them, just duplicate/wrong rows. A
second real message (`19f863478395a204`) is already correctly attached to
the real lead but still carries a stray `JobTracker/NEEDS_REVIEW` Gmail
label from the same pre-fix triage run.

What this does:
  1. Backs up `var/leads.db` before touching anything (only when --live).
  2. Folds the two garbage leads' contacts/conversations into the real
     lead — dropping conversations that are exact duplicates (same
     message_id already on the real lead) rather than re-keying them into
     new duplicates — then hard-deletes the two garbage `job_leads` rows.
  3. Consolidates the resulting duplicate "Cole" `job_contacts` rows on the
     real lead into one survivor (whichever has the most complete
     name/email/phone), re-pointing any `job_conversations.contact_id`
     references first.
  4. Drops both messages' stale `processed_messages` rows (a Linked message
     isn't tracked there — see `triage_recruiter_inbox.py`) and relabels
     them `JobTracker/Linked` + archives them in Gmail, matching what the
     existing-lead short-circuit would have done had it existed when these
     were first triaged.

Not a general-purpose tool — every id/key below is hardcoded from this one
real incident. Defaults to --dry-run (prints the plan, writes nothing);
pass --live to actually commit.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from job_tracker.email import gmail_writer
from job_tracker.email.gmail_reader import default_credentials_path, default_token_path, get_gmail_service_writable
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect

CANONICAL_KEY = "directv::senior data engineer"
GARBAGE_KEYS = [
    "directv::first 30 min video with hiring manager",
    "directv::this role is not just a traditional data engineer he wants someone",
]
# Both already are (or, after step 2, will be) correctly tied to
# CANONICAL_KEY via job_conversations, but still carry a stale outcome
# label from before the existing-lead short-circuit existed.
STALE_MESSAGE_IDS = ["19f8602fda591978", "19f863478395a204"]


def _backup_db(db_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.name}.bak-{stamp}-pre-cole-directv-fix")
    shutil.copy2(db_path, backup_path)
    return backup_path


def _fold_garbage_leads(conn, *, dry_run: bool) -> dict[str, int]:
    counts = {"conversations_deduped": 0, "conversations_moved": 0, "contacts_moved": 0, "leads_deleted": 0}
    for garbage_key in GARBAGE_KEYS:
        lead_row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (garbage_key,)).fetchone()
        if lead_row is None:
            print(f"  (already gone: {garbage_key!r})")
            continue

        for convo in conn.execute("SELECT * FROM job_conversations WHERE job_key = ?", (garbage_key,)).fetchall():
            dup = None
            if convo["message_id"]:
                dup = conn.execute(
                    "SELECT 1 FROM job_conversations WHERE job_key = ? AND message_id = ?",
                    (CANONICAL_KEY, convo["message_id"]),
                ).fetchone()
            if dup is not None:
                print(
                    f"  conversation id={convo['id']} (message_id={convo['message_id']!r}) "
                    "already on the real lead — dropping duplicate"
                )
                counts["conversations_deduped"] += 1
                if not dry_run:
                    conn.execute("DELETE FROM job_conversations WHERE id = ?", (convo["id"],))
            else:
                print(f"  conversation id={convo['id']} (message_id={convo['message_id']!r}) — moving to real lead")
                counts["conversations_moved"] += 1
                if not dry_run:
                    conn.execute("UPDATE job_conversations SET job_key = ? WHERE id = ?", (CANONICAL_KEY, convo["id"]))

        for contact in conn.execute("SELECT * FROM job_contacts WHERE job_key = ?", (garbage_key,)).fetchall():
            print(f"  contact id={contact['id']} (name={contact['name']!r}, email={contact['email']!r}) — moving to real lead")
            counts["contacts_moved"] += 1
            if not dry_run:
                conn.execute("UPDATE job_contacts SET job_key = ? WHERE id = ?", (CANONICAL_KEY, contact["id"]))

        print(f"  deleting garbage lead {garbage_key!r} (title={lead_row['title']!r})")
        counts["leads_deleted"] += 1
        if not dry_run:
            conn.execute("DELETE FROM job_leads WHERE normalized_key = ?", (garbage_key,))

    if not dry_run:
        conn.commit()
    return counts


def _consolidate_cole_contacts(conn, *, dry_run: bool) -> int:
    rows = conn.execute(
        "SELECT * FROM job_contacts WHERE job_key = ? AND (lower(name) LIKE 'cole%' OR lower(email) = 'cole@crbworkforce.com') "
        "ORDER BY first_contacted_at ASC",
        (CANONICAL_KEY,),
    ).fetchall()
    if len(rows) <= 1:
        return 0

    # Prefer the most complete row (name + email + phone) as the survivor.
    survivor = max(rows, key=lambda r: (bool(r["email"]), bool(r["phone"]), bool(r["name"])))
    merged_name = survivor["name"] or next((r["name"] for r in rows if r["name"]), "")
    merged_email = survivor["email"] or next((r["email"] for r in rows if r["email"]), "")
    merged_phone = survivor["phone"] or next((r["phone"] for r in rows if r["phone"]), "")
    first_contacted = min(r["first_contacted_at"] for r in rows)
    last_contacted = max(r["last_contacted_at"] for r in rows)

    dupe_ids = [r["id"] for r in rows if r["id"] != survivor["id"]]
    print(
        f"  consolidating {len(rows)} 'Cole' contact rows {[r['id'] for r in rows]} -> survivor id={survivor['id']} "
        f"(name={merged_name!r}, email={merged_email!r}, phone={merged_phone!r}); dropping {dupe_ids}"
    )
    if dry_run:
        return len(dupe_ids)

    conn.execute(
        "UPDATE job_contacts SET name = ?, email = ?, phone = ?, first_contacted_at = ?, last_contacted_at = ? WHERE id = ?",
        (merged_name, merged_email, merged_phone, first_contacted, last_contacted, survivor["id"]),
    )
    for dupe_id in dupe_ids:
        conn.execute("UPDATE job_conversations SET contact_id = ? WHERE contact_id = ?", (survivor["id"], dupe_id))
        conn.execute("DELETE FROM job_contacts WHERE id = ?", (dupe_id,))
    conn.commit()
    return len(dupe_ids)


def _relabel_gmail(conn, *, dry_run: bool, account: str | None) -> None:
    present_ids = [
        mid
        for mid in STALE_MESSAGE_IDS
        if conn.execute("SELECT 1 FROM processed_messages WHERE message_id = ?", (mid,)).fetchone() is not None
    ]
    if not present_ids:
        print("  no stale processed_messages rows found for these message ids — nothing to relabel")
        return

    print(f"  {len(present_ids)} message(s) to relabel JobTracker/Linked + archive, and drop from processed_messages: {present_ids}")
    if dry_run:
        return

    credentials_path = default_credentials_path(account)
    token_path = default_token_path(account, writable=True)
    service = get_gmail_service_writable(credentials_path, token_path, account=account)

    stale_outcome_ids = [
        gmail_writer.get_or_create_label(service, name)
        for name in (gmail_writer.PURSUE_LABEL, gmail_writer.SKIP_LABEL, gmail_writer.NEEDS_REVIEW_LABEL)
    ]
    linked_id = gmail_writer.get_or_create_label(service, gmail_writer.LINKED_LABEL)

    for message_id in present_ids:
        gmail_writer.label_and_archive(service, message_id, linked_id, remove_label_ids=stale_outcome_ids, archive=True)
        conn.execute("DELETE FROM processed_messages WHERE message_id = ?", (message_id,))
        print(f"    relabeled {message_id} -> JobTracker/Linked (archived)")
    conn.commit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--account", default=None, help="Gmail account for relabeling (default: recruiting funnel)")
    ap.add_argument("--live", action="store_true", help="Actually write; default is a dry-run preview")
    ap.add_argument("--skip-gmail", action="store_true", help="DB-only — skip the Gmail relabel/archive step")
    args = ap.parse_args(argv)

    dry_run = not args.live
    if dry_run:
        print("=== DRY RUN — no writes will happen; pass --live to commit ===\n", file=sys.stderr)

    if args.live:
        backup_path = _backup_db(args.db)
        print(f"Backed up {args.db} -> {backup_path}\n")

    conn = connect(args.db)
    try:
        print("Step 1: fold garbage DIRECTV leads into the real one")
        fold_counts = _fold_garbage_leads(conn, dry_run=dry_run)
        print(f"  {fold_counts}\n")

        print("Step 2: consolidate duplicate 'Cole' contact rows")
        merged = _consolidate_cole_contacts(conn, dry_run=dry_run)
        print(f"  merged away {merged} duplicate contact row(s)\n")

        if not args.skip_gmail:
            print("Step 3: relabel stale messages JobTracker/Linked + archive")
            _relabel_gmail(conn, dry_run=dry_run, account=args.account)
        else:
            print("Step 3: skipped (--skip-gmail)")
    finally:
        conn.close()

    print("\nDone." if args.live else "\nDry run complete — re-run with --live to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
