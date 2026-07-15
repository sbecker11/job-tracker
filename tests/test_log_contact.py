"""Tests for the log_contact CLI (manual conversation/meeting logging)."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli.log_contact import main as log_contact_main
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, list_job_conversations, list_job_meetings, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(conn, JobLead(company="Acme", title="Software Engineer", source_message_id="m1", source_label="single-jd"))
    conn.close()
    return db_path


def _key(db_path: Path) -> str:
    conn = connect(db_path)
    row = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Acme'").fetchone()
    conn.close()
    return row["normalized_key"]


def test_log_contact_conversation_outbound_sets_awaiting_response(seeded_db: Path):
    rc = log_contact_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--conversation", "--channel", "email", "--direction", "outbound",
            "--summary", "Sent a follow-up email",
        ]
    )
    assert rc == 0

    conn = connect(seeded_db)
    key = _key(seeded_db)
    conversations = list_job_conversations(conn, key)
    assert len(conversations) == 1
    assert conversations[0]["direction"] == "outbound"
    row = conn.execute("SELECT awaiting_response_since FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    assert row["awaiting_response_since"] is not None
    conn.close()


def test_log_contact_conversation_requires_direction_and_summary(seeded_db: Path):
    with pytest.raises(SystemExit):
        log_contact_main(["--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer", "--conversation"])


def test_log_contact_meeting_records_interview(seeded_db: Path):
    rc = log_contact_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--meeting", "--kind", "technical", "--status", "completed", "--notes", "Went well",
        ]
    )
    assert rc == 0

    conn = connect(seeded_db)
    meetings = list_job_meetings(conn, _key(seeded_db))
    assert len(meetings) == 1
    assert meetings[0]["kind"] == "technical"
    assert meetings[0]["status"] == "completed"
    conn.close()


def test_log_contact_meeting_with_waiting_override_sets_awaiting_response(seeded_db: Path):
    rc = log_contact_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--meeting", "--kind", "technical", "--status", "completed", "--waiting",
        ]
    )
    assert rc == 0

    conn = connect(seeded_db)
    row = conn.execute(
        "SELECT awaiting_response_since FROM job_leads WHERE normalized_key = ?", (_key(seeded_db),)
    ).fetchone()
    assert row["awaiting_response_since"] is not None
    conn.close()


def test_log_contact_conversation_with_not_waiting_override_clears_awaiting_response(seeded_db: Path):
    conn = connect(seeded_db)
    conn.execute(
        "UPDATE job_leads SET awaiting_response_since = '2026-01-01T00:00:00+00:00' WHERE normalized_key = ?",
        (_key(seeded_db),),
    )
    conn.commit()
    conn.close()

    rc = log_contact_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--conversation", "--direction", "outbound", "--summary", "Left a voicemail", "--not-waiting",
        ]
    )
    assert rc == 0

    conn = connect(seeded_db)
    row = conn.execute(
        "SELECT awaiting_response_since FROM job_leads WHERE normalized_key = ?", (_key(seeded_db),)
    ).fetchone()
    assert row["awaiting_response_since"] is None
    conn.close()


def test_log_contact_attaches_contact_with_phone(seeded_db: Path):
    rc = log_contact_main(
        [
            "--db", str(seeded_db), "--company", "Acme", "--title", "Software Engineer",
            "--conversation", "--direction", "inbound", "--summary", "They called",
            "--contact-name", "Jane Doe", "--contact-phone", "555-1234",
        ]
    )
    assert rc == 0

    conn = connect(seeded_db)
    contact = conn.execute("SELECT * FROM job_contacts WHERE job_key = ?", (_key(seeded_db),)).fetchone()
    assert contact["name"] == "Jane Doe"
    assert contact["phone"] == "555-1234"
    conn.close()


def test_log_contact_unknown_job_reports_error(seeded_db: Path, capsys):
    rc = log_contact_main(
        [
            "--db", str(seeded_db), "--company", "Nonexistent Co", "--title", "Nowhere Job",
            "--conversation", "--direction", "outbound", "--summary", "n/a",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "No job found" in err
