"""EVAL-12, CARD-9, CARD-10: a pitch that states a dealbreaker gets a decline
letter and ranks low in Today, instead of a #1 clarifying reply."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_tracker.pipeline.decline import pitch_dealbreaker
from job_tracker.pipeline.models import UnmatchedMessage
from job_tracker.pipeline.pending_workflow import build_workflow_payload
from job_tracker.pipeline.store import connect, record_unmatched_message

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "render_pending_actions.py"
_spec = importlib.util.spec_from_file_location("render_pending_actions", _SCRIPT_PATH)
render_pending_actions = importlib.util.module_from_spec(_spec)
sys.modules["render_pending_actions"] = render_pending_actions
_spec.loader.exec_module(render_pending_actions)

NOW = datetime(2026, 10, 22, 18, 0, tzinfo=timezone.utc)

ONSITE_PITCH = (
    "Dear,\n\nMy name is Deepak Kumar and I'm a Recruitment Professional at Diverse Lynx.\n\n"
    "Role: Sr. Forward Deployed Engineer\n"
    "Location: Milford, OH OR Santa Clara, CA - Onsite\n\n"
    "Must-Have Skills\n- Vertex AI, Google Cloud, prompt engineering\n\n"
    "Thanks,\nDeepak Kumar\n"
)
OPEN_PITCH = (
    "Hi Shawn,\n\nI'm Prachi Gupta with an exciting contract role.\n"
    "Please reply with your interest.\n\nBest,\nPrachi Gupta\nRecruiter"
)


@pytest.mark.parametrize(
    "text",
    [
        "Location: Milford, OH OR Santa Clara, CA - Onsite",
        "Location: Cleveland, OH (Onsite)",
        "Location :- Arlington, VA 22201 (Onsite)",
    ],
)
def test_out_of_state_onsite_location_is_a_dealbreaker(text):
    hit = pitch_dealbreaker(text, recruiter_name="Deepak Kumar")
    assert hit is not None
    assert "Onsite-only" in hit.label
    assert "100% remote" in hit.letter
    assert hit.letter.startswith("Hi Deepak,")
    assert hit.letter.rstrip().endswith("shawn.becker@spexture.com")


@pytest.mark.parametrize(
    "text",
    [
        "Location: Lehi, UT - Onsite",
        "Location: Salt Lake City, Utah (Onsite)",
        "Location: Denver, CO - Onsite/Hybrid",
        "Location: Remote",
        "Mostly remote with occasional onsite team days.",
    ],
)
def test_utah_hybrid_or_remote_is_not(text):
    assert pitch_dealbreaker(text) is None


def test_c2c_only_keeps_the_clarifier():
    assert pitch_dealbreaker("C2C only. Rate: open. Location: Remote") is None


def _record(conn, mid, sender, subject, body, age_days):
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id=mid,
            direction="inbound",
            from_address=sender,
            to_address="shawnbecker.recruiting@gmail.com",
            subject=subject,
            body_text=body,
            detected_at=(NOW - timedelta(days=age_days)).isoformat(),
        ),
    )


def test_dealbreaker_card_gets_decline_and_ranks_below_replies(tmp_path: Path):
    conn = connect(tmp_path / "leads.db")
    _record(conn, "gmail:onsite", "deepak.kumar@diverselynx.com",
            "Job opportunity :: Sr. Forward Deployed Engineer :: Milford, OH - Onsite", ONSITE_PITCH, 13)
    _record(conn, "gmail:open", "prachi@agency.com", "Exploring a new opportunity together", OPEN_PITCH, 1)
    data = render_pending_actions.render(conn, output_root=tmp_path, now=NOW)
    workflow = build_workflow_payload(data, conn=conn, age_days_fn=render_pending_actions._age_days, now=NOW)
    conn.close()

    clarify = {i["messageId"]: i for i in workflow["stages"]["clarify"] if i.get("messageId")}
    onsite, open_ = clarify["gmail:onsite"], clarify["gmail:open"]

    assert "Onsite-only" in onsite["dealbreaker"]
    assert "isn't a fit" in onsite["draftReply"]
    assert "remote (US)" not in onsite["draftReply"]
    assert "Dealbreaker in the pitch" in onsite["nextAction"]
    assert onsite["todayTier"] == 8
    assert onsite["todayLabel"] == "Decline — dealbreaker in the pitch"

    assert not open_.get("dealbreaker")
    assert open_["todayTier"] == 1
    assert open_["todayRank"] < onsite["todayRank"]
