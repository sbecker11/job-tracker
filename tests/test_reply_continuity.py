"""Tests for reply-continuity Gmail link resolution."""

from __future__ import annotations

from job_tracker.pipeline.reply_continuity import (
    backfill_inbound_gmail_message_id,
    build_continuity_gmail_queries,
    enrich_item_reply_links,
    resolve_reply_continuity,
    recruiting_gmail_message_url,
)


def test_build_queries_prefer_linkedin_person_and_company():
    qs = build_continuity_gmail_queries(
        recruiter_name="Deven Mehta",
        company="NeoTek (HHS Medicaid / end client TBD)",
        subject="LinkedIn follow-up: Medicaid RRR",
        recruiter_email="Deven.Mehta@neotekus.com",
    )
    assert qs
    assert any("hit-reply@linkedin.com" in q and "Deven Mehta" in q for q in qs)
    assert any("NeoTek" in q for q in qs)
    assert any("from:Deven.Mehta@neotekus.com" in q for q in qs)
    # Corporate email before company-only LinkedIn collision surface.
    email_i = next(i for i, q in enumerate(qs) if "from:Deven.Mehta@neotekus.com" in q)
    company_only = [i for i, q in enumerate(qs) if "NeoTek" in q and "Deven" not in q and "Medicaid" not in q]
    if company_only:
        assert email_i < company_only[0]


def test_company_only_linkedin_hit_rejected_without_subject_overlap():
    """Artech Data Engineer must not inherit Vibhor's GRC InMail links."""

    def search(query: str, limit: int) -> list[str]:
        if "Artech" in query and "hit-reply" in query:
            return ["19f86b8f747f4ea1"]
        return []

    def body(message_id: str) -> str:
        return (
            "Subject: GRC Software Engineer needed with our major Technology end-client\n"
            "GRC Software Engineer needed with our major Technology end-client "
            "https://www.linkedin.com/messaging/thread/2-NjViMmE1YTctOGEwMS00NTNiLTkxMDUtNGRkNDFkYmUyMDczXzEwMA==/\n"
            "This email was intended for Shawn Becker (Open to Senior Software, Full Stack, & Data Engineer · AI/ML Engineer)"
        )

    resolved = resolve_reply_continuity(
        recruiter_name="",
        company="Artech",
        subject="100% Remote Position | Position: AI & Data - Data Engineer | Long Term Contract",
        recruiter_email="Kashish.Thakur@artech.com",
        search_gmail=search,
        fetch_body=body,
    )
    assert resolved["gmailUrl"] == ""
    assert resolved["threadUrl"] == ""
    assert resolved["messageId"] == ""


def test_body_matches_lead_subject_uses_email_subject_not_footer():
    from job_tracker.pipeline.reply_continuity import body_matches_lead_subject

    subj = "AI & Data - Data Engineer | Long Term Contract"
    grc = (
        "Subject: GRC Software Engineer needed\n"
        "Hello\n"
        "This email was intended for Shawn Becker (Open to Senior Software, Full Stack, & Data Engineer · AI/ML Engineer)"
    )
    good = "Subject: 100% Remote Position | AI & Data - Data Engineer\nHello"
    assert body_matches_lead_subject(grc, subj) is False
    assert body_matches_lead_subject(good, subj) is True
    assert body_matches_lead_subject("Data Engineer remote role", subj) is True
    assert body_matches_lead_subject("GRC Software Engineer onsite", subj) is False


def test_resolve_uses_injected_gmail_search():
    def search(query: str, limit: int) -> list[str]:
        if "Deven Mehta" in query:
            return ["19fb2d71933ae946"]
        return []

    def body(message_id: str) -> str:
        assert message_id == "19fb2d71933ae946"
        return (
            "Subject: AWS AI Engineer\n"
            "AWS AI Engineer follow-up "
            "Reply https://www.linkedin.com/messaging/thread/2-abc123/ more text"
        )

    resolved = resolve_reply_continuity(
        recruiter_name="Deven Mehta",
        company="NeoTek",
        subject="AWS AI Engineer",
        search_gmail=search,
        fetch_body=body,
    )
    assert "19fb2d71933ae946" in resolved["gmailUrl"]
    assert "AccountChooser" in resolved["gmailUrl"]
    assert "linkedin.com/messaging/thread/2-abc123" in resolved["threadUrl"]
    assert resolved["messageId"] == "19fb2d71933ae946"


def test_enrich_item_fills_gmail_and_thread():
    item = {
        "recruiterName": "Deven Mehta",
        "company": "NeoTek",
        "title": "AWS AI Engineer",
        "channel": "email",
        "gmailUrl": "",
        "threadUrl": "",
        "messageId": "",
    }

    def search(query: str, limit: int) -> list[str]:
        return ["aabbccddeeff0011"]

    def body(message_id: str) -> str:
        return "Subject: AWS AI Engineer\nAWS AI Engineer — no thread url here"

    enrich_item_reply_links(item, search_gmail=search, fetch_body=body)
    assert item["gmailUrl"] == recruiting_gmail_message_url("aabbccddeeff0011")
    assert item["messageId"] == "aabbccddeeff0011"


def test_backfill_inbound_empty_message_id(tmp_path, monkeypatch):
    from job_tracker.pipeline.models import JobConversation, JobLead
    from job_tracker.pipeline.store import add_job_conversation, connect, upsert_lead

    db = tmp_path / "t.db"
    conn = connect(db)
    lead = JobLead(company="NeoTek", title="AWS AI Engineer", source_message_id="x", source_label="linkedin_message")
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="inbound",
            summary="follow-up",
            occurred_at="2026-07-30T11:43:00+00:00",
            message_id="",
            body_text="hi",
        ),
    )
    assert backfill_inbound_gmail_message_id(conn, lead.normalized_key, "19fb2d71933ae946") is True
    row = conn.execute(
        "SELECT message_id FROM job_conversations WHERE job_key=?",
        (lead.normalized_key,),
    ).fetchone()
    assert row["message_id"] == "19fb2d71933ae946"
    conn.close()
