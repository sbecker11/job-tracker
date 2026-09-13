"""set-lead-status CLI + store.append_lead_note — 'Manage lead' status control."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli.set_lead_status import main as set_lead_status_main
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import append_lead_note, connect, upsert_lead


def _make_lead(conn, **overrides) -> JobLead:
    lead = JobLead(
        company=overrides.pop("company", "Thyme Care"),
        title=overrides.pop("title", "Senior Software Engineer"),
        source_message_id="imap:thyme-1",
        source_label="ats_api",
        **overrides,
    )
    upsert_lead(conn, lead)
    return lead


def test_append_lead_note_appends_not_clobbers(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    lead = _make_lead(conn)
    append_lead_note(conn, lead.normalized_key, "first note", when="2026-08-01T00:00:00+00:00")
    append_lead_note(conn, lead.normalized_key, "second note", when="2026-08-02T00:00:00+00:00")
    notes = conn.execute(
        "SELECT notes FROM job_leads WHERE normalized_key = ?", (lead.normalized_key,)
    ).fetchone()["notes"]
    assert "[2026-08-01T00:00:00+00:00] first note" in notes
    assert "[2026-08-02T00:00:00+00:00] second note" in notes
    assert notes.index("first note") < notes.index("second note")
    conn.close()


def test_append_lead_note_missing_key_raises(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    with pytest.raises(ValueError):
        append_lead_note(conn, "does-not-exist::role", "note")
    conn.close()


def test_set_lead_status_updates_status_and_logs_reason(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = _make_lead(conn, status="skipped")
    conn.close()

    rc = set_lead_status_main(
        [
            "--db",
            str(db),
            "--key",
            lead.normalized_key,
            "--status",
            "applied",
            "--reason",
            "Shawn confirmed he already applied for this role.",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped -> applied" in out

    conn = connect(db)
    row = conn.execute(
        "SELECT status, applied_at, notes FROM job_leads WHERE normalized_key = ?",
        (lead.normalized_key,),
    ).fetchone()
    assert row["status"] == "applied"
    assert row["applied_at"] is not None
    assert "skipped -> applied" in row["notes"]
    assert "Shawn confirmed he already applied for this role." in row["notes"]
    conn.close()


def test_set_lead_status_unknown_key_returns_error(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    connect(db).close()  # create an empty but valid DB
    rc = set_lead_status_main(["--db", str(db), "--key", "nope::role", "--status", "applied"])
    assert rc == 1
    assert "No lead found" in capsys.readouterr().err


def test_set_lead_status_missing_db_returns_error(tmp_path: Path, capsys):
    rc = set_lead_status_main(
        ["--db", str(tmp_path / "does-not-exist.db"), "--key", "x::y", "--status", "applied"]
    )
    assert rc == 1
    assert "No leads DB found" in capsys.readouterr().err
