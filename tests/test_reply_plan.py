"""ROUTE-1 reply plan: one server-side decision of channel, recipient and routes."""

from __future__ import annotations

from job_tracker.pipeline.reply_plan import is_office_line, reply_plan

THREAD = "https://www.linkedin.com/messaging/thread/2-abc/"
GMAIL = "https://mail.google.com/mail/u/1/#all/abc"


def _kinds(plan: dict) -> list[str]:
    return [r["kind"] for r in plan["routes"]]


def test_route_order_matches_route_2():
    plan = reply_plan(
        {
            "channel": "email",
            "recruiterName": "Priya Nair",
            "recruiterEmail": "priya.nair@acmestaffing.example",
            "threadUrl": THREAD,
            "recruiterLinkedinUrl": "https://www.linkedin.com/in/priya/",
            "gmailUrl": GMAIL,
            "companyEmail": "careers@bigco.example",
            "hasRecruiter": False,
            "recruiterPhone": "212-555-0100",
        }
    )
    assert _kinds(plan) == ["compose", "thread", "profile", "gmail_thread", "company", "text"]
    assert plan["channel"] == "email"
    assert plan["recipient"]["email"] == "priya.nair@acmestaffing.example"


def test_company_inbox_only_when_no_recruiter_owns_the_lead():
    plan = reply_plan({"companyEmail": "careers@bigco.example", "hasRecruiter": True})
    assert "company" not in _kinds(plan)


def test_linkedin_without_real_email_offers_only_linkedin_routes():
    plan = reply_plan(
        {
            "channel": "linkedin",
            "recruiterEmail": "hit-reply@linkedin.com",
            "emailIsLinkedInRelay": True,
            "threadUrl": THREAD,
            "gmailUrl": GMAIL,
        }
    )
    assert plan["channel"] == "linkedin"
    assert plan["recipient"]["email"] == ""
    assert _kinds(plan) == ["thread"]
    assert plan["routes"][0]["linkedin"] is True


def test_linkedin_without_thread_falls_back_to_the_notification():
    plan = reply_plan({"channel": "linkedin", "emailIsLinkedInRelay": True, "gmailUrl": GMAIL})
    assert _kinds(plan) == ["notification"]


def test_role_mailbox_on_the_lead_is_not_a_compose_route():
    plan = reply_plan({"channel": "email", "recruiterEmail": "careers@bigco.example", "gmailUrl": GMAIL})
    assert "compose" not in _kinds(plan)
    assert plan["recipient"]["email"] == ""


def test_address_quoted_in_jd_is_not_a_route_but_signoff_is():
    jd = "Please apply via jane.doe@bigco.example per the posting.\n" + ("Requirements: Python. " * 60)
    body = jd + "\n\nThanks,\nPriya Nair\npriya.nair@acmestaffing.example\n"
    plan = reply_plan({"channel": "email", "messageBody": body})
    assert plan["recipient"]["email"] == "priya.nair@acmestaffing.example"
    assert plan["recipient"]["via"] == "their signature"

    only_jd = reply_plan({"channel": "email", "messageBody": jd})
    assert "compose" not in _kinds(only_jd)


def test_own_phone_is_never_a_route():
    plan = reply_plan({"recruiterPhone": "+1 857-891-0896"})
    assert plan["routes"] == []


def test_office_line_is_call_only():
    assert is_office_line("212-555-0100 (office)")
    assert is_office_line("212-555-0100", "Thanks,\nPriya\nOffice: 212-555-0100\n")
    assert not is_office_line("212-555-0100 (mobile)")
    plan = reply_plan({"recruiterPhone": "212-555-0100 (office) / 917-555-0199 (mobile)"})
    assert _kinds(plan) == ["call"]
    plan = reply_plan({"recruiterPhone": "917-555-0199 (mobile)"})
    assert _kinds(plan) == ["text"]


def test_subject_and_has_recruiter():
    plan = reply_plan({"company": "Acme", "title": "Data Engineer", "threadUrl": THREAD})
    assert plan["subject"] == "RE: Acme Data Engineer"
    assert plan["hasRecruiter"] is True
    assert reply_plan({})["hasRecruiter"] is False
