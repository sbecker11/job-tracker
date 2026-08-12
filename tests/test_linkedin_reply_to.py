"""LinkedIn uuid@reply.linkedin.com capture + mailto fallback."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.comms_match import match_message_to_job
from job_tracker.pipeline.models import JobContact, JobLead
from job_tracker.pipeline.pending_workflow import _recruiter_contact
from job_tracker.pipeline.store import (
    add_job_contact,
    connect,
    contact_addresses_from_inbound,
    find_job_by_contact_email,
    is_linkedin_thread_reply_address,
    linkedin_reply_to_from_header,
    upsert_lead,
)
from job_tracker.email.models import EmailMessage


def test_linkedin_reply_to_from_header_parses_display_name():
    assert (
        linkedin_reply_to_from_header(
            "Deven Mehta <a971f516-ea0f-42bf-a315-bdb8d949c45f@reply.linkedin.com>"
        )
        == "a971f516-ea0f-42bf-a315-bdb8d949c45f@reply.linkedin.com"
    )
    assert linkedin_reply_to_from_header("deven@neotekus.com") == ""
    assert is_linkedin_thread_reply_address("x@reply.linkedin.com")
    assert not is_linkedin_thread_reply_address("hit-reply@linkedin.com")


def test_contact_addresses_prefers_signature_keeps_reply_to():
    email, li = contact_addresses_from_inbound(
        from_address="hit-reply@linkedin.com",
        reply_to="uuid-1@reply.linkedin.com",
        signature_email="Deven.Mehta@neotekus.com",
    )
    assert email == "Deven.Mehta@neotekus.com"
    assert li == "uuid-1@reply.linkedin.com"


def test_contact_addresses_falls_back_to_linkedin_reply_only():
    email, li = contact_addresses_from_inbound(
        from_address="hit-reply@linkedin.com",
        reply_to="uuid-2@reply.linkedin.com",
        signature_email="",
    )
    assert email == ""
    assert li == "uuid-2@reply.linkedin.com"


def test_add_job_contact_never_stores_reply_as_email(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(
        conn,
        JobLead(
            company="NeoTek",
            title="AWS AI Engineer",
            source_message_id="m1",
            source_label="linkedin_message",
        ),
    )
    key = "neotek::aws ai engineer"
    cid = add_job_contact(
        conn,
        JobContact(
            job_key=key,
            name="Deven Mehta",
            email="uuid-3@reply.linkedin.com",  # mis-filed as email on purpose
        ),
    )
    row = conn.execute("SELECT email, linkedin_reply_to FROM job_contacts WHERE id = ?", (cid,)).fetchone()
    assert (row["email"] or "") == ""
    assert row["linkedin_reply_to"] == "uuid-3@reply.linkedin.com"
    conn.close()


def test_outbound_tier2_matches_linkedin_reply_to(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(
        conn,
        JobLead(
            company="NeoTek",
            title="AWS AI Engineer",
            source_message_id="m1",
            source_label="linkedin_message",
        ),
    )
    key = "neotek::aws ai engineer"
    add_job_contact(
        conn,
        JobContact(job_key=key, name="Deven Mehta", linkedin_reply_to="uuid-out@reply.linkedin.com"),
    )
    assert find_job_by_contact_email(conn, "uuid-out@reply.linkedin.com") == key
    outcome = match_message_to_job(
        conn,
        EmailMessage(
            id="sent-1",
            from_address="shawn.becker@spexture.com",
            to_address="uuid-out@reply.linkedin.com",
            subject="Re: AWS AI",
            body_plain="Quick clarifiers...",
        ),
        direction="outbound",
    )
    assert outcome.matched
    assert outcome.job_key == key
    assert outcome.tier == "contact_email"
    conn.close()


def test_recruiter_contact_flags_linkedin_relay(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(
        conn,
        JobLead(
            company="NeoTek",
            title="AWS AI Engineer",
            source_message_id="m1",
            source_label="linkedin_message",
        ),
    )
    key = "neotek::aws ai engineer"
    add_job_contact(
        conn,
        JobContact(job_key=key, name="Deven Mehta", linkedin_reply_to="uuid-ui@reply.linkedin.com"),
    )
    name, email, is_relay = _recruiter_contact(conn, key)
    assert name == "Deven Mehta"
    assert email == "uuid-ui@reply.linkedin.com"
    assert is_relay is True
    conn.close()


def test_recruiter_contact_prefers_real_email_over_relay(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(
        conn,
        JobLead(
            company="NeoTek",
            title="AWS AI Engineer",
            source_message_id="m1",
            source_label="linkedin_message",
        ),
    )
    key = "neotek::aws ai engineer"
    add_job_contact(
        conn,
        JobContact(
            job_key=key,
            name="Deven Mehta",
            email="Deven.Mehta@neotekus.com",
            linkedin_reply_to="uuid-ui@reply.linkedin.com",
        ),
    )
    name, email, is_relay = _recruiter_contact(conn, key)
    assert name == "Deven Mehta"
    assert email.lower() == "deven.mehta@neotekus.com"
    assert is_relay is False
    conn.close()
