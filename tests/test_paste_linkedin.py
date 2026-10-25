"""A pasted LinkedIn thread becomes one inbound lead and a Clarify card."""

from __future__ import annotations

from datetime import datetime

from job_tracker.cli.paste_linkedin_lead import read_paste
from job_tracker.pipeline.paste_linkedin import post_pasted_thread
from job_tracker.pipeline.pending_workflow import build_clarify_queue
from job_tracker.pipeline.store import connect, list_job_conversations

PITCH = """
Exciting opportunity in software and data engineering

      Madhvendra Kumar
        Reply
        https://www.linkedin.com/messaging/thread/2-ZDMxMTM2MDUtOTQ2My00M2Q0LWEzZTUtNDNlZDY1YTUwYjNlXzEwMA==/

Hi,

Position: LLM Data Engineer
Type: Contract (Long term)
Location: Remote

Required Skills
	• Data engineering: Production-grade pipeline code
	• AWS data stack: S3, AWS Glue, Spark on EMR, Athena

Best regards,
Madhvendra Kumar
Sr. Technical Recruiter
KPG99,INC
  madhvendra@kpgtech.com   Direct: 6098300389
"""

THIN = """
      Evan T.
        Reply
        https://www.linkedin.com/messaging/thread/2-MjVhMzhmNDEtMzQ3MS00NzVlLWE5ZDUtYmJjM2I2Y2NlMGU5XzEwMA==/

Hey Shawn, we have an opening with one of our clients for an AI Engineer. Please send your resume.

Evan Turner
Technology Recruiter
"""


def _cards(conn):
    return build_clarify_queue(
        linkedin_reply_queue=[],
        unmatched_communications=[],
        age_days_fn=lambda _ts, _now: 0,
        now=datetime.now().astimezone(),
        conn=conn,
    )


def test_paste_creates_a_linkedin_lead_on_the_clarify_queue(tmp_path):
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, PITCH, use_llm=False)

    assert result["ok"] is True
    assert result["created"] is True
    assert result["changed"] is True
    assert "KPG99" in result["company"].upper()
    assert result["title"] == "LLM Data Engineer"
    assert result["threadUrl"].endswith("XzEwMA==/")
    assert result["recruiterName"].startswith("Madhvendra")

    row = conn.execute(
        "SELECT source_label, jd_text FROM job_leads WHERE normalized_key = ?",
        (result["jobKey"],),
    ).fetchone()
    assert row["source_label"] == "linkedin_message"
    assert "Glue" in row["jd_text"]

    convs = list(list_job_conversations(conn, result["jobKey"]))
    assert len(convs) == 1
    assert convs[0]["channel"] == "linkedin"
    assert convs[0]["direction"] == "inbound"
    assert "messaging/thread/" in convs[0]["body_text"]

    cards = [c for c in _cards(conn) if c.get("normalizedKey") == result["jobKey"]]
    assert len(cards) == 1
    assert cards[0]["channel"] == "linkedin"
    assert "messaging/thread/" in cards[0]["threadUrl"]


def test_same_thread_is_not_inserted_twice(tmp_path):
    conn = connect(tmp_path / "leads.db")
    first = post_pasted_thread(conn, PITCH, use_llm=False)
    second = post_pasted_thread(conn, PITCH + "\nThanks,\nMadhvendra\n", use_llm=False)

    assert second["created"] is False
    assert second["changed"] is False
    assert second["jobKey"] == first["jobKey"]
    assert len(list(list_job_conversations(conn, first["jobKey"]))) == 1


def test_thin_pitch_still_becomes_a_card(tmp_path):
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, THIN, use_llm=False)

    assert result["created"] is True
    assert result["title"] == "Role TBD"
    assert result["company"].startswith("End-client TBD via")
    cards = [c for c in _cards(conn) if c.get("normalizedKey") == result["jobKey"]]
    assert len(cards) == 1
    assert "messaging/thread/" in cards[0]["threadUrl"]


def test_messaging_pane_copy_uses_the_opening_name(tmp_path):
    pane = """
Jane Doe
Hi Shawn,

Position: Senior Software Engineer
Location: Remote

We need production Python and AWS Glue.

Jane Doe
Recruiter at Northwind
"""
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, pane, use_llm=False)
    assert result["created"] is True
    assert result["recruiterName"] == "Jane Doe"
    assert result["title"] == "Senior Software Engineer"
    assert result["company"] == "Northwind"
    cards = [c for c in _cards(conn) if c.get("normalizedKey") == result["jobKey"]]
    assert len(cards) == 1
    assert cards[0]["channel"] == "linkedin"


def test_paste_without_a_role_or_recruiter_is_refused(tmp_path):
    conn = connect(tmp_path / "leads.db")
    result = post_pasted_thread(conn, "hello there, nothing to extract from this note at all", use_llm=False)
    assert result["ok"] is False
    assert conn.execute("SELECT COUNT(*) FROM job_leads").fetchone()[0] == 0


def test_read_paste_uses_the_clipboard_at_a_terminal_and_stdin_when_piped():
    assert read_paste(stdin_text="piped", clipboard_text="copied", stdin_is_tty=True) == "copied"
    assert read_paste(stdin_text="piped", clipboard_text="copied", stdin_is_tty=False) == "piped"
