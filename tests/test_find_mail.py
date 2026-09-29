"""Tests for pipeline.find_mail — DB search (after tick) and IMAP criteria (before tick)."""

from __future__ import annotations

from pathlib import Path

from job_tracker.pipeline.find_mail import (
    hostinger_mailbox_url,
    imap_search_criteria,
    lead_href,
    search,
    search_db,
)
from job_tracker.pipeline.models import JobContact, JobConversation, JobLead, UnmatchedMessage
from job_tracker.pipeline.store import (
    add_job_contact,
    add_job_conversation,
    connect,
    record_unmatched_message,
    upsert_lead,
)


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "leads.db"
    conn = connect(path)
    upsert_lead(
        conn,
        JobLead(company="Artech", title="Data Engineer", source_message_id="m1", source_label="single-jd"),
    )
    conn.close()
    return path


def test_hostinger_url_keeps_dots():
    assert hostinger_mailbox_url("INBOX.JobTracker.Linked").endswith("/INBOX.JobTracker.Linked")


def test_lead_href_collapses_key():
    assert lead_href("artech::data engineer") == "http://127.0.0.1:3174/#lead-artech--data-engineer"


def test_imap_criteria_ors_from_subject_text():
    crit = imap_search_criteria("Aakriti")
    assert "FROM" in crit and "SUBJECT" in crit and "TEXT" in crit
    assert "Aakriti" in crit


def test_search_db_matches_contact_name_and_email(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    key = "artech::data engineer"
    add_job_contact(
        conn,
        JobContact(job_key=key, name="Aakriti Luitel", email="aakriti.luitel@artech.com", phone="(973) 967-3401"),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=key,
            summary="Data Engineer for a remote position with a 1-year duration",
            body_text="We're seeking a Data Engineer for a remote position. Spark, Python.",
            direction="inbound",
            channel="email",
        ),
    )
    hits = search_db(conn, "Aakriti")
    conn.close()
    assert hits
    assert any("aakriti.luitel@artech.com" in (h["from_address"] or "").lower() for h in hits)
    assert any(h["company"] == "Artech" for h in hits)


def test_search_db_matches_company_and_title(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    by_company = search_db(conn, "Artech")
    by_title = search_db(conn, "Data Engineer")
    conn.close()
    assert any(h["company"] == "Artech" and h["title"] == "Data Engineer" for h in by_company)
    assert any(h["title"] == "Data Engineer" for h in by_title)


def test_search_db_matches_phone_digits(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    add_job_contact(
        conn,
        JobContact(
            job_key="artech::data engineer",
            name="Aakriti Luitel",
            email="aakriti.luitel@artech.com",
            phone="(973) 967-3401",
        ),
    )
    hits = search_db(conn, "9739673401")
    conn.close()
    assert hits
    assert hits[0]["contact_name"] == "Aakriti Luitel"


def test_search_db_matches_unmatched_from(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="mid-1",
            from_address="recruiter@example.com",
            subject="Urgent JD",
            body_text="Please find the job description attached.",
            direction="inbound",
        ),
    )
    hits = search_db(conn, "recruiter@example.com")
    conn.close()
    assert any(h["subject"] == "Urgent JD" for h in hits)


def test_search_combines_db_and_mocked_imap(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    add_job_contact(
        conn,
        JobContact(job_key="artech::data engineer", name="Aakriti Luitel", email="aakriti.luitel@artech.com"),
    )

    def fake_imap(_query: str):
        return [
            {
                "source": "imap",
                "when": "before_tick",
                "from_address": "Aakriti.Luitel@artech.com",
                "subject": "Data Engineer for a remote",
                "folder": "INBOX",
                "hostinger_url": hostinger_mailbox_url("INBOX"),
            }
        ]

    result = search("Aakriti", conn=conn, imap_search=fake_imap)
    conn.close()
    assert result["imap"][0]["folder"] == "INBOX"
    assert result["db"]
    assert result["imap_error"] == ""


def test_search_skip_imap_still_returns_db(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    add_job_contact(
        conn,
        JobContact(job_key="artech::data engineer", name="Aakriti Luitel", email="aakriti.luitel@artech.com"),
    )
    result = search("Aakriti", conn=conn, skip_imap=True)
    conn.close()
    assert result["imap"] == []
    assert result["db"]
