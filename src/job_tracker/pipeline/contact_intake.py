"""The one way an intake path turns an email into a job contact (REQUIREMENTS CON-5).

Gmail triage, IMAP triage and the communications scan all call
`record_message_contact`, so they agree on:

- the signature block (name, email, phone) — inbound only, since an outbound
  message's sign-off is Shawn's own;
- Reply-To, where LinkedIn puts the per-thread `uuid@reply.linkedin.com`
  bridge (stored as `linkedin_reply_to`, never as the email);
- relay filtering: a shared relay / digest sender is never stored as the
  contact's email, or every later digest would match this one lead.
"""

from __future__ import annotations

import sqlite3

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.models import JobContact
from job_tracker.pipeline.signature import parse_signature
from job_tracker.pipeline.store import (
    GENERIC_RELAY_ADDRESSES,
    add_job_contact,
    contact_addresses_from_inbound,
)


def contact_from_message(
    job_key: str,
    message: EmailMessage,
    *,
    direction: str = "inbound",
    message_id: str | None = None,
    read_signature: bool = True,
) -> JobContact | None:
    """The recruiter contact `message` names, or None when it names nobody.

    `read_signature=False` is for job digests that fan out into several
    roles: a digest's sign-off belongs to no single one of them."""
    detected = parse_signature(message.combined_text) if direction == "inbound" and read_signature else None
    if direction == "inbound":
        email, linkedin_reply_to = contact_addresses_from_inbound(
            from_address=message.from_address,
            reply_to=message.reply_to or "",
            signature_email=detected.email if detected else "",
        )
    else:
        other = (message.to_address or "").strip()
        email = other if other and other.lower() not in GENERIC_RELAY_ADDRESSES else ""
        linkedin_reply_to = ""
    name = detected.name if detected else ""
    phone = detected.phone if detected else ""
    if not (email or linkedin_reply_to or name or phone):
        return None
    return JobContact(
        job_key=job_key,
        name=name,
        email=email,
        phone=phone,
        linkedin_reply_to=linkedin_reply_to,
        role="recruiter",
        source_message_id=message_id or message.id,
    )


def record_message_contact(
    conn: sqlite3.Connection,
    job_key: str,
    message: EmailMessage,
    *,
    direction: str = "inbound",
    message_id: str | None = None,
    read_signature: bool = True,
) -> int | None:
    """Store `contact_from_message` on `job_key`; returns the contact id."""
    contact = contact_from_message(
        job_key, message, direction=direction, message_id=message_id, read_signature=read_signature
    )
    return add_job_contact(conn, contact) if contact is not None else None
