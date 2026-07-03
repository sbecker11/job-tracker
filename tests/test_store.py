"""Tests for the SQLite-backed lead store, including schema migration."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import _SCHEMA, connect, upsert_lead


def test_jd_text_persists_on_insert(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(
        conn,
        JobLead(
            company="Acme",
            title="Engineer",
            source_message_id="m1",
            source_label="single-jd",
            jd_resolved=True,
            jd_source="ats_api",
            jd_text="Full JD body text here.",
        ),
    )
    row = conn.execute("SELECT jd_text, jd_source FROM job_leads WHERE company='Acme'").fetchone()
    assert row["jd_text"] == "Full JD body text here."
    assert row["jd_source"] == "ats_api"
    conn.close()


def test_jd_text_refreshes_while_status_is_new(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = JobLead(
        company="Acme",
        title="Engineer",
        source_message_id="m1",
        source_label="single-jd",
        jd_resolved=False,
        jd_source="email_body",
        jd_text="First pass — only the email body was available.",
    )
    upsert_lead(conn, lead)

    # Re-seen later, this time the ATS resolved successfully with the real JD.
    better_lead = JobLead(
        company="Acme",
        title="Engineer",
        source_message_id="m2",
        source_label="single-jd",
        jd_resolved=True,
        jd_source="ats_api",
        jd_text="Second pass — full ATS-resolved JD text.",
    )
    is_new = upsert_lead(conn, better_lead)
    assert is_new is False

    row = conn.execute("SELECT jd_text, jd_source, jd_resolved FROM job_leads WHERE company='Acme'").fetchone()
    assert row["jd_text"] == "Second pass — full ATS-resolved JD text."
    assert row["jd_source"] == "ats_api"
    assert row["jd_resolved"] == 1
    conn.close()


def test_jd_text_preserved_once_status_is_no_longer_new(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(
        conn,
        JobLead(
            company="Acme",
            title="Engineer",
            source_message_id="m1",
            source_label="single-jd",
            jd_text="Original JD text the user already reviewed.",
        ),
    )
    conn.execute("UPDATE job_leads SET status = 'pursuing' WHERE company = 'Acme'")
    conn.commit()

    # A later re-send of the same digest must not silently overwrite the JD
    # text (or anything else) once a human has started acting on the lead.
    upsert_lead(
        conn,
        JobLead(
            company="Acme",
            title="Engineer",
            source_message_id="m2",
            source_label="single-jd",
            jd_text="A different digest re-send's JD text.",
        ),
    )
    row = conn.execute("SELECT jd_text, status FROM job_leads WHERE company='Acme'").fetchone()
    assert row["jd_text"] == "Original JD text the user already reviewed."
    assert row["status"] == "pursuing"
    conn.close()


def test_connect_migrates_a_pre_existing_db_missing_jd_text_column(tmp_path: Path):
    """Regression: upgrading job-tracker must not require deleting var/leads.db."""
    db_path = tmp_path / "old_leads.db"

    # Simulate a database created before the jd_text column existed.
    legacy_schema = _SCHEMA.replace("    jd_text TEXT,\n", "")
    assert "jd_text" not in legacy_schema
    raw_conn = sqlite3.connect(str(db_path))
    raw_conn.execute(legacy_schema)
    raw_conn.execute(
        """
        INSERT INTO job_leads (normalized_key, company, title, status, first_seen, last_seen)
        VALUES ('acme::engineer', 'Acme', 'Engineer', 'new', '2026-01-01T00:00:00', '2026-01-01T00:00:00')
        """
    )
    raw_conn.commit()
    raw_conn.close()

    conn = connect(db_path)  # should migrate in place, not raise
    row = conn.execute("SELECT jd_text FROM job_leads WHERE company='Acme'").fetchone()
    assert row["jd_text"] is None
    conn.close()
