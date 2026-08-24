"""Tests for pipeline/label_drift.py (Phase 3)."""

from __future__ import annotations

from job_tracker.pipeline.label_drift import compute_label_drift
from job_tracker.pipeline.store import connect, utc_now_iso
from job_tracker.pipeline.triage import SKIP


def test_compute_label_drift_finds_verdict_mismatch(tmp_path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO job_leads (
          normalized_key, company, title, status, verdict, llm_verdict,
          first_seen, last_seen, jd_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("acme|eng", "Acme", "Eng", "new", "pursue", "pass", now, now, "jd"),
    )
    conn.execute(
        """
        INSERT INTO processed_messages (message_id, outcome, label_applied, lead_keys, processed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("msg-1", "pursue", "JobTracker/PURSUE", '["acme|eng"]', now),
    )
    conn.commit()

    entries, checked, skipped = compute_label_drift(conn, {"msg-1": "pursue"})
    conn.close()

    assert checked == 1
    assert skipped == 0
    assert len(entries) == 1
    assert entries[0].desired_outcome == SKIP
    assert entries[0].current_outcome == "pursue"


def test_compute_label_drift_skips_imap_ids(tmp_path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    now = utc_now_iso()
    conn.execute(
        """
        INSERT INTO job_leads (
          normalized_key, company, title, status, verdict, first_seen, last_seen, jd_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("acme|eng", "Acme", "Eng", "new", "pursue", now, now, "jd"),
    )
    conn.execute(
        """
        INSERT INTO processed_messages (message_id, outcome, label_applied, lead_keys, processed_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("imap:<abc>", "pursue", "JobTracker/PURSUE", '["acme|eng"]', now),
    )
    conn.commit()

    entries, checked, skipped = compute_label_drift(conn, {})
    conn.close()

    assert checked == 0
    assert skipped == 1
    assert entries == []
