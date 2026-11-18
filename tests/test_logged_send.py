"""Message sent? posts to the API on the machine that holds the database."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.pipeline.logged_send import LoggedSendError, apply_logged_send
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead


def test_message_sent_logs_the_followup_on_this_database(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(
        company="Intentsify",
        title="Backend Engineer",
        source_message_id="imap:intentsify-1",
        source_label="linkedin_message",
        status="package_generated",
    )
    upsert_lead(conn, lead)
    conn.close()

    detail = apply_logged_send(
        db,
        {
            "scheme": "leadstatus",
            "key": lead.normalized_key,
            "status": "following_up",
            "sent_channel": "linkedin",
            "sent_message": "Hi Ed,\n\nStill very interested.\n\nBest, Shawn",
        },
    )
    assert "package_generated -> following_up" in detail

    conn = connect(db)
    row = conn.execute(
        "SELECT status, awaiting_response_since FROM job_leads WHERE normalized_key = ?",
        (lead.normalized_key,),
    ).fetchone()
    body = conn.execute(
        "SELECT direction, channel, body_text FROM job_conversations WHERE job_key = ?",
        (lead.normalized_key,),
    ).fetchone()
    conn.close()
    assert row["status"] == "following_up"
    assert row["awaiting_response_since"]
    assert body["direction"] == "outbound"
    assert body["channel"] == "linkedin"
    assert "Still very interested." in body["body_text"]


def test_message_sent_rejects_a_missing_lead(tmp_path: Path):
    db = tmp_path / "leads.db"
    connect(db).close()
    with pytest.raises(LoggedSendError, match="No lead found"):
        apply_logged_send(
            db,
            {
                "scheme": "leadstatus",
                "key": "missing::role",
                "status": "following_up",
                "sent_channel": "linkedin",
                "sent_message": "Hi Ed,",
            },
        )
