"""Regression tests for pipeline/signature.py, built from real (sanitized)
LinkedIn InMail bodies captured during the 2026-07-17 communications
archival work -- see that module's docstring for the two signal sources
being tested here."""

from job_tracker.pipeline.signature import SignatureInfo, parse_signature

WAFERWIRE_BODY = (
    "Exciting opportunity for your skills\r\n"
    "Exciting opportunity for your skills\r\n"
    "Hi Shawn,I hope this message finds you well! As a ... \r\n"
    "Exciting opportunity for your skills\r\n"
    "Exciting opportunity for your skills\r\n"
    "\r\n"
    "      Akshay Saliya\r\n"
    "        Reply\r\n"
    "        https://www.linkedin.com/messaging/thread/2-abc123==/\r\n"
    "\r\n"
    "Hi Shawn,\r\n"
    "\r\n"
    "Our client is looking for someone with your skill set.\r\n"
    "\r\n"
    "Best regards,\r\n"
    "Akshay\r\n"
    "\r\n"
    "Akshay Saliya\r\n"
    "Talent Acquisition Lead\r\n"
    "WaferWire Cloud Technologies\r\n"
    "Email: akshays@waferwire.com | Cell: 425-336-4324\r\n"
    "\r\n"
    "----------------------------------------\r\n"
    "This email was intended for Shawn Becker\r\n"
)

CENTRAPRISE_BODY = (
    "Hiring!!! Data ML Engineer - Scientist (Backfill)\r\n"
    "\r\n"
    "      Manish K.\r\n"
    "        Reply\r\n"
    "        https://www.linkedin.com/messaging/thread/2-def456==/\r\n"
    "\r\n"
    "Hello\r\n"
    "This is Manish from Centraprise\r\n"
    "\r\n"
    "If you are interested please share your updated resume to k.manish@centraprise.com\r\n"
    "\r\n"
    "Thanks and Regards\r\n"
    "Manish Khemnani\r\n"
    "\r\n"
    "Manish Khemnani | Centraprise Global\r\n"
    "Senior Talent Acquisition Specialist \r\n"
    "Desk :   +1 469 639 0667\r\n"
    "Email :  k.manish@centraprise.com\r\n"
)

JUDGE_GROUP_BODY = (
    "Machine Learning Engineer - 6-12+ months - Remote\r\n"
    "\r\n"
    "      Amit Gupta\r\n"
    "        Reply\r\n"
    "        https://www.linkedin.com/messaging/thread/2-ghi789==/\r\n"
    "\r\n"
    "Hi Shawn,\r\n"
    "This is Amit from Judge Group. Opportunity for Machine Learning Engineer.\r\n"
    "\r\n"
    "Amit Gupta\r\n"
    "Segment Lead - BFSI\r\n"
)


def test_parse_signature_extracts_email_and_promotes_full_name():
    info = parse_signature(WAFERWIRE_BODY)
    assert info.email == "akshays@waferwire.com"
    assert info.phone == "425-336-4324"
    assert info.name == "Akshay Saliya"


def test_parse_signature_prefers_full_signature_name_over_truncated_sender_block():
    # The sender block alone would only yield "Manish K." -- the recruiter's
    # own typed signature ("Manish Khemnani | Centraprise Global") is fuller
    # and should win.
    info = parse_signature(CENTRAPRISE_BODY)
    assert info.name == "Manish Khemnani"
    assert info.email == "k.manish@centraprise.com"
    assert info.phone == "+1 469 639 0667"


def test_parse_signature_falls_back_to_sender_block_name_with_no_email_or_phone():
    info = parse_signature(JUDGE_GROUP_BODY)
    assert info.name == "Amit Gupta"
    assert info.email == ""
    assert info.phone == ""


def test_parse_signature_empty_text_returns_falsy_info():
    info = parse_signature("")
    assert not info
    assert info == SignatureInfo()


def test_parse_signature_excludes_shawns_own_email_and_linkedin_domain():
    body = (
        "\r\n      Jane Doe\r\n        Reply\r\n"
        "        https://www.linkedin.com/messaging/thread/2-xyz==/\r\n"
        "To: shawn.becker@spexture.com\r\n"
        "Learn why: https://www.linkedin.com/help/linkedin/answer/4788?x=security@linkedin.com\r\n"
        "Email: jane.recruiter@agency.com\r\n"
    )
    info = parse_signature(body)
    assert info.email == "jane.recruiter@agency.com"


def test_parse_signature_no_sender_block_no_labeled_contact_info():
    info = parse_signature("Just some plain text with no structure at all.")
    assert not info
