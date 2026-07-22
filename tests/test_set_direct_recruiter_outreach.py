"""Tests for cli/set_direct_recruiter_outreach.py — the one-shot setter the
dashboard's inline tri-state <select> triggers via the setdro:// helper
app (tools/set-direct-recruiter-outreach/)."""

from __future__ import annotations

from pathlib import Path

from job_tracker.cli.set_direct_recruiter_outreach import main
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead


def _seed(conn, **overrides) -> JobLead:
    fields = dict(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd")
    fields.update(overrides)
    lead = JobLead(**fields)
    upsert_lead(conn, lead)
    return lead


def _read_value(db_path: Path, key: str):
    conn = connect(db_path)
    row = conn.execute("SELECT direct_recruiter_outreach FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    conn.close()
    return row["direct_recruiter_outreach"] if row else "MISSING"


def test_sets_yes(tmp_path: Path, capsys):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = _seed(conn)
    conn.close()

    rc = main(["--db", str(db_path), "--key", lead.normalized_key, "--value", "yes"])
    assert rc == 0
    assert _read_value(db_path, lead.normalized_key) == 1
    assert "Set direct_recruiter_outreach='yes'" in capsys.readouterr().out


def test_sets_no(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = _seed(conn)
    conn.close()

    rc = main(["--db", str(db_path), "--key", lead.normalized_key, "--value", "no"])
    assert rc == 0
    assert _read_value(db_path, lead.normalized_key) == 0


def test_sets_undecided_resets_a_decided_lead(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = _seed(conn)
    conn.execute("UPDATE job_leads SET direct_recruiter_outreach = 1 WHERE normalized_key = ?", (lead.normalized_key,))
    conn.commit()
    conn.close()

    rc = main(["--db", str(db_path), "--key", lead.normalized_key, "--value", "undecided"])
    assert rc == 0
    assert _read_value(db_path, lead.normalized_key) is None


def test_unknown_key_errors(tmp_path: Path, capsys):
    db_path = tmp_path / "leads.db"
    connect(db_path).close()

    rc = main(["--db", str(db_path), "--key", "nonexistent::key", "--value", "yes"])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_missing_db_errors(tmp_path: Path, capsys):
    rc = main(["--db", str(tmp_path / "nope.db"), "--key", "acme::engineer", "--value", "yes"])
    assert rc == 1
    assert "No leads DB found" in capsys.readouterr().err


def test_invalid_value_rejected_by_argparse(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    connect(db_path).close()

    try:
        main(["--db", str(db_path), "--key", "acme::engineer", "--value", "maybe"])
        assert False, "argparse should have raised SystemExit for an invalid --value"
    except SystemExit as e:
        assert e.code != 0
