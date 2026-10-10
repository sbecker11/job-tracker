"""Tests for the on-demand communications ODT export CLI."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from job_tracker.cli.export_communications import (
    clean_body_for_history,
    collapse_conversations_for_history,
    collect_tracking_log_lines,
    omit_empty_email_husk,
    replace_duplicate_bodies,
    replace_duplicate_job_descriptions,
    main as export_main,
)
from job_tracker.pipeline.models import JobConversation, JobLead
from job_tracker.pipeline.store import add_job_conversation, connect, upsert_lead


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = JobLead(company="Acme", title="Engineer", source_message_id="m1", source_label="single-jd")
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="inbound",
            summary="They reached out",
            body_text="Hi Shawn, we'd love to chat about a role. \u2014 Recruiter",
        ),
    )
    conn.close()
    return db_path


def test_export_writes_odt(seeded_db: Path, tmp_path: Path, capsys):
    out_root = tmp_path / "out"
    rc = export_main(
        ["--db", str(seeded_db), "--company", "Acme", "--title", "Engineer", "--output-root", str(out_root)]
    )
    assert rc == 0

    odt_path = out_root / "Acme" / "Engineer" / "communications" / "Communications_Engineer.odt"
    assert odt_path.is_file()
    assert odt_path.stat().st_size > 0
    assert "Exported 1 conversation" in capsys.readouterr().out
    assert odt_path.suffix == ".odt"

    from job_tracker.cli.export_communications import export_job_history

    result = export_job_history(
        company="Acme", title="Engineer", db_path=seeded_db, output_root=out_root
    )
    assert result["ok"] is True
    assert result["count"] == 1
    assert Path(result["path"]).is_file()

    with zipfile.ZipFile(odt_path) as zf:
        assert zf.namelist()[0] == "mimetype"
        assert zf.read("mimetype") == b"application/vnd.oasis.opendocument.text"
        content = zf.read("content.xml").decode("utf-8")
    assert "Engineer @ Acme" in content
    assert "Hi Shawn" in content
    assert "\u2014" in content or "—" in content  # curly em dash preserved (UTF-8)
    log_path = odt_path.with_suffix(".log")
    assert log_path.is_file()
    assert "No non-human tracking data extracted." in log_path.read_text(encoding="utf-8")


def test_clean_body_strips_quoted_thread_and_vendor_footer():
    body = """Invitation to Your Screening with W3R
Hi Shawn,

You may access the assessment here: https://w3r.ropes.ai/interview/e5e90c

Thanks & Regards
Ramdas Sakthivel | Sr.Technical Recruiter
Arthur Grand Technologies Inc
E: ramdas@arthurgrand.com<mailto:ramdas@arthurgrand.com>

This e-mail is intended solely for the person or entity to which it is addressed.

From: Shawn Becker <shawn.becker@spexture.com>
Sent: Tuesday, September 29, 2026 4:03 PM
To: Ramdas Sakthivel
Subject: Re: Rate Confirmation

