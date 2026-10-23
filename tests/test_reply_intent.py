"""DRAFT-9: a live-thread reply answers the recruiter's newest message."""

from __future__ import annotations

import re

from job_tracker.pipeline.qualifying_reply import PitchGaps
from job_tracker.pipeline.reply_intent import draft_live_thread_reply, newest_message_text

FRAMEWORK = {"candidate": {"compensation_floor": {"contract_hourly_w2_usd": 75, "permanent_base_usd": 115000}}}
NO_GAPS = PitchGaps(needs_jd=False, needs_end_client=False, needs_engagement=False, needs_remote=False, needs_rate_band=False)


def _draft(text, gaps=NO_GAPS):
    return draft_live_thread_reply(text, first_name="Pat", gaps=gaps, framework=FRAMEWORK)


def test_call_request_offers_to_talk():
    out = _draft("Thanks for the note back here. What's a good time to reach you tomorrow?")
    assert "Happy to talk tomorrow" in out
    assert "+1 857-891-0896" in out


def test_rate_question_with_range_at_floor_says_it_works():
    text = "Client is IBM. We do have a rate range of $90 per hr. on a 1099 or $85 on a W-2. What is your expectation rate?"
    out = _draft(text, PitchGaps(needs_jd=True, needs_end_client=False, needs_engagement=False, needs_remote=False))
    assert "The range you mentioned works for me, and W2 is my preference." in out
    assert "job description" in out
    assert not re.search(r"\$\s?\d", out)


def test_quoted_rate_below_floor_passes_without_a_figure():
    out = _draft("Hi Shawn rate on this is $55/hr on w2 max, Remote no Travel")
    assert "below what I'm targeting" in out
    assert not re.search(r"\$\s?\d|\b55\b", out)


def test_quoted_rate_ok_then_asks_remaining_gaps():
    gaps = PitchGaps(needs_jd=False, needs_end_client=True, needs_engagement=False, needs_remote=False)
    out = _draft("Hi Shawn rate on this is $80/hr on w2 max, Remote no Travel", gaps)
    assert "that rate works for me" in out
    assert "end client" in out


def test_rate_asked_without_figure_defers_to_jd():
    out = _draft("Before I submit you, what are your rate expectations?")
    assert "before settling on a rate" in out


def test_details_pending_keeps_conversation_going():
    text = (
        "The engagement structure (W-2 or 1099) and compensation range have not yet been finalized. "
        "Would you be available to exchange a few messages over the next few days?"
    )
    out = _draft(text)
    assert "keep the conversation going" in out
    assert "not set up for C2C" in out


def test_no_recognized_intent_returns_none():
    assert _draft("Thanks Shawn, I'll get back to you soon.") is None


def test_newest_text_drops_quoted_history():
    body = "Sounds good, talk soon.\n\nOn Mon, Oct 19, 2026 at 4:00 PM Shawn wrote:\n> What's your rate expectation?"
    assert newest_message_text(body) == "Sounds good, talk soon."


def test_rate_reading_uses_range_top_and_skips_bonuses_and_c2c():
    from job_tracker.pipeline.reply_intent import quoted_rate

    assert quoted_rate("$100k-140k base", framework=FRAMEWORK).meets_floor
    assert quoted_rate("$98k-231.278k", framework=FRAMEWORK).meets_floor
    assert not quoted_rate("$80K–$100K+", framework=FRAMEWORK).meets_floor
    assert quoted_rate("$15k relocation bonus", framework=FRAMEWORK) is None
    assert quoted_rate("$57/hr on C2C (All Inclusive)", framework=FRAMEWORK) is None
