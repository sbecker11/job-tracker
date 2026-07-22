"""Tests for cli/backfill_post_application_signals.py — the one-time catch-up
scan of stored `job_conversations` history for post-application signals."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli.backfill_post_application_signals import main as backfill_main
from job_tracker.pipeline.models import JobConversation, JobLead
from job_tracker.pipeline.store import add_job_conversation, connect, get_lead_status, upsert_lead


@pytest.fixture()
def db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = JobLead(company="Acme Corp", title="Senior Engineer", source_message_id="m0", source_label="single-jd")
    upsert_lead(conn, lead)
    key = lead.normalized_key
    add_job_conversation(
        conn,
        JobConversation(
            job_key=key,
            message_id="msg-rej",
            direction="inbound",
            summary="Update on your application",
            body_text="Unfortunately, we have decided to move forward with other candidates.",
            occurred_at="2026-06-01T00:00:00Z",
        ),
    )
    conn.close()
    return db_path


def _key() -> str:
    from job_tracker.pipeline.models import normalize_key

    return normalize_key("Acme Corp", "Senior Engineer")


def test_backfill_advances_lead_to_rejected(db: Path, capsys):
    rc = backfill_main(["--db", str(db)])
    assert rc == 0

    conn = connect(db)
    assert get_lead_status(conn, _key()) == "rejected"
    conn.close()

    err = capsys.readouterr().err
    assert "1 lead(s) actually updated" in err


def test_backfill_dry_run_writes_nothing(db: Path):
    rc = backfill_main(["--db", str(db), "--dry-run"])
    assert rc == 0

    conn = connect(db)
    assert get_lead_status(conn, _key()) == "new"
    conn.close()


def test_backfill_is_idempotent_on_rerun(db: Path, capsys):
    backfill_main(["--db", str(db)])
    capsys.readouterr()
    rc = backfill_main(["--db", str(db)])
    assert rc == 0

    err = capsys.readouterr().err
    assert "0 lead(s) actually updated" in err


def test_backfill_ignores_next_steps_only_conversations(tmp_path: Path, capsys):
    db_path = tmp_path / "leads2.db"
    conn = connect(db_path)
    lead = JobLead(company="Beta Inc", title="Engineer", source_message_id="m0", source_label="single-jd")
    upsert_lead(conn, lead)
    key = lead.normalized_key
    add_job_conversation(
        conn,
        JobConversation(
            job_key=key,
            message_id="msg-followup",
            direction="inbound",
            summary="Checking in",
            body_text="Just checking in on your availability for a call.",
            occurred_at="2026-06-01T00:00:00Z",
        ),
    )
    conn.close()

    rc = backfill_main(["--db", str(db_path)])
    assert rc == 0

    conn = connect(db_path)
    assert get_lead_status(conn, key) == "new"
    conn.close()


def test_backfill_json_output(db: Path, capsys):
    rc = backfill_main(["--db", str(db), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"label": "rejection"' in out
