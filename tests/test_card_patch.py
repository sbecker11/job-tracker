"""After Apply / Reply sent / decline, only the acted-on lead's card changes."""

from __future__ import annotations

from pathlib import Path

import pytest

from job_tracker.cli.set_lead_status import main as set_lead_status_main
from job_tracker.pipeline import card_patch, pending_workflow
from job_tracker.pipeline.card_patch import NeedsFullRender, patch_lead_card, spawn_card_refresh
from job_tracker.pipeline.models import JobConversation, JobLead
from job_tracker.pipeline.store import add_job_conversation, connect, upsert_lead


@pytest.fixture(autouse=True)
def _no_gmail(monkeypatch):
    monkeypatch.setattr(pending_workflow, "enrich_item_reply_links", lambda item, conn=None: None)


def _lead(conn, company: str, title: str = "Engineer") -> str:
    lead = JobLead(company=company, title=title, source_message_id=f"m-{company}", source_label="linkedin")
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            message_id=f"in-{company}",
            channel="linkedin",
            direction="inbound",
            summary=f"Pitch from {company}",
            occurred_at="2026-11-01T15:00:00+00:00",
            body_text="Hello Shawn,\n\nWould you be open to a quick call?\n\nPat Recruiter",
        ),
    )
    return lead.normalized_key


def _payload(*clarify_keys: str, wait: list[dict] | None = None, send: list[dict] | None = None) -> dict:
    return {
        "pipeline": [
            {"id": "clarify", "count": len(clarify_keys)},
            {"id": "send_resume", "count": len(send or [])},
            {"id": "wait_schedule", "count": len(wait or [])},
            {"id": "decide_apply", "count": 0},
        ],
        "stages": {
            "clarify": [
                {"normalizedKey": k, "stage": "clarify", "replyDue": True, "unansweredDays": 9 - i, "todayRank": i}
                for i, k in enumerate(clarify_keys)
            ],
            "sendResume": send or [],
            "waitSchedule": wait or [],
            "decideApply": {},
        },
        "unmatchedCommunications": [],
    }


def test_closed_lead_is_removed_and_the_rest_reranked(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    pandey = _lead(conn, "Pandey Co")
    other = _lead(conn, "Other Co")
    conn.execute("UPDATE job_leads SET status = 'skipped' WHERE normalized_key = ?", (pandey,))
    conn.commit()
    payload = _payload(pandey, other)

    assert patch_lead_card(conn, pandey, payload=payload) == "removed"

    keys = [c["normalizedKey"] for c in payload["stages"]["clarify"]]
    assert keys == [other]
    assert payload["stages"]["clarify"][0]["todayRank"] == 0
    assert payload["pipeline"][0]["count"] == 1


def test_logged_reply_moves_the_card_to_waiting(tmp_path: Path):
    db = tmp_path / "leads.db"
    conn = connect(db)
    key = _lead(conn, "Acme")
    conn.close()
    assert set_lead_status_main(
        ["--db", str(db), "--key", key, "--status", "pursued", "--sent-channel", "linkedin", "--sent-message", "Hi Pat"]
    ) == 0
    conn = connect(db)
    payload = _payload(key)

    assert patch_lead_card(conn, key, payload=payload) == "wait"

    assert payload["stages"]["clarify"] == []
    card = payload["stages"]["waitSchedule"][0]
    assert card["normalizedKey"] == key
    assert card["stage"] == "wait_schedule"
    assert card["todayTier"] == 6
    assert card["replyPlan"]
    assert payload["pipeline"][2]["count"] == 1


def test_lead_still_owed_a_reply_gets_a_fresh_clarify_card(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn, "Acme")
    payload = _payload(key)

    assert patch_lead_card(conn, key, payload=payload) == "clarify"

    card = payload["stages"]["clarify"][0]
    assert card["normalizedKey"] == key
    assert "Happy to talk" in card["draftReply"]
    assert card["todayTier"] == 1


def test_send_resume_card_falls_back_to_a_full_render(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn, "Acme")
    payload = _payload(send=[{"normalizedKey": key, "stage": "send_resume", "todayRank": 0}])

    with pytest.raises(NeedsFullRender):
        patch_lead_card(conn, key, payload=payload)
    assert payload["stages"]["sendResume"][0]["normalizedKey"] == key


def test_recorded_reply_drops_the_unmatched_card_by_message_id(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn, "Acme")
    conn.execute("UPDATE job_leads SET status = 'skipped' WHERE normalized_key = ?", (key,))
    conn.commit()
    payload = _payload()
    payload["stages"]["clarify"].append({"kind": "unmatched", "messageId": "msg-1", "stage": "clarify"})

    patch_lead_card(conn, key, payload=payload, message_id="msg-1")

    assert payload["stages"]["clarify"] == []


def test_spawn_ignores_databases_other_than_the_real_one(tmp_path: Path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("must not spawn for a test database")

    monkeypatch.setattr(card_patch.subprocess, "Popen", boom)
    spawn_card_refresh(tmp_path / "leads.db", "acme::engineer")
