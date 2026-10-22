"""Shawn's own mail is never a recruiter reply or a contact (REQUIREMENTS ING-1, ING-6)."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.models import JobContact, JobLead
from job_tracker.pipeline.store import (
    add_job_contact,
    connect,
    find_job_by_contact_email,
    is_own_address,
    is_own_sent_message,
    list_job_contacts,
    upsert_lead,
)


def _lead(conn) -> str:
    upsert_lead(conn, JobLead(company="Artech", title="Software Engineer", source_message_id="m1", source_label="single-jd"))
    return "artech::software engineer"


def test_own_addresses():
    for a in ("shawn.becker@spexture.com", "Shawn Becker <Shawn.Becker@spexture.com>", "shawnbecker.recruiting@gmail.com", "scbboston@gmail.com"):
        assert is_own_address(a), a
    assert not is_own_address("priyanshu.sharma@artech.com")


def test_own_replies_skipped_but_forwards_triaged():
    assert is_own_sent_message("shawn.becker@spexture.com", "RE: Artech Software Engineer")
    assert is_own_sent_message("shawn.becker@spexture.com", "Re: FW: Follow Up")
    assert not is_own_sent_message("scbboston@gmail.com", "Fwd: SHAWN, First Electronic Bank may want to hire you")
    assert not is_own_sent_message("scbboston@gmail.com", "FW: Data Engineer")
    assert not is_own_sent_message("priyanshu.sharma@artech.com", "RE: Artech Software Engineer")


def test_own_address_never_becomes_a_contact_or_a_match(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn)
    assert add_job_contact(conn, JobContact(job_key=key, email="shawn.becker@spexture.com", name="Shawn Becker")) is None
    assert list_job_contacts(conn, key) == []
    conn.execute("INSERT INTO job_contacts (job_key, email) VALUES (?, ?)", (key, "scbboston@gmail.com"))
    assert find_job_by_contact_email(conn, "scbboston@gmail.com") is None
    conn.close()