confirmed
"""
    cleaned = clean_body_for_history(body, summary="Invitation to Your Screening with W3R")
    assert "https://w3r.ropes.ai/interview/e5e90c" in cleaned
    assert "From: Shawn Becker" not in cleaned
    assert "confirmed" not in cleaned
    assert "mailto:" not in cleaned
    assert "intended solely" not in cleaned


def test_collapse_drops_imap_and_gmail_twins():
    rows = [
        {
            "channel": "email",
            "direction": "inbound",
            "summary": "Reminder from W3R on your screening",
            "body_text": "Reminder from W3R on your screening\nHi Shawn, complete https://w3r.ropes.ai/interview/e5e90c",
            "occurred_at": "2026-09-26T14:37:46+00:00",
            "message_id": "1a0de24e2d9302d9",
            "contact_id": 1,
        },
        {
            "channel": "email",
            "direction": "inbound",
            "summary": "Reminder from W3R on your screening",
            "body_text": "Reminder from W3R on your screening\nHi Shawn,\n\ncomplete https://w3r.ropes.ai/interview/e5e90c",
            "occurred_at": "2026-09-26T14:37:49+00:00",
            "message_id": "imap:<010001a0de24ce94-eb7c4d7d@ropes.ai>",
            "contact_id": 1,
        },
        {
            "channel": "call",
            "direction": "inbound",
            "summary": "Inbound call from Ramdas",
            "body_text": "",
            "occurred_at": "2026-09-24T16:29:40+00:00",
            "message_id": "",
            "contact_id": 1,
        },
    ]
    kept = collapse_conversations_for_history(rows)
    assert len(kept) == 2
    assert kept[0]["message_id"] == "1a0de24e2d9302d9"
    assert kept[1]["channel"] == "call"


def test_collapse_keeps_two_rate_confirmations_hours_apart():
    rows = [
        {
            "channel": "email",
            "summary": "Rate Confirmation",
            "body_text": "Hi Shawn, confirm $65/Hour on W2 for Full Stack Engineer. Right To Represent...",
            "occurred_at": "2026-09-24T16:03:14+00:00",
            "message_id": "imap:<one>",
            "contact_id": 1,
            "direction": "inbound",
        },
        {
            "channel": "email",
            "summary": "Rate Confirmation",
            "body_text": "Hi Shawn, confirm $68/Hour on W2 for Full Stack Engineer. Right To Represent...",
            "occurred_at": "2026-09-24T18:16:45+00:00",
            "message_id": "imap:<two>",
            "contact_id": 1,
            "direction": "inbound",
        },
    ]
    kept = collapse_conversations_for_history(rows)
    assert len(kept) == 2


def test_omit_empty_email_husk_drops_quote_only_replies():
    husk = {
        "channel": "email",
        "summary": "Re: Rate Confirmation",
        "body_text": "On Fri, Sep 25, 2026 at 8:34 AM Richard wrote:\n> Hi Shawn\n",
        "direction": "outbound",
        "occurred_at": "2026-09-24T17:45:20+00:00",
        "message_id": "x",
    }
    note = {
        "channel": "email",
        "summary": "Shawn replied to ramdas@arthurgrand.com that his minimum consulting fee is $75/hr.",
        "body_text": "",
        "direction": "outbound",
        "occurred_at": "2026-09-24T17:51:21+00:00",
        "message_id": "",
    }
    assert omit_empty_email_husk(husk) is True
    assert omit_empty_email_husk(note) is False


def test_replace_duplicate_job_descriptions_keeps_first_only():
    jd = (
        "Job role: Full Stack Engineer\n"
        "Location: Bloomfield, CT - Remote\n"
        "Duration: 6 months+\n\n"
        "Job Description:\n\n"
        "  *   Provides counsel and advice to top management on significant Application Development matters.\n"
        "  *   Acts as the principal designer for complex major systems.\n"
        "  *   Python FastAPI React PostgreSQL AWS Docker Kubernetes LangChain OpenAI embeddings.\n"
        "  *   " + ("x" * 350)
    )
    first = "Please find the Job Description attached.\n\n" + jd
    second = (
        "Hi Shawn, confirm $68/Hour on W2.\n\n"
        "Right To Represent\n\n"
        + jd
    )
    out = replace_duplicate_job_descriptions([first, second])
    assert "Provides counsel" in out[0]
    assert "see first JD above (or below)" in out[1]
    assert "confirm $68/Hour" in out[1]
    assert "Provides counsel" not in out[1]


def test_clean_body_strips_linkedin_inmail_chrome_and_zwsp():
    body = (
        "AI Engineer Salt Lake City\n"
        "I hope you are well. I'm Stephen, Director at PCC ... "
        + ("͏ " * 40)
        + "\nAI Engineer Salt Lake City\n\n"
        "        Stephen Gordon Joughin\n\n"
        "        Reply\n"
        "        https://www.linkedin.com/messaging/thread/2-abc/\n\n"
        "I hope you are well. I'm Stephen, Director at PCC Recruitment.\n"
        "Would you be open to a confidential chat?\n\n"
        "Kind regards,\nStephen\n"
        "----------------------------------------\n"
        "This email was intended for Shawn Becker (Open to Senior Software)\n"
        "Unsubscribe: https://www.linkedin.com/comm/mypreferences\n"
    )
    cleaned = clean_body_for_history(body, summary="AI Engineer Salt Lake City")
    assert "I hope you are well. I'm Stephen, Director at PCC Recruitment." in cleaned
    assert "confidential chat" in cleaned
    assert "intended for Shawn" not in cleaned
    assert "Unsubscribe" not in cleaned
    assert "͏" not in cleaned
    assert "midToken" not in cleaned
    assert "linkedin.com/messaging/thread/" not in cleaned


def test_collect_tracking_log_lines_captures_pixel_and_tokens():
    raw = (
        "Hello" + ("͏" * 12) + "\n"
        "https://www.linkedin.com/help/linkedin/answer/4788?midToken=AQHN1hl0Di2vzA&lipi=urn\n"
        "https://www.linkedin.com/messaging/thread/2-N2UzNDQ3NTgtZjVlMS00YWZh/\n"
        "https://meet.google.com/urm-tesc-nfw?hs=224\n"
        "https://calendar.app.google/5UNV51VEuuNKxwkM8\n"
    )
    lines = collect_tracking_log_lines(raw)
    joined = "\n".join(lines)
    assert "invisible_padding_chars: 12" in joined
    assert "midToken=AQHN1hl0Di2vzA" in joined
    assert "linkedin_thread:" in joined
    assert "meet_tracking_url:" in joined
    assert "calendar.app.google" not in joined
    cleaned = clean_body_for_history(raw, summary="")
    assert "Hello" in cleaned
    assert "meet.google.com/urm-tesc-nfw" in cleaned
    assert "hs=224" not in cleaned
    assert "midToken" not in cleaned
    assert "calendar.app.google/5UNV51VEuuNKxwkM8" in cleaned


def test_export_diverts_tracking_to_log(tmp_path: Path):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    lead = JobLead(company="PCC", title="Staff AI", source_message_id="m1", source_label="single-jd")
    upsert_lead(conn, lead)
    add_job_conversation(
        conn,
        JobConversation(
            job_key=lead.normalized_key,
            direction="inbound",
            summary="AI Engineer Salt Lake City",
            body_text=(
                "I hope you are well. Join https://meet.google.com/urm-tesc-nfw?hs=224 "
                "https://www.linkedin.com/help/linkedin/answer/4788?midToken=AQHN1hl0Di2vzA"
                + ("͏" * 8)
            ),
            message_id="1a0e77c8d70a70ba",
        ),
    )
    conn.close()
    out_root = tmp_path / "out"
    rc = export_main(
        ["--db", str(db_path), "--company", "PCC", "--title", "Staff AI", "--output-root", str(out_root)]
    )
    assert rc == 0
    odt_path = out_root / "PCC" / "Staff_AI" / "communications" / "Communications_Staff_AI.odt"
    log_path = odt_path.with_suffix(".log")
    with zipfile.ZipFile(odt_path) as zf:
        content = zf.read("content.xml").decode("utf-8")
    assert "midToken" not in content
    assert "AQHN1hl0Di2vzA" not in content
    assert "͏" not in content
    assert "meet.google.com/urm-tesc-nfw" in content
    assert "hs=224" not in content
    log_text = log_path.read_text(encoding="utf-8")
    assert "midToken=AQHN1hl0Di2vzA" in log_text
    assert "invisible_padding_chars: 8" in log_text
    assert "meet_tracking_url:" in log_text


def test_collapse_drops_linkedin_gmail_imap_twins_with_chrome():
    gmail = {
        "channel": "email",
        "direction": "inbound",
        "summary": "AI Engineer Salt Lake City",
        "body_text": (
            "I hope you are well. I'm Stephen, Director at PCC ... "
            + ("͏ " * 30)
            + "\nI hope you are well. I'm Stephen, Director at PCC Recruitment. "
            "They are looking for a Staff AI Engineer. Would you be open to a confidential chat?"
        ),
        "occurred_at": "2026-09-29T19:19:07+00:00",
        "message_id": "1a0e77c8d70a70ba",
        "contact_id": 1,
    }
    imap = {
        "channel": "email",
        "direction": "inbound",
        "summary": "AI Engineer Salt Lake City",
        "body_text": (
            "AI Engineer Salt Lake City\n\n        Stephen Gordon Joughin\n"
            "        Reply\n        https://www.linkedin.com/messaging/thread/2-abc/\n\n"
            "I hope you are well. I'm Stephen, Director at PCC Recruitment. "
            "They are looking for a Staff AI Engineer. Would you be open to a confidential chat?\n\n"
            "----------------------------------------\n"
            "This email was intended for Shawn Becker\n"
        ),
        "occurred_at": "2026-09-29T19:19:07+00:00",
        "message_id": "imap:<1420268410.17219714.1790590089512@ltx1-app86696.prod.linkedin.com>",
        "contact_id": 1,
    }
    kept = collapse_conversations_for_history([gmail, imap])
    assert len(kept) == 1
    assert kept[0]["message_id"] == "1a0e77c8d70a70ba"


def test_replace_duplicate_bodies_points_at_first_instance():
    body = (
        "I hope you are well. I'm Stephen, Director at PCC Recruitment. "
        "They are looking for a Staff AI Engineer to set architecture. "
        "Would you be open to a confidential chat about the role?"
    )
    out = replace_duplicate_bodies(
        [body, "prefix\n\n" + body],
        occurred_at=["2026-09-29T19:19:07+00:00", "2026-09-29T19:19:07+00:00"],
        summaries=["AI Engineer Salt Lake City", "AI Engineer Salt Lake City"],
    )
    assert "confidential chat" in out[0]
    assert out[1].startswith("see first instance above (2026-09-29T19:19:07+00:00")
    assert "AI Engineer Salt Lake City" in out[1]


def test_replace_duplicate_bodies_points_appointment_twins():
    invite = (
        "Stephen Gordon Joughin has invited you to a Google Meet.\n"
        "Join: https://meet.google.com/abc-defg-hij\n"
        "When: Thursday, October 1, 2026 2:00 PM Mountain Time\n"
        "Staff AI Engineer — confidential chat with PCC Recruitment."
    )
    out = replace_duplicate_bodies(
        [invite, "You have accepted this invitation.\n\n" + invite],
        occurred_at=["2026-09-30T18:00:00+00:00", "2026-09-30T18:05:00+00:00"],
        summaries=["Invitation: Google Meet", "Accepted: Google Meet"],
    )
    assert "meet.google.com" in out[0]
    assert out[1].startswith("see first instance above (2026-09-30T18:00:00+00:00")


def test_export_no_conversations_is_a_noop(tmp_path: Path, capsys):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(conn, JobLead(company="NoConvos", title="Role", source_message_id="m1", source_label="single-jd"))
    conn.close()

    rc = export_main(
        ["--db", str(db_path), "--company", "NoConvos", "--title", "Role", "--output-root", str(tmp_path / "out")]
    )
    assert rc == 0
    assert "nothing to export" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_export_unknown_job_errors(tmp_path: Path, capsys):
    db_path = tmp_path / "leads.db"
    connect(db_path).close()
    rc = export_main(
        ["--db", str(db_path), "--company", "Nope", "--title", "Nope", "--output-root", str(tmp_path / "out")]
    )
    assert rc == 1
    assert "No job found" in capsys.readouterr().err


def test_all_on_disk_reexports_every_odt(tmp_path: Path, capsys):
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    for company, title, body in (
        ("Acme", "Engineer", "Hi Shawn, we'd love to chat about a role."),
        ("Beta Inc", "Staff AI", "Join https://meet.google.com/abc-defg-hij?hs=224 https://www.linkedin.com/help/x?midToken=ABC"),
    ):
        lead = JobLead(company=company, title=title, source_message_id=title, source_label="single-jd")
        upsert_lead(conn, lead)
        add_job_conversation(
            conn,
            JobConversation(
                job_key=lead.normalized_key,
                direction="inbound",
                summary=title,
                body_text=body,
            ),
        )
    conn.close()
    out_root = tmp_path / "out"
    assert (
        export_main(
            ["--db", str(db_path), "--company", "Acme", "--title", "Engineer", "--output-root", str(out_root)]
        )
        == 0
    )
    assert (
        export_main(
            ["--db", str(db_path), "--company", "Beta Inc", "--title", "Staff AI", "--output-root", str(out_root)]
        )
        == 0
    )
    capsys.readouterr()
    rc = export_main(["--db", str(db_path), "--all-on-disk", "--output-root", str(out_root)])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Batch: 2 exported, 0 failed (2 on-disk ODT(s))." in captured.out
    beta_odt = out_root / "Beta_Inc" / "Staff_AI" / "communications" / "Communications_Staff_AI.odt"
    beta_log = beta_odt.with_suffix(".log")
    with zipfile.ZipFile(beta_odt) as zf:
        content = zf.read("content.xml").decode("utf-8")
    assert "hs=224" not in content
    assert "Non-human mailer tracking was moved" in content
    assert "midToken=ABC" in beta_log.read_text(encoding="utf-8")
