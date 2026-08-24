"""Tests for Monday v1 decision report."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from job_tracker.cli.monday_report import (
    _interview_likelihood_score,
    build_kpi_counts,
    build_monday_snapshot,
    render_text,
)
from job_tracker.pipeline.store import connect, utc_now_iso


def test_interview_likelihood_prefers_direct_recruiter():
    cold = {"direct_recruiter_outreach": False, "llm_match_pct": 90, "age_days": 1}
    direct = {"direct_recruiter_outreach": True, "llm_match_pct": 70, "age_days": 1}
    assert _interview_likelihood_score(direct) > _interview_likelihood_score(cold)


def test_build_monday_snapshot_counts(tmp_path: Path):
    db = tmp_path / "leads.db"
    state = tmp_path / "state"
    state.mkdir()
    (state / "last_ok_cycle").write_text(str(int(datetime.now(timezone.utc).timestamp())))
    output = tmp_path / "packages"
    output.mkdir()

    conn = connect(db)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO job_leads (
          normalized_key, company, title, status, match_pct, verdict,
          llm_verdict, llm_match_pct, first_seen, last_seen,
          direct_recruiter_outreach, jd_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "acme|senior engineer",
            "Acme",
            "Senior Engineer",
            "package_generated",
            80.0,
            "pursue",
            "pursue",
            85.0,
            now,
            now,
            1,
            "JD text here",
        ),
    )
    pkg = output / "Acme" / "Senior Engineer"
    pkg.mkdir(parents=True)
    (pkg / "Shawn_Becker_Resume_Acme.docx").write_bytes(b"PK")
    (pkg / "Shawn_Becker_Cover_Letter_Acme.docx").write_bytes(b"PK")
    conn.execute(
        """
        INSERT INTO unmatched_messages (
          message_id, detected_at, subject, from_address
        ) VALUES (?, ?, ?, ?)
        """,
        ("mid-1", now, "Re: role", "recruiter@example.com"),
    )
    conn.commit()
    conn.close()

    snap = build_monday_snapshot(
        db_path=db,
        output_root=output,
        state_dir=state,
        top_n=5,
    )
    assert snap["counts"]["packagesReady"] == 1
    assert snap["counts"]["unmatchedCommunications"] == 1
    assert snap["schedule"]["halted"] is False
    text = render_text(snap)
    assert "MONDAY v1" in text
    assert "Acme" in text


def test_build_kpi_counts_lightweight(tmp_path: Path):
    db = tmp_path / "leads.db"
    state = tmp_path / "state"
    state.mkdir()
    (state / "last_ok_cycle").write_text(str(int(datetime.now(timezone.utc).timestamp())))

    conn = connect(db)
    conn.commit()
    conn.close()

    kpi = build_kpi_counts(db_path=db, output_root=tmp_path / "packages", state_dir=state)
    assert "counts" in kpi
    assert kpi["counts"]["unmatchedCommunications"] == 0
    assert kpi["schedule"]["halted"] is False
