"""Tests for spend_report (Phase 4)."""

from __future__ import annotations

from job_tracker.cli.spend_report import build_spend_summary
from job_tracker.pipeline.store import connect, utc_now_iso


def test_build_spend_summary(tmp_path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO job_leads (
          normalized_key, company, title, status, verdict, llm_verdict,
          llm_eval_cost_usd, first_seen, last_seen, jd_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("a|b", "A", "B", "new", "pursue", "pursue", 0.05, now, now, "jd"),
    )
    conn.commit()
    summary = build_spend_summary(conn)
    conn.close()
    assert summary["totalEvalCostUsd"] == 0.05
    assert summary["pursueLeads"] == 1
