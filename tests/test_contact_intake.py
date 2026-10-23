"""CON-5: every intake path builds contacts with `contact_intake`."""

from __future__ import annotations

import re
from pathlib import Path

from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.contact_intake import contact_from_message

SRC = Path(__file__).resolve().parents[1] / "src" / "job_tracker"

SIGNED = (
    "      Dana Reyes\r\n"
    "        Reply\r\n"
    "        https://www.linkedin.com/messaging/thread/2-abc123==/\r\n"
    "\r\n"
    "Hi Shawn, are you open to a W2 contract?\r\n"
    "\r\n"
    "Dana Reyes\r\n"
    "Senior Recruiter\r\n"
    "Email: dana.reyes@agency.example\r\n"
    "Phone: 801-555-0142\r\n"
)


def _msg(**kw) -> EmailMessage:
    base = dict(id="m1", from_address="dana.reyes@agency.example", subject="Role", body_plain=SIGNED)
    base.update(kw)
    return EmailMessage(**base)


def test_signature_supplies_name_and_phone():
    c = contact_from_message("k", _msg())
    assert c is not None
    assert c.email == "dana.reyes@agency.example"
    assert c.name == "Dana Reyes"
    assert c.phone
    assert c.role == "recruiter"


def test_relay_sender_is_not_the_contact_email():
    c = contact_from_message("k", _msg(from_address="jobalerts-noreply@linkedin.com", body_plain="New jobs for you"))
    assert c is None


def test_linkedin_reply_to_is_kept_apart_from_email():
    c = contact_from_message(
        "k",
        _msg(
            from_address="inmail-hit-reply@linkedin.com",
            reply_to="abc123@reply.linkedin.com",
            body_plain="Hi Shawn, quick question about a role.",
        ),
    )
    assert c is not None
    assert c.email == ""
    assert c.linkedin_reply_to == "abc123@reply.linkedin.com"


def test_digest_skips_signature():
    c = contact_from_message("k", _msg(), read_signature=False)
    assert c is not None
    assert c.name == ""
    assert c.email == "dana.reyes@agency.example"


def test_outbound_uses_recipient_and_never_reads_own_signoff():
    c = contact_from_message(
        "k",
        _msg(from_address="shawn.becker@spexture.com", to_address="dana.reyes@agency.example",
             body_plain="Thanks!\n\nBest, Shawn\nshawn.becker@spexture.com"),
        direction="outbound",
    )
    assert c is not None
    assert c.email == "dana.reyes@agency.example"
    assert c.name == ""


def test_intake_clis_have_no_private_contact_builder():
    for name in ("triage_recruiter_inbox", "triage_imap_inbox", "scan_communications"):
        text = (SRC / "cli" / f"{name}.py").read_text()
        assert "record_message_contact(" in text, name
        assert not re.search(r"\badd_job_contact\(|\bparse_signature\(", text), name
