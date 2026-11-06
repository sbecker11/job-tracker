from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from job_tracker.pipeline import llm_reply


class FakeClient:
    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.replies.pop(0)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0),
        )


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("JOB_TRACKER_LLM_REPLY", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")  # pragma: allowlist secret
    monkeypatch.setattr(llm_reply, "_profile_text", lambda: "Shawn Becker profile")


def _card(**extra) -> dict:
    card = {
        "kind": "unmatched",
        "stage": "clarify",
        "messageId": "m-1",
        "recruiterName": "Aman Mishra",
        "company": "Adobe",
        "title": "Data Architect",
        "channel": "email",
        "subject": "Data Architect with Adobe: 100% Remote, W2 only",
        "messageBody": "Data Architect, 100% remote, W2 only, long term.",
        "draftReply": "Hi Aman,\n\nWho is the end client? W2 or 1099?\n\nBest, Shawn",
    }
    card.update(extra)
    return card


GOOD = "Hi Aman,\n\nI'd like to be submitted for the Adobe Data Architect role.\n\nBest,\nShawn"


def test_llm_reply_replaces_the_template_and_is_cached(conn):
    card = _card()
    client = FakeClient(GOOD)
    assert llm_reply.apply_llm_drafts([card], conn, client=client) == 1
    assert card["draftReply"] == GOOD
    assert card["draftSource"] == "llm"
    assert card["templateDraft"].startswith("Hi Aman,\n\nWho is the end client")
    prompt = client.calls[0]["messages"][0]["content"]
    assert "W2 only" in prompt and "Data Architect" in prompt

    again = _card()
    assert llm_reply.apply_llm_drafts([again], conn, client=FakeClient()) == 1
    assert again["draftReply"] == GOOD


def test_new_inbound_message_invalidates_the_cache(conn):
    llm_reply.apply_llm_drafts([_card()], conn, client=FakeClient(GOOD))
    changed = _card(messageBody="Update: the client wants a call Friday.")
    client = FakeClient("Hi Aman,\n\nFriday works.\n\nBest,\nShawn")
    llm_reply.apply_llm_drafts([changed], conn, client=client)
    assert len(client.calls) == 1
    assert "Friday works" in changed["draftReply"]


def test_rate_figure_is_rewritten_then_falls_back_to_template(conn):
    card = _card()
    bad = "Hi Aman,\n\nMy rate is $70/hr.\n\nBest,\nShawn"
    client = FakeClient(bad, bad)
    assert llm_reply.apply_llm_drafts([card], conn, client=client) == 0
    assert len(client.calls) == 2
    assert card["draftSource"] == "template"
    assert "end client" in card["draftReply"]


def test_greeting_line_follows_the_house_rule(conn):
    card = _card(recruiterName="Dr. Md Mostafizur Rahman")
    llm_reply.apply_llm_drafts([card], conn, client=FakeClient("Hi Mostafizur,\n\nThanks.\n\nBest,\nShawn"))
    assert card["draftReply"].startswith("Hello, Dr. Rahman.\n")


def test_decline_and_disabled_cards_keep_the_template(conn, monkeypatch):
    declined = _card(dealbreaker="Onsite-only")
    assert llm_reply.apply_llm_drafts([declined], conn, client=FakeClient()) == 0
    assert declined["draftSource"] == "template"

    monkeypatch.setenv("JOB_TRACKER_LLM_REPLY", "0")
    card = _card()
    assert llm_reply.apply_llm_drafts([card], conn, client=FakeClient()) == 0
    assert card["draftReply"].startswith("Hi Aman,\n\nWho is")


@pytest.mark.parametrize(
    "text",
    [
        "Can't speak to rate, but engagement-wise I'm good with W2.",
        "I can't speak to compensation here.",
        "I'm an employee of Cricut directly, so happy to move forward.",
        "On linkage-engine (my own Java/Spring project) I wrote the full Terraform IaC layer.",
        "I designed and built the Terraform-managed AWS infrastructure for linkage-engine.",
        "I'm already engaged with Adobe directly on their SEO platform.",
        "Since Cricut is the hiring company, I assume this is a full-time role.",
        "I designed and directed linkage-engine, and I hand-coded a React frontend in sushi-rag-mcp-server.",
        "For a call, I'm open this week on Thursday or Friday afternoon, Mountain Time.",
    ],
)
def test_bad_claims_are_violations(text):
    assert llm_reply._violations(text)


@pytest.mark.parametrize(
    "text",
    [
        "linkage-engine - Spring AI/Bedrock agentic pipeline I architected.",
        "At SeniorLink I built a schema-governed event pipeline.",
        "I designed and directed AI coding agents to build linkage-engine.",
        "I built resume-flyer, a Vue 3 SPA.",
        "Most recently I worked at Adobe on their SEO platform.",
    ],
)
def test_true_claims_pass(text):
    assert llm_reply._violations(text) == []


def test_send_package_card_gets_an_llm_note_that_may_say_attached(conn):
    card = _card(
        stage="send_resume",
        recruiterName="",
        senders=[{"name": "smcleod@searchservices.com"}, {"name": "Scott McLeod"}],
    )
    note = "Hi Scott,\n\nMy résumé and cover letter are attached.\n\nBest,\nShawn"
    client = FakeClient(note)
    assert llm_reply.apply_llm_drafts([card], conn, client=client) == 1
    assert card["draftReply"] == note
    assert "ARE attached" in client.calls[0]["messages"][0]["content"]


def test_follow_up_due_wait_card_gets_an_llm_follow_up(conn):
    card = _card(stage="wait_schedule", followUpDue=True, followupDraft="Hi Aman,\n\nJust checking in.\n\nBest, Shawn")
    quiet = _card(stage="wait_schedule", messageId="m-2", followupDraft="Hi,\n\nJust checking in.")
    note = "Hi Aman,\n\nStill very interested in the Adobe role.\n\nBest,\nShawn"
    client = FakeClient(note)
    assert llm_reply.apply_llm_drafts([card, quiet], conn, client=client) == 1
    assert card["followupDraft"] == note and card["followupSource"] == "llm"
    assert card["templateFollowup"].endswith("Just checking in.\n\nBest, Shawn")
    assert card["draftReply"].startswith("Hi Aman,\n\nWho is")
    assert quiet["followupSource"] == "template"
    assert "following up" in client.calls[0]["messages"][0]["content"]


def test_attached_resume_needs_the_recruiter_to_ask(conn):
    text = "I've attached my résumé. Happy to talk this week."
    unasked = llm_reply.build_context(conn, _card(messageBody="Data Architect, remote, W2 only."))
    asked = llm_reply.build_context(conn, _card(messageBody="Please send your updated resume."))
    assert llm_reply._violations(text, unasked)
    assert llm_reply._violations(text, asked) == []
