"""CLI: manually create a Job (docs/JOB_CRM_VISION.md UC-3) for a role that
never came in as a triaged email — found via a careers page, a referral, or
a conversation. Purely interactive by design (a --company/--title flags
version was considered and explicitly passed on): manual entry is occasional
enough that a short back-and-forth prompt beats remembering flag names.

`main()` takes `input_func`/`print_func` so tests can drive it with canned
answers instead of real stdin/stdout.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from job_tracker.pipeline.models import JobContact, JobConversation, JobLead, LEAD_STAGES
from job_tracker.pipeline.store import (
    DEFAULT_DB_PATH,
    add_job_contact,
    add_job_conversation,
    advance_status,
    connect,
    upsert_lead,
)

_ROLE_CHOICES = ("recruiter", "hiring_manager", "referral", "other")
# advance_status() only accepts LEAD_STAGES; "new" is the no-op default (no
# <stage>_at column to stamp), so it's the sensible default for a lead
# that's merely being tracked, not yet acted on.
_STATUS_CHOICES = LEAD_STAGES


def _prompt(input_func, print_func, question: str, *, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input_func(f"{question}{suffix}: ").strip()
    return answer or default


def _prompt_choice(input_func, print_func, question: str, choices: tuple[str, ...], *, default: str) -> str:
    while True:
        answer = _prompt(input_func, print_func, f"{question} ({'/'.join(choices)})", default=default)
        if answer in choices:
            return answer
        print_func(f"  '{answer}' isn't one of {choices} — try again.")


def main(argv: list[str] | None = None, *, input_func=input, print_func=print) -> int:
    ap = argparse.ArgumentParser(
        description="Interactively add a Job that didn't come from a triaged email (UC-3)."
    )
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help=f"Leads DB path (default: {DEFAULT_DB_PATH})")
    args = ap.parse_args(argv)

    print_func("Add a job manually (Enter to skip any optional field, Ctrl-C to abort).\n")

    company = _prompt(input_func, print_func, "Company")
    while not company:
        print_func("  Company is required.")
        company = _prompt(input_func, print_func, "Company")

    title = _prompt(input_func, print_func, "Title")
    while not title:
        print_func("  Title is required.")
        title = _prompt(input_func, print_func, "Title")

    apply_url = _prompt(input_func, print_func, "Apply URL (optional)")
    note = _prompt(input_func, print_func, "Note (optional — how you found it, referral source, etc.)")
    status = _prompt_choice(input_func, print_func, "Initial status", _STATUS_CHOICES, default="new")

    contact_name = _prompt(input_func, print_func, "Contact name (optional)")
    contact_email = ""
    contact_phone = ""
    contact_role = "recruiter"
    if contact_name:
        contact_email = _prompt(input_func, print_func, "Contact email (optional)")
        contact_phone = _prompt(input_func, print_func, "Contact phone (optional)")
        contact_role = _prompt_choice(input_func, print_func, "Contact role", _ROLE_CHOICES, default="recruiter")

    conn = connect(args.db)
    try:
        lead = JobLead(
            company=company,
            title=title,
            source_message_id="",
            source_label="manual",
            apply_url=apply_url,
        )
        key = lead.normalized_key
        is_new = upsert_lead(conn, lead)
        if status != "new":
            advance_status(conn, key, status)

        if contact_name or contact_email:
            add_job_contact(
                conn,
                JobContact(job_key=key, name=contact_name, email=contact_email, phone=contact_phone, role=contact_role),
            )

        if note:
            # direction="other" deliberately leaves awaiting_response_since
            # untouched — a "how I found this" note isn't a turn in a
            # back-and-forth, so it shouldn't imply either side is waiting.
            add_job_conversation(
                conn,
                JobConversation(job_key=key, channel="other", direction="other", summary=note),
            )

        print_func(
            f"\n{'Added' if is_new else 'Updated existing'} job: {title} @ {company} "
            f"(status={status}, key={key})"
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
