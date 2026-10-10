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
from job_tracker.pipeline.models import JobContact, JobConversation, JobDocument, JobLead, UnmatchedMessage
from job_tracker.pipeline.store import (
    add_job_contact,
    add_job_conversation,
    add_job_document,
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


def test_search_db_collapses_many_conversations_to_one_lead(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    key = "artech::data engineer"
    add_job_contact(
        conn,
        JobContact(job_key=key, name="Ramdas Sakthivel", email="ramdas@arthurgrand.com"),
    )
    for i, summary in enumerate(("JD", "Rate Confirmation", "Re: Rate Confirmation")):
        add_job_conversation(
            conn,
            JobConversation(
                job_key=key,
                message_id=f"m-{i}",
                summary=summary,
                body_text=f"From ramdas@arthurgrand.com — {summary}",
                direction="inbound",
                channel="email",
            ),
        )
    hits = search_db(conn, "ramdas@arthurgrand.com")
    conn.close()
    keyed = [h for h in hits if h.get("job_key") == key]
    assert len(keyed) == 1
    assert keyed[0]["comm_count"] == 3
    assert keyed[0]["subject"] == "Artech — Data Engineer"
    assert keyed[0]["reveal_url"].startswith("revealfolder://")
    assert keyed[0]["folder_path"]


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

    result = search("Aakriti", conn=conn, imap_search=fake_imap, skip_files=True)
    conn.close()
    assert result["imap"] == []  # same sender is already the tracked lead
    assert result["db"]
    assert result["imap_error"] == ""


def test_search_keeps_imap_when_sender_is_not_a_tracked_lead(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)

    def fake_imap(_query: str):
        return [
            {
                "source": "imap",
                "when": "before_tick",
                "from_address": "new.recruiter@example.com",
                "subject": "New JD",
                "folder": "INBOX",
            },
            {
                "source": "imap",
                "when": "before_tick",
                "from_address": "new.recruiter@example.com",
                "subject": "Re: New JD",
                "folder": "INBOX",
            },
        ]

    result = search("new.recruiter@example.com", conn=conn, imap_search=fake_imap, skip_files=True)
    conn.close()
    assert len(result["imap"]) == 1
    assert result["imap"][0]["from_address"] == "new.recruiter@example.com"


def test_search_skip_imap_still_returns_db(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    add_job_contact(
        conn,
        JobContact(job_key="artech::data engineer", name="Aakriti Luitel", email="aakriti.luitel@artech.com"),
    )
    result = search("Aakriti", conn=conn, skip_imap=True, skip_files=True)
    conn.close()
    assert result["imap"] == []
    assert result["db"]


def test_search_db_matches_jd_text_on_skipped_lead(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    upsert_lead(
        conn,
        JobLead(
            company="DASH2 Group",
            title="Software Engineer",
            source_message_id="m-poelman",
            source_label="single-jd",
            status="skipped",
            jd_text="Follow up from Elder Poelman at the Employment Center.",
        ),
    )
    hits = search_db(conn, "Poelman")
    conn.close()
    keyed = [h for h in hits if h.get("company") == "DASH2 Group"]
    assert keyed
    assert keyed[0]["status"] == "skipped"
    assert any("jd_text" in f for f in keyed[0].get("match_fields") or [])


def test_search_db_matches_notes_field(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    conn.execute(
        "UPDATE job_leads SET notes = ? WHERE normalized_key = ?",
        ("Called Elder Poelman back on Friday", "artech::data engineer"),
    )
    conn.commit()
    hits = search_db(conn, "Poelman")
    conn.close()
    assert any(h.get("job_key") == "artech::data engineer" for h in hits)
    assert any(
        "notes" in f for h in hits for f in (h.get("match_fields") or []) if h.get("job_key") == "artech::data engineer"
    )


def test_search_db_matches_package_file_contents(tmp_path: Path):
    from job_tracker.pipeline.llm_apply import _safe_filename

    db = _db(tmp_path)
    conn = connect(db)
    root = tmp_path / "packages"
    role = root / _safe_filename("Artech") / _safe_filename("Data Engineer")
    role.mkdir(parents=True)
    (role / "Followup.txt").write_text("Elder Poelman asked me to check Mecor.")
    hits = search_db(conn, "Poelman", output_root=root)
    conn.close()
    keyed = [h for h in hits if h.get("job_key") == "artech::data engineer"]
    assert keyed
    assert any("Followup.txt" in f for f in keyed[0].get("match_fields") or [])


def test_search_db_matches_job_document_file(tmp_path: Path):
    db = _db(tmp_path)
    conn = connect(db)
    doc = tmp_path / "counselor-notes.txt"
    doc.write_text("Recommendations from John Poelman: Silicon Slopes job board.")
    add_job_document(
        conn,
        JobDocument(job_key="artech::data engineer", doc_type="other", path_or_url=str(doc)),
    )
    hits = search_db(conn, "Poelman")
    conn.close()
    keyed = [h for h in hits if h.get("job_key") == "artech::data engineer"]
    assert keyed
    assert any("job_documents" in f for f in keyed[0].get("match_fields") or [])
