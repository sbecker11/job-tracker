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


def test_set_lead_status_with_sent_message_logs_outbound_and_starts_wait(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = _make_lead(conn, status="applied")
    conn.close()

    rc = set_lead_status_main(
        [
            "--db", str(db),
            "--key", lead.normalized_key,
            "--status", "following_up",
            "--sent-channel", "linkedin",
            "--sent-message", "Hi Kimber,\n\nJust bumping this.\n\nBest,\nShawn",
            "--on", "2026-10-20T19:30:00+00:00",
        ]
    )
    assert rc == 0
    conn = connect(db)
    row = conn.execute(
        "SELECT status, awaiting_response_since, notes FROM job_leads WHERE normalized_key = ?",
        (lead.normalized_key,),
    ).fetchone()
    assert row["status"] == "following_up"
    assert row["awaiting_response_since"] == "2026-10-20T19:30:00+00:00"
    assert "applied -> following_up (message sent via linkedin)" in row["notes"]
    conv = conn.execute(
        "SELECT direction, channel, body_text FROM job_conversations WHERE job_key = ?",
        (lead.normalized_key,),
    ).fetchone()
    assert (conv["direction"], conv["channel"]) == ("outbound", "linkedin")
    assert conv["body_text"].startswith("Hi Kimber,")
    conn.close()


def test_set_lead_status_sent_channel_requires_message(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = _make_lead(conn, status="applied")
    conn.close()
    rc = set_lead_status_main(
        ["--db", str(db), "--key", lead.normalized_key, "--status", "following_up", "--sent-channel", "email"]
    )
    assert rc == 1
    conn = connect(db)
    assert conn.execute(
        "SELECT status FROM job_leads WHERE normalized_key = ?", (lead.normalized_key,)
    ).fetchone()["status"] == "applied"
    conn.close()


def test_followup_draft_uses_first_name_and_drops_placeholders():
    from job_tracker.pipeline.pending_workflow import draft_wait_followup_message

    msg = draft_wait_followup_message(
        recruiter_name="Kimber Gabryszak",
        company="North Grand Technology (end-client TBD)",
        title="Role TBD",
        waiting_days=9,
    )
    assert msg.startswith(
        "Hi Kimber,\n\nJust checking in on the role at North Grand Technology. "
        "It's been 9 days since I last reached out, so I wanted to see"
    )
    assert "TBD" not in msg
    assert draft_wait_followup_message(title="Data Engineer", company="Acme").startswith(
        "Hi there,\n\nJust checking in on the Data Engineer role at Acme. I wanted to see"
    )
    assert msg.endswith("Best,\nShawn")


def test_reopened_status_clears_deleted_and_stamps_latest_reopen(tmp_path: Path):
    from job_tracker.pipeline.store import advance_status

    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = _make_lead(conn)
    advance_status(conn, lead.normalized_key, "applied", when="2026-07-03T10:00:00+00:00")
    advance_status(conn, lead.normalized_key, "reopened", when="2026-07-05T10:00:00+00:00")
    advance_status(conn, lead.normalized_key, "deleted", when="2026-07-09T10:00:00+00:00")
    conn.close()

    rc = set_lead_status_main(
        ["--db", str(db), "--key", lead.normalized_key, "--status", "reopened", "--on", "2026-07-12T10:00:00+00:00"]
    )
    assert rc == 0
    conn = connect(db)
    row = conn.execute(
        "SELECT status, deleted_at, reopened_at, notes FROM job_leads WHERE normalized_key = ?",
        (lead.normalized_key,),
    ).fetchone()
    assert row["status"] == "reopened"
    assert row["deleted_at"] is None
    assert row["reopened_at"] == "2026-07-12T10:00:00+00:00"
    assert "Status changed deleted -> reopened" in row["notes"]
    conn.close()


def test_reply_with_unchanged_status_logs_message_and_starts_wait(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = _make_lead(conn)
    conn.close()

    rc = set_lead_status_main(
        [
            "--db", str(db), "--key", lead.normalized_key, "--status", "new",
            "--sent-channel", "email", "--sent-message", "Hi Karth,\n\nCould you share the end client?\n\nBest,\nShawn",
        ]
    )
    assert rc == 0
    conn = connect(db)
    row = conn.execute(
        "SELECT status, notes, awaiting_response_since FROM job_leads WHERE normalized_key = ?",
        (lead.normalized_key,),
    ).fetchone()
    assert row["status"] == "new"
    assert "Message sent via email (status unchanged: new)" in row["notes"]
    assert row["awaiting_response_since"]
    out = conn.execute(
        "SELECT direction, body_text FROM job_conversations WHERE job_key = ?", (lead.normalized_key,)
    ).fetchall()
    assert [r["direction"] for r in out] == ["outbound"]
    conn.close()
