"""Shared LinkedIn / job-board senders never match a lead by contact email
(REQUIREMENTS MAT-2), even when only one lead has them on file."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, find_job_by_contact_email, upsert_lead


def _lead_with_contact(conn, company: str, **contact: str) -> str:
    upsert_lead(conn, JobLead(company=company, title="Data Engineer", source_message_id=f"m-{company}", source_label="single-jd"))
    key = f"{company.lower()}::data engineer"
    cols = ", ".join(["job_key", *contact])
    conn.execute(f"INSERT INTO job_contacts ({cols}) VALUES ({', '.join('?' * (len(contact) + 1))})", (key, *contact.values()))
    return key


def test_platform_senders_on_one_lead_do_not_match(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    for i, address in enumerate(
        (
            "messages-noreply@linkedin.com",
            "notifications-noreply@linkedin.com",
            "no-reply@alerts.talent.com",
            "no-reply@us.adzunajobs.com",
            "noreply@joblookup.com",
        )
    ):
        _lead_with_contact(conn, f"Board{i}", email=address)
        assert find_job_by_contact_email(conn, address) is None, address
    conn.close()


def test_company_sender_and_linkedin_thread_bridge_still_match(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    ats = _lead_with_contact(conn, "Collective Health", email="no-reply@collectivehealth.com")
    assert find_job_by_contact_email(conn, "no-reply@collectivehealth.com") == ats
    bridge = "0f3c2a@reply.linkedin.com"
    li = _lead_with_contact(conn, "Artech", linkedin_reply_to=bridge)
    assert find_job_by_contact_email(conn, bridge) == li
    conn.close()
