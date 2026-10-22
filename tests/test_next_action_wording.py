"""NEXT-2 — step wording matches the card's actual reply route."""

from __future__ import annotations

import re

from job_tracker.pipeline.pending_workflow import _next_action_clarify


def _numbers(text: str) -> list[int]:
    return [int(n) for n in re.findall(r"\((\d+)\)", text)]


def test_email_card_never_says_linkedin():
    text = _next_action_clarify(
        channel="email", recruiter_name="Pat", gmail_url="https://mail.google.com/x"
    )
    assert "LinkedIn" not in text
    assert "Reply in Gmail to Pat" in text


def test_linkedin_without_link_never_says_open_thread():
    text = _next_action_clarify(channel="linkedin", recruiter_name="Pat")
    assert "Open LinkedIn thread" not in text
    assert "Pat's LinkedIn conversation" in text


def test_linkedin_with_link_opens_thread():
    text = _next_action_clarify(
        channel="linkedin", recruiter_name="Pat", thread_url="https://www.linkedin.com/messaging/thread/1/"
    )
    assert "Open LinkedIn thread" in text
    assert "Paste & send to Pat" in text


def test_linkedin_email_bridge_uses_continuity_copy():
    text = _next_action_clarify(
        channel="linkedin", recruiter_name="Pat", gmail_url="https://mail.google.com/x"
    )
    assert "LinkedIn continuity copy" in text
    assert "Open LinkedIn thread" not in text


def test_steps_are_numbered_without_gaps():
    cases = [
        dict(channel="email"),
        dict(channel="linkedin"),
        dict(channel="linkedin", thread_url="https://www.linkedin.com/messaging/thread/1/"),
        dict(channel="linkedin", gmail_url="https://mail.google.com/x"),
        dict(channel="email", screen_first=True, reply_due=True),
        dict(channel="linkedin", thread_url="https://x", screen_first=True),
        dict(channel="email", reply_due=True, unanswered_days=3),
    ]
    for kw in cases:
        nums = _numbers(_next_action_clarify(**kw))
        assert nums == list(range(1, len(nums) + 1)), (kw, nums)


def test_plan_routes_drive_the_send_step():
    compose = _next_action_clarify(
        channel="email", recruiter_name="Pat", gmail_url="https://mail.google.com/x",
        routes=[{"kind": "compose", "to": "pat@agency.example"}, {"kind": "gmail_thread", "to": "x"}],
    )
    assert "Email Pat (Gmail opens with the reply filled in)" in compose
    assert "Copy reply" not in compose

    thread = _next_action_clarify(
        channel="linkedin", recruiter_name="Pat", routes=[{"kind": "thread", "to": "x", "linkedin": True}]
    )
    assert "(1) Copy reply (2) Open LinkedIn thread (3) Paste & send to Pat (4) Dismiss" in thread

    none = _next_action_clarify(channel="other", routes=[])
    assert "No reply route on file" in none
    for text in (compose, thread, none):
        nums = _numbers(text)
        assert nums == list(range(1, len(nums) + 1))


def test_reply_due_prefix():
    text = _next_action_clarify(channel="email", reply_due=True, unanswered_days=4)
    assert text.startswith("YOUR ACTION: Recruiter followed up (4d unanswered) — reply now. (1)")
