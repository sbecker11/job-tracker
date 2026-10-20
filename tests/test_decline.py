"""Tests for declining a lead (letter, reasoned skip, post-decline reply card)."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_tracker.cli import decline_lead
from job_tracker.pipeline.decline import (
    DECLINE_REASONS,
    draft_decline_letter,
    record_decline,
    terms_changed,
)
from job_tracker.pipeline.models import JobContact, JobConversation, JobLead
from job_tracker.pipeline.pending_workflow import build_workflow_payload
from job_tracker.pipeline.store import add_job_contact, add_job_conversation, connect, upsert_lead

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_pending_actions.py"
_spec = importlib.util.spec_from_file_location("render_pending_actions", _SCRIPT_PATH)
render_pending_actions = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("render_pending_actions", render_pending_actions)
assert _spec.loader is not None
_spec.loader.exec_module(render_pending_actions)

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc)


def _lead(conn, *, company="Acme", title="Data Engineer", status_days_ago=10):
    lead = JobLead(
        company=company,
        title=title,
        source_message_id=f"m-{company}",
        source_label="linkedin_message",
        jd_text="python aws",
        match_pct=75.0,
        verdict="pursue",
    )
    upsert_lead(conn, lead)
    seen = (NOW - timedelta(days=status_days_ago)).isoformat()
    conn.execute(
        "UPDATE job_leads SET first_seen = ?, last_seen = ? WHERE normalized_key = ?",
        (seen, seen, lead.normalized_key),
    )
    conn.commit()
    add_job_contact(conn, JobContact(job_key=lead.normalized_key, name="Ed Soucie", role="recruiter"))
    return lead.normalized_key


@pytest.mark.parametrize("reason", [r for r in DECLINE_REASONS if r.code != "other"], ids=lambda r: r.code)
def test_every_letter_follows_reply_house_rules(reason):
    letter = draft_decline_letter(reason.code, recruiter_name="Ed Soucie", company="Acme", title="Data Engineer")
    assert letter.startswith("Hi Ed,\n\nThank you for reaching out about the Data Engineer role at Acme.")
    assert reason.sentence in letter
    assert "keep me in mind for future roles" in letter
    assert letter.endswith("Best,\nShawn")
    assert "$" not in letter and "/hr" not in letter.lower()
    for banned in ("citizen", "sponsor", "visa", "authoriz", "available"):
        assert banned not in letter.lower()


def test_company_ending_in_a_period_is_not_doubled():
    letter = draft_decline_letter("not_qualified", company="Next Level, Inc.", title="Lead")
    assert "role at Next Level, Inc. After reviewing" in letter


def test_other_reason_uses_the_note_and_requires_one():
    letter = draft_decline_letter("other", recruiter_name="", note="I've just accepted another offer.")
    assert letter.startswith("Hi there,\n\nThank you for reaching out.")
    assert "I've just accepted another offer." in letter
    with pytest.raises(ValueError):
        draft_decline_letter("other")


def test_terms_changed_matches_the_declined_on_term():
    assert terms_changed("rate_too_low", "Good news, the client raised the budget")
    assert not terms_changed("rate_too_low", "Thanks, take care")
    assert terms_changed("location", "Update: this one can be fully remote now")
    assert not terms_changed("other", "remote rate budget")


def test_record_decline_logs_letter_skips_and_stores_reason(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn)
    when = (NOW - timedelta(days=3)).isoformat()
    record_decline(conn, key, code="period_too_short", letter="Hi Ed, no thanks", channel="linkedin", when=when)
    row = conn.execute(
        "SELECT status, decline_reason, declined_at, awaiting_response_since, notes FROM job_leads "
        "WHERE normalized_key = ?",
        (key,),
    ).fetchone()
    assert row["status"] == "skipped"
    assert row["decline_reason"] == "period_too_short"
    assert row["declined_at"] == when
    assert row["awaiting_response_since"] is None
    assert "Declined new -> skipped (letter sent): Period too short" in row["notes"]
    out = conn.execute(
        "SELECT direction, channel, summary FROM job_conversations WHERE job_key = ? AND message_id LIKE 'decline:%'",
        (key,),
    ).fetchone()
    assert (out["direction"], out["channel"], out["summary"]) == ("outbound", "linkedin", "Declined (Period too short)")
    conn.close()


def test_cli_no_letter_skips_without_logging_a_conversation(tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    key = _lead(conn)
    conn.close()

    assert decline_lead.main(["--db", str(db), "--key", key, "--reason", "no_travel", "--print-letter"]) == 0
    printed = capsys.readouterr().out
    assert "Hi Ed," in printed and "on-site travel" in printed

    assert decline_lead.main(["--db", str(db), "--key", key, "--reason", "no_travel", "--no-letter"]) == 0
    conn = connect(db)
    row = conn.execute("SELECT status, decline_reason FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    assert (row["status"], row["decline_reason"]) == ("skipped", "no_travel")
    assert conn.execute(
        "SELECT COUNT(*) FROM job_conversations WHERE message_id LIKE 'decline:%'"
    ).fetchone()[0] == 0
    conn.close()


def _payload(conn, tmp_path):
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    return build_workflow_payload(
        data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW, output_root=tmp_path
    )


def test_reply_after_decline_surfaces_as_clarify_card(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn)
    record_decline(
        conn, key, code="rate_too_low", letter="Hi Ed, no thanks", channel="linkedin",
        when=(NOW - timedelta(days=5)).isoformat(),
    )
    add_job_conversation(
        conn,
        JobConversation(
            job_key=key,
            message_id="imap:<after@li>",
            channel="linkedin",
            direction="inbound",
            summary="Re: Data Engineer",
            occurred_at=(NOW - timedelta(days=2)).isoformat(),
            body_text="Hi Shawn, the client can raise the rate. Still interested?\n\nEd",
        ),
    )
    workflow = _payload(conn, tmp_path)
    conn.close()

    card = next(i for i in workflow["stages"]["clarify"] if i["normalizedKey"] == key)
    assert card["postDecline"] is True
    assert card["declineReasonLabel"] == "Rate too low"
    assert card["termsChanged"] is True
    assert card["replyDue"] is True
    assert card["todayTier"] == 1
    assert "replied after you declined (Rate too low)" in card["nextAction"]
    assert card["draftReply"].endswith("Best,\nShawn")
    assert "declineGreeting" not in card
    assert [r["code"] for r in workflow["declineReasons"]][0] == "not_qualified"
    assert "keep me in mind" in workflow["declineClosing"]


def test_declined_lead_without_a_reply_stays_off_the_list(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    key = _lead(conn)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=key,
            message_id="imap:<pitch@li>",
            channel="linkedin",
            direction="inbound",
            summary="Data Engineer",
            occurred_at=(NOW - timedelta(days=6)).isoformat(),
            body_text="Hi Shawn, 3-month contract. Interested?\n\nEd",
        ),
    )
    record_decline(
        conn, key, code="period_too_short", letter="Hi Ed, no thanks",
        when=(NOW - timedelta(days=5)).isoformat(),
    )
    workflow = _payload(conn, tmp_path)
    conn.close()

    for items in workflow["stages"].values():
        if isinstance(items, list):
            assert all(i.get("normalizedKey") != key for i in items if isinstance(i, dict))
