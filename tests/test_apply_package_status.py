"""A generated package moves the lead to package_generated, or says why not
(REQUIREMENTS LIFE-5)."""

from __future__ import annotations

from pathlib import Path

from job_tracker.cli.apply_package import _record_package
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import advance_status, connect, get_lead_status, upsert_lead

KEY = "acme::data engineer"


def _conn(tmp_path: Path, status: str):
    conn = connect(tmp_path / "leads.db")
    upsert_lead(conn, JobLead(company="Acme", title="Data Engineer", source_message_id="m1", source_label="single-jd"))
    advance_status(conn, KEY, status, force=True)
    return conn


def test_pursued_lead_moves(tmp_path: Path):
    conn = _conn(tmp_path, "pursued")
    _record_package(conn, KEY, force=False)
    assert get_lead_status(conn, KEY) == "package_generated"


def test_skipped_lead_moves_only_with_force(tmp_path: Path, capsys):
    conn = _conn(tmp_path, "skipped")
    _record_package(conn, KEY, force=False)
    assert get_lead_status(conn, KEY) == "skipped"
    assert "stays skipped" in capsys.readouterr().err

    _record_package(conn, KEY, force=True)
    assert get_lead_status(conn, KEY) == "package_generated"
    notes = conn.execute("SELECT notes FROM job_leads WHERE normalized_key = ?", (KEY,)).fetchone()[0]
    assert "moved from skipped" in notes


def test_applied_lead_is_not_moved_back(tmp_path: Path):
    conn = _conn(tmp_path, "applied")
    _record_package(conn, KEY, force=True)
    assert get_lead_status(conn, KEY) == "applied"
