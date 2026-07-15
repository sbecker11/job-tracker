"""Tests for scripts/render_pending_actions.py's age-based display/sort
(added 2026-07-15: a lead's value decays the longer it sits unreviewed, so
the pending-actions page needs to show + default-sort by days-since-received).

render_pending_actions.py lives in scripts/, not src/job_tracker/, so it
isn't on pytest's `pythonpath = ["src"]` — loaded here via importlib instead
of a normal import.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import connect, upsert_lead

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_pending_actions.py"
_spec = importlib.util.spec_from_file_location("render_pending_actions", _SCRIPT_PATH)
render_pending_actions = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("render_pending_actions", render_pending_actions)
assert _spec.loader is not None
_spec.loader.exec_module(render_pending_actions)


NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)


def test_age_days_computes_whole_days_since_first_seen():
    ten_days_ago = (NOW - timedelta(days=10)).isoformat()
    assert render_pending_actions._age_days(ten_days_ago, NOW) == 10


def test_age_days_zero_for_a_lead_seen_today():
    assert render_pending_actions._age_days(NOW.isoformat(), NOW) == 0


def test_age_days_handles_missing_or_malformed_value_without_raising():
    assert render_pending_actions._age_days(None, NOW) == 0
    assert render_pending_actions._age_days("", NOW) == 0
    assert render_pending_actions._age_days("not-a-date", NOW) == 0


def test_age_days_treats_naive_timestamp_as_utc():
    naive_five_days_ago = (NOW - timedelta(days=5)).replace(tzinfo=None).isoformat()
    assert render_pending_actions._age_days(naive_five_days_ago, NOW) == 5


def _make_lead(conn, *, company: str, title: str, match_pct: float, verdict: str, first_seen: str) -> None:
    lead = JobLead(
        company=company,
        title=title,
        source_message_id=f"test-{company}-{title}",
        source_label="test",
        match_pct=match_pct,
        verdict=verdict,
        jd_text="some jd text",
    )
    upsert_lead(conn, lead)
    conn.execute(
        "UPDATE job_leads SET first_seen = ? WHERE normalized_key = ?", (first_seen, lead.normalized_key)
    )
    conn.commit()


def test_render_sorts_pending_review_oldest_first_by_default(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    _make_lead(
        conn, company="Newer Co", title="Engineer", match_pct=80.0, verdict="pursue",
        first_seen=(NOW - timedelta(days=2)).isoformat(),
    )
    _make_lead(
        conn, company="Older Co", title="Engineer", match_pct=40.0, verdict="review",
        first_seen=(NOW - timedelta(days=20)).isoformat(),
    )

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    companies_in_order = [entry["company"] for entry in data["pending_review"]]
    assert companies_in_order == ["Older Co", "Newer Co"]
    assert data["pending_review"][0]["ageDays"] == 20
    assert data["pending_review"][1]["ageDays"] == 2


def test_render_populates_age_days_on_every_new_lead_bucket(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    _make_lead(
        conn, company="Auto Skip Co", title="Engineer", match_pct=10.0, verdict="pass",
        first_seen=(NOW - timedelta(days=3)).isoformat(),
    )
    _make_lead(
        conn, company="Unresolved Co", title="Engineer", match_pct=0.0, verdict="review",
        first_seen=(NOW - timedelta(days=7)).isoformat(),
    )

    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    conn.close()

    assert data["auto_skipped"][0]["ageDays"] == 3
    assert data["unresolved"][0]["ageDays"] == 7
