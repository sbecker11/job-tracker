"""Tests for triage_imap_inbox CLI with IMAP + triage mocked."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from job_tracker.cli import triage_imap_inbox as imap_cli
from job_tracker.cli.triage_imap_inbox import main as imap_main
from job_tracker.email.models import EmailMessage, ExtractedRole
from job_tracker.pipeline.comms_match import MatchOutcome
from job_tracker.pipeline.llm_apply import EvaluationResult, TwoTierPackageResult
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import add_job_contact, add_job_conversation, connect, upsert_lead
from job_tracker.pipeline.models import JobContact, JobConversation
from job_tracker.pipeline.triage import MessageTriageResult, RoleOutcome, NEEDS_REVIEW, PURSUE, SKIP
from job_tracker.scoring.scorer import ScoreResult


def _msg(
    mid: str = "imap:<msg-1>",
    *,
    from_address: str = "recruiter@acme.example",
    thread_id: str = "",
    subject: str = "Software Engineer @ Acme",
    body_plain: str = "We are hiring.",
) -> EmailMessage:
    return EmailMessage(
        id=mid,
        from_address=from_address,
        subject=subject,
        body_plain=body_plain,
        thread_id=thread_id,
        label_ids=["INBOX"],
    )


def _role(verdict: str = "pursue", resume: Path | None = None) -> RoleOutcome:
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="imap:<msg-1>", source_label="single-jd", jd_text="JD text")
    package = TwoTierPackageResult(
        no_llm_score=ScoreResult(match_pct=80.0, verdict=verdict),
        evaluation=EvaluationResult(verdict=verdict, match_pct=80.0, rationale="ok"),
        resume_path=resume,
        cover_letter_path=resume,
        jd_path=Path("/tmp/Acme/jd.docx") if resume else None,
        full_llm_review_path=Path("/tmp/Acme/review.docx") if resume else None,
        warnings=[],
    )
    return RoleOutcome(lead=lead, package=package)


def _result(outcome: str = PURSUE, *, mid: str = "imap:<msg-1>", roles: list[RoleOutcome] | None = None) -> MessageTriageResult:
    return MessageTriageResult(
        message_id=mid,
        subject="Software Engineer @ Acme",
        from_address="recruiter@acme.example",
        outcome=outcome,
        reason="test",
        classifier_label="single-jd",
        roles=roles if roles is not None else [_role()],
        extraction_complete=True,
        extraction_issue="",
    )


@pytest.fixture()
def mock_imap(monkeypatch):
    fake_conn = SimpleNamespace(name="fake-imap-conn", logout=lambda: None)
    monkeypatch.setattr(
        imap_cli,
        "ImapAccount",
        SimpleNamespace(from_env=lambda prefix: SimpleNamespace(host="h", port=993, user="u", password="p")),  # pragma: allowlist secret
    )
    monkeypatch.setattr(imap_cli, "connect", lambda account: fake_conn)
    monkeypatch.setattr(imap_cli, "ensure_folder", lambda conn, folder: None)
    moves: list[dict] = []
    monkeypatch.setattr(
        imap_cli,
        "move_message",
        lambda conn, uid, *, from_folder, to_folder: moves.append({"uid": uid, "from": from_folder, "to": to_folder}),
    )
    return SimpleNamespace(conn=fake_conn, moves=moves)


def test_triage_imap_no_messages(monkeypatch, mock_imap, tmp_path: Path, capsys):
    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: [])
    rc = imap_main(["--dry-run", "--db", str(tmp_path / "leads.db")])
    assert rc == 0
    assert "No messages" in capsys.readouterr().err


def test_triage_imap_dry_run_prints_and_writes_nothing(monkeypatch, mock_imap, tmp_path: Path, capsys):
    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["1"])
    monkeypatch.setattr(imap_cli, "fetch_message", lambda *a, **k: _msg())
    monkeypatch.setattr(imap_cli, "triage_message", lambda *a, **k: _result(roles=[_role(resume=Path("/t/r.docx"))]))

    rc = imap_main(["--dry-run", "--db", str(tmp_path / "leads.db")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[PURSUE]" in out
    assert "dry run" in out
    assert mock_imap.moves == []


def test_triage_imap_live_moves_and_stores(monkeypatch, mock_imap, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["1"])
    monkeypatch.setattr(imap_cli, "fetch_message", lambda *a, **k: _msg())
    monkeypatch.setattr(imap_cli, "triage_message", lambda *a, **k: _result(roles=[_role(resume=Path("/tmp/r.docx"))]))

    rc = imap_main(["--db", str(db)])
    assert rc == 0
    assert mock_imap.moves == [{"uid": "1", "from": "INBOX", "to": "INBOX.JobTracker.PURSUE"}]
    conn = connect(db)
    row = conn.execute("SELECT company FROM job_leads").fetchone()
    assert row["company"] == "Acme"
    conn.close()
    assert "Processed 1" in capsys.readouterr().err


def test_triage_imap_skips_already_seen(monkeypatch, mock_imap, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="imap:<msg-1>", source_label="single-jd")
    upsert_lead(conn, lead)
    add_job_conversation(conn, JobConversation(job_key=lead.normalized_key, message_id="imap:<msg-1>", direction="inbound"))
    conn.close()

    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["1"])
    monkeypatch.setattr(imap_cli, "fetch_message", lambda *a, **k: _msg())

    rc = imap_main(["--db", str(db)])
    assert rc == 0
    assert "already triaged" in capsys.readouterr().err
    assert mock_imap.moves == []


def test_triage_imap_links_reply_by_thread_id(monkeypatch, mock_imap, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(company="DIRECTV", title="Remote Senior Data Engineer", source_message_id="imap:<msg-0>", source_label="single-jd")
    upsert_lead(conn, lead)
    job_key = lead.normalized_key
    add_job_conversation(conn, JobConversation(job_key=job_key, message_id="imap:<msg-0>", thread_id="thread-cole", direction="inbound"))
    conn.close()

    def _triage_should_not_run(*a, **k):
        raise AssertionError("triage_message must not run for a message matched to an existing lead")

    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["1"])
    monkeypatch.setattr(imap_cli, "fetch_message", lambda *a, **k: _msg("imap:<msg-1>", thread_id="thread-cole", body_plain="Thanks, talk soon!"))
    monkeypatch.setattr(imap_cli, "triage_message", _triage_should_not_run)

    rc = imap_main(["--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[LINKED]" in out
    assert mock_imap.moves == [{"uid": "1", "from": "INBOX", "to": "INBOX.JobTracker.Linked"}]

    conn = connect(db)
    convo = conn.execute("SELECT job_key FROM job_conversations WHERE message_id = 'imap:<msg-1>'").fetchone()
    assert convo["job_key"] == job_key
    conn.close()


def test_triage_imap_existing_lead_interview_invite_advances_status(monkeypatch, mock_imap, tmp_path: Path, capsys):
    """2026-07-22 post-application signal wiring: an interview invite on an
    already-tracked lead's thread must advance that lead to 'interviewing'."""
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="imap:<msg-0>", source_label="single-jd", status="applied")
    upsert_lead(conn, lead)
    job_key = lead.normalized_key
    add_job_conversation(conn, JobConversation(job_key=job_key, message_id="imap:<msg-0>", thread_id="thread-int", direction="inbound"))
    conn.close()

    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["1"])
    monkeypatch.setattr(
        imap_cli,
        "fetch_message",
        lambda *a, **k: _msg(
            "imap:<msg-1>", thread_id="thread-int", body_plain="We'd like to schedule an interview with you next week."
        ),
    )
    monkeypatch.setattr(imap_cli, "triage_message", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-triage")))

    rc = imap_main(["--db", str(db)])
    assert rc == 0
    assert "post-application signal: status -> interviewing" in capsys.readouterr().out

    conn = connect(db)
    row = conn.execute("SELECT status FROM job_leads WHERE normalized_key = ?", (job_key,)).fetchone()
    assert row["status"] == "interviewing"
    conn.close()


def test_triage_imap_linkedin_reply_matched_existing_lead(monkeypatch, mock_imap, tmp_path: Path, capsys):
    """hit-reply@linkedin.com mail with a thread/contact match is handled by
    the LinkedIn-reply branch, not the generic classify()/triage_message()
    path — see module docstring's "Two branches" section."""
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="imap:<msg-0>", source_label="single-jd")
    upsert_lead(conn, lead)
    job_key = lead.normalized_key
    add_job_contact(conn, JobContact(job_key=job_key, email="cole@agency.example", role="recruiter"))
    conn.close()

    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["1"])
    monkeypatch.setattr(
        imap_cli,
        "fetch_message",
        lambda *a, **k: _msg("imap:<msg-1>", from_address="hit-reply@linkedin.com", body_plain="Cole: following up on the role."),
    )

    def _match(conn, message, *, direction, use_llm_fallback=False, llm_model=None):
        return MatchOutcome(job_key, "contact_email", "matched")

    monkeypatch.setattr(imap_cli, "match_message_to_job", _match)
    monkeypatch.setattr(imap_cli, "triage_message", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not classify a LinkedIn reply generically")))

    rc = imap_main(["--db", str(db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[LINKEDIN-REPLY]" in out
    assert mock_imap.moves == [{"uid": "1", "from": "INBOX", "to": "INBOX.JobTracker.Linked"}]

    conn = connect(db)
    convo = conn.execute("SELECT job_key FROM job_conversations WHERE message_id = 'imap:<msg-1>'").fetchone()
    assert convo["job_key"] == job_key
    conn.close()


def test_triage_imap_linkedin_reply_unmatched_parks_in_needs_followup(monkeypatch, mock_imap, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"

    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["1"])
    monkeypatch.setattr(
        imap_cli,
        "fetch_message",
        lambda *a, **k: _msg("imap:<msg-1>", from_address="inmail-hit-reply@linkedin.com", body_plain="Someone replied."),
    )

    def _match(conn, message, *, direction, use_llm_fallback=False, llm_model=None):
        return MatchOutcome(None, "unmatched", "no match")

    monkeypatch.setattr(imap_cli, "match_message_to_job", _match)

    rc = imap_main(["--db", str(db)])
    assert rc == 0
    assert mock_imap.moves == [{"uid": "1", "from": "INBOX", "to": "INBOX.JobTracker.NeedsFollowup"}]

    conn = connect(db)
    row = conn.execute("SELECT 1 FROM unmatched_messages WHERE message_id = 'imap:<msg-1>'").fetchone()
    assert row is not None
    conn.close()


def test_triage_imap_linkedin_reply_new_lead_candidate_creates_stub(monkeypatch, mock_imap, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"

    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["1"])
    monkeypatch.setattr(
        imap_cli,
        "fetch_message",
        lambda *a, **k: _msg("imap:<msg-1>", from_address="hit-reply@linkedin.com", body_plain="AI Engineer role at Clevanoo, remote."),
    )

    role = ExtractedRole(company="Clevanoo", title="AI Engineer", confidence=0.9, snippet="AI Engineer role at Clevanoo, remote.")

    def _match(conn, message, *, direction, use_llm_fallback=False, llm_model=None):
        return MatchOutcome(None, "llm_new_lead", "new lead", extracted_role=role)

    monkeypatch.setattr(imap_cli, "match_message_to_job", _match)

    rc = imap_main(["--db", str(db)])
    assert rc == 0
    assert mock_imap.moves == [{"uid": "1", "from": "INBOX", "to": "INBOX.JobTracker.Linked"}]

    conn = connect(db)
    row = conn.execute("SELECT company, title FROM job_leads WHERE company = 'Clevanoo'").fetchone()
    assert row is not None
    assert row["title"] == "AI Engineer"
    conn.close()


def test_triage_imap_error_continues(monkeypatch, mock_imap, tmp_path: Path, capsys):
    monkeypatch.setattr(imap_cli, "list_message_uids", lambda *a, **k: ["bad", "good"])

    def fetch(conn, uid, *, folder):
        if uid == "bad":
            raise RuntimeError("boom")
        return _msg("imap:<good>")

    monkeypatch.setattr(imap_cli, "fetch_message", fetch)
    monkeypatch.setattr(imap_cli, "triage_message", lambda *a, **k: _result(outcome=SKIP, mid="imap:<good>", roles=[]))

    rc = imap_main(["--db", str(tmp_path / "leads.db"), "--force"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "errored" in err
