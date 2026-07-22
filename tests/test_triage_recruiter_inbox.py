"""Tests for triage_recruiter_inbox CLI with Gmail + triage mocked."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from job_tracker.cli import triage_recruiter_inbox as triage_cli
from job_tracker.cli.triage_recruiter_inbox import main as triage_main
from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.llm_apply import EvaluationResult, TwoTierPackageResult
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import add_job_contact, add_job_conversation, connect, record_message_processed, upsert_lead
from job_tracker.pipeline.models import JobContact, JobConversation
from job_tracker.pipeline.triage import MessageTriageResult, RoleOutcome, NEEDS_REVIEW, PURSUE, SKIP
from job_tracker.scoring.scorer import ScoreResult


def _msg(
    mid: str = "msg-1",
    labels: list[str] | None = None,
    *,
    from_address: str = "recruiter@acme.example",
    thread_id: str = "",
    body_plain: str = "We are hiring.",
) -> EmailMessage:
    return EmailMessage(
        id=mid,
        from_address=from_address,
        subject="Software Engineer @ Acme",
        body_plain=body_plain,
        thread_id=thread_id,
        label_ids=labels or ["INBOX"],
    )


def _role(verdict: str = "pursue", resume: Path | None = None) -> RoleOutcome:
    lead = JobLead(
        company="Acme",
        title="Software Engineer",
        source_message_id="msg-1",
        source_label="single-jd",
        jd_text="JD text",
    )
    package = TwoTierPackageResult(
        no_llm_score=ScoreResult(match_pct=80.0, verdict=verdict),
        evaluation=EvaluationResult(verdict=verdict, match_pct=80.0, rationale="ok"),
        resume_path=resume,
        cover_letter_path=resume,
        jd_path=Path("/tmp/Acme/jd.docx") if resume else None,
        full_llm_review_path=Path("/tmp/Acme/review.docx") if resume else None,
        warnings=["w"] if resume else [],
    )
    return RoleOutcome(lead=lead, package=package)


def _result(
    outcome: str = PURSUE,
    *,
    mid: str = "msg-1",
    roles: list[RoleOutcome] | None = None,
    extraction_complete: bool = True,
    extraction_issue: str = "",
) -> MessageTriageResult:
    return MessageTriageResult(
        message_id=mid,
        subject="Software Engineer @ Acme",
        from_address="recruiter@acme.example",
        outcome=outcome,
        reason="test",
        classifier_label="single-jd",
        roles=roles if roles is not None else [_role()],
        extraction_complete=extraction_complete,
        extraction_issue=extraction_issue,
    )


@pytest.fixture()
def mock_gmail(monkeypatch):
    service = SimpleNamespace(name="fake-service")
    monkeypatch.setattr(triage_cli, "default_credentials_path", lambda *a, **k: Path("/tmp/creds.json"))
    monkeypatch.setattr(triage_cli, "default_token_path", lambda *a, **k: Path("/tmp/token.json"))
    monkeypatch.setattr(triage_cli, "get_gmail_service", lambda *a, **k: service)
    monkeypatch.setattr(triage_cli, "get_gmail_service_writable", lambda *a, **k: service)
    return service


def test_triage_no_messages(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: [])
    rc = triage_main(["--dry-run", "--db", str(tmp_path / "leads.db")])
    assert rc == 0
    assert "No messages matched" in capsys.readouterr().err


def test_triage_dry_run_prints(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(triage_cli, "fetch_message", lambda *a, **k: _msg())
    monkeypatch.setattr(triage_cli, "triage_message", lambda *a, **k: _result(roles=[_role(resume=Path("/t/r.docx"))]))
    rc = triage_main(["--dry-run", "--db", str(tmp_path / "leads.db"), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[PURSUE]" in out
    assert "dry run" in out
    # JSON payload also printed
    assert '"outcome"' in out


def test_triage_live_labels_and_stores(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    labeled: list[dict] = []

    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(triage_cli, "fetch_message", lambda *a, **k: _msg())
    monkeypatch.setattr(
        triage_cli,
        "triage_message",
        lambda *a, **k: _result(roles=[_role(resume=Path("/tmp/r.docx"))]),
    )
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "get_or_create_label",
        lambda service, label: f"id-{label}",
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)

    def _label_and_archive(service, message_id, label_id, remove_label_ids=None, archive=True):
        labeled.append(
            {
                "message_id": message_id,
                "label_id": label_id,
                "remove": remove_label_ids,
                "archive": archive,
            }
        )

    monkeypatch.setattr(triage_cli.gmail_writer, "label_and_archive", _label_and_archive)

    rc = triage_main(["--db", str(db), "--newer-than", "3"])
    assert rc == 0
    assert labeled and labeled[0]["archive"] is True
    conn = connect(db)
    row = conn.execute("SELECT company FROM job_leads").fetchone()
    assert row["company"] == "Acme"
    conn.close()
    assert "Processed 1" in capsys.readouterr().err


def test_triage_skips_already_processed(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    record_message_processed(conn, "msg-1", outcome=SKIP, subject="s", from_address="a@b.com")
    conn.close()

    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "get_or_create_label",
        lambda service, label: f"id-{label}",
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)

    rc = triage_main(["--db", str(db)])
    assert rc == 0
    assert "already triaged" in capsys.readouterr().err


def test_triage_error_continues(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["bad", "good"])
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "get_or_create_label",
        lambda service, label: f"id-{label}",
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)
    monkeypatch.setattr(triage_cli.gmail_writer, "label_and_archive", lambda *a, **k: None)

    def fetch(service, mid):
        if mid == "bad":
            raise RuntimeError("boom")
        return _msg(mid)

    monkeypatch.setattr(triage_cli, "fetch_message", fetch)
    monkeypatch.setattr(triage_cli, "triage_message", lambda *a, **k: _result(outcome=SKIP, mid="good", roles=[]))

    rc = triage_main(["--db", str(tmp_path / "leads.db"), "--force"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "ERROR" in err
    assert "errored" in err


def test_triage_job_digest_incomplete_not_archived(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    labeled: list[bool] = []
    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(triage_cli, "fetch_message", lambda *a, **k: _msg(labels=["INBOX", "Label_99"]))
    monkeypatch.setattr(
        triage_cli,
        "triage_message",
        lambda *a, **k: _result(
            outcome=NEEDS_REVIEW,
            roles=[],
            extraction_complete=False,
            extraction_issue="truncated",
        ),
    )
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "get_or_create_label",
        lambda service, label: f"id-{label}",
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: "Label_99")

    def _la(service, message_id, label_id, remove_label_ids=None, archive=True):
        labeled.append(archive)

    monkeypatch.setattr(triage_cli.gmail_writer, "label_and_archive", _la)

    rc = triage_main(["--db", str(tmp_path / "leads.db")])
    assert rc == 0
    assert labeled == [False]
    assert "left in the inbox" in capsys.readouterr().out


def test_triage_force_since_skips_recent(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    record_message_processed(
        conn,
        "msg-1",
        outcome=SKIP,
        subject="s",
        from_address="a@b.com",
    )
    # Force processed_at into the future relative to --force-since threshold
    conn.execute(
        "UPDATE processed_messages SET processed_at = ? WHERE message_id = ?",
        ("2026-07-10T00:00:00+00:00", "msg-1"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "get_or_create_label",
        lambda service, label: f"id-{label}",
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)

    rc = triage_main(["--db", str(db), "--force-since", "2026-07-01T00:00:00+00:00"])
    assert rc == 0
    assert "already triaged" in capsys.readouterr().err


def test_default_query_does_not_require_inbox():
    """2026-07-18 fix: `Category/recruiter_job` mail on this account was found

    100% archived (0 of 374 in the inbox), with comms-migration's own
    archive-safeguard for this account verified working correctly via a live
    test — so an `in:inbox`-scoped query was silently losing everything
    already archived by the time this script got to it, no matter the cause.
    """
    assert "in:inbox" not in triage_cli.DEFAULT_QUERY
    assert "label:Category/recruiter_job" in triage_cli.DEFAULT_QUERY


def test_print_result_falls_back_to_no_llm_score_when_evaluation_is_none(capsys):
    """Regression test: a role can reach a PURSUE/SKIP/NEEDS_REVIEW outcome on
    the free rule-based score alone (see triage._effective_verdict) — the
    full LLM `evaluation` only runs once that score clears
    `should_run_llm_review`'s gate. `_print_result` crashed with
    `AttributeError: 'NoneType' object has no attribute 'verdict'` on exactly
    this case (found 2026-07-18 running a backfill triage over previously
    archived mail whose extraction-fallback role never cleared the gate)."""
    role = _role(verdict="pass")
    role.package.evaluation = None
    result = _result(outcome=SKIP, roles=[role])
    triage_cli._print_result(result, dry_run=True)
    out = capsys.readouterr().out
    assert "PASS (80%)" in out


def test_triage_live_run_survives_evaluation_none(monkeypatch, mock_gmail, tmp_path: Path):
    """Regression test: `update_llm_evaluation` unconditionally dereferences
    `evaluation.metrics` — calling it with `evaluation=None` (a role that
    reached its verdict on the free rule-based score alone, see
    triage._effective_verdict) crashed the *entire* batch, not just one
    message, partway through a real 176-message backfill on 2026-07-18.
    upsert_lead() already persists the rule-based verdict, so the fix is to
    skip the update_llm_evaluation call entirely when evaluation is None."""
    db = tmp_path / "leads.db"
    role = _role(verdict="pass")
    role.package.evaluation = None

    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(triage_cli, "fetch_message", lambda *a, **k: _msg())
    monkeypatch.setattr(triage_cli, "triage_message", lambda *a, **k: _result(outcome=SKIP, roles=[role]))
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "get_or_create_label",
        lambda service, label: f"id-{label}",
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)
    monkeypatch.setattr(triage_cli.gmail_writer, "label_and_archive", lambda *a, **k: None)

    rc = triage_main(["--db", str(db)])
    assert rc == 0
    conn = connect(db)
    row = conn.execute("SELECT company, status, llm_verdict FROM job_leads").fetchone()
    assert row["company"] == "Acme"
    assert row["status"] == "skipped"
    assert row["llm_verdict"] is None  # never backfilled — no full LLM review ran
    conn.close()


def test_triage_json_output_survives_evaluation_none(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    """Regression test: the --json summary block was the *fourth* spot in
    this file with the same `evaluation is None` crash (found 2026-07-18) —
    it only fires with --json, so it slipped past every other fix and blew
    up only after an entire real 176-message batch had already finished
    processing, corrupting nothing but losing the final summary."""
    db = tmp_path / "leads.db"
    role = _role(verdict="pass")
    role.package.evaluation = None

    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(triage_cli, "fetch_message", lambda *a, **k: _msg())
    monkeypatch.setattr(triage_cli, "triage_message", lambda *a, **k: _result(outcome=SKIP, roles=[role]))
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "get_or_create_label",
        lambda service, label: f"id-{label}",
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)
    monkeypatch.setattr(triage_cli.gmail_writer, "label_and_archive", lambda *a, **k: None)

    rc = triage_main(["--db", str(db), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out[out.index("[\n") :])
    assert payload[0]["roles"][0]["verdict"] == "pass"
    assert payload[0]["roles"][0]["match_pct"] == 80.0


def test_default_query_excludes_linked():
    """2026-07-22 fix: once a reply is recorded via the existing-lead
    short-circuit and labeled JobTracker/Linked, it must never be picked up
    again by this script's own query — mirrors the exclusions already in
    place for the three outcome labels."""
    assert "-label:JobTracker/Linked" in triage_cli.DEFAULT_QUERY


def test_triage_links_reply_by_thread_id_instead_of_retriaging(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    """A reply within an existing tracked lead's thread (e.g. a recruiter
    follow-up with no JD in it) must be recorded as a conversation against
    that lead — not independently classified/scored and risk landing on a
    fresh, unrelated SKIP/NEEDS_REVIEW outcome. Regression test for the real
    DIRECTV/Cole Keener thread that ended up carrying both
    JobTracker/NEEDS_REVIEW and JobTracker/SKIP from two different messages."""
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(company="DIRECTV", title="Remote Senior Data Engineer", source_message_id="msg-0", source_label="single-jd")
    upsert_lead(conn, lead)
    job_key = lead.normalized_key
    # An earlier message in this same thread already got linked to the lead.
    add_job_conversation(
        conn,
        JobConversation(job_key=job_key, message_id="msg-0", thread_id="thread-cole-directv", direction="inbound"),
    )
    conn.close()

    def _triage_message_should_not_run(*a, **k):
        raise AssertionError("triage_message must not run for a message matched to an existing lead")

    labeled: list[dict] = []
    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(
        triage_cli,
        "fetch_message",
        lambda *a, **k: _msg("msg-1", thread_id="thread-cole-directv", body_plain="Thanks so much, talk soon!"),
    )
    monkeypatch.setattr(triage_cli, "triage_message", _triage_message_should_not_run)
    monkeypatch.setattr(triage_cli.gmail_writer, "get_or_create_label", lambda service, label: f"id-{label}")
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "label_and_archive",
        lambda service, message_id, label_id, remove_label_ids=None, archive=True: labeled.append(
            {"message_id": message_id, "label_id": label_id, "archive": archive}
        ),
    )

    rc = triage_main(["--db", str(db)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "[LINKED]" in out
    assert "thread_id" in out

    assert labeled == [{"message_id": "msg-1", "label_id": "id-JobTracker/Linked", "archive": True}]

    conn = connect(db)
    convo = conn.execute(
        "SELECT job_key, thread_id FROM job_conversations WHERE message_id = 'msg-1'"
    ).fetchone()
    assert convo["job_key"] == job_key
    assert convo["thread_id"] == "thread-cole-directv"
    # The lead itself must be untouched — no fresh verdict/status stamped.
    row = conn.execute("SELECT status, verdict FROM job_leads WHERE normalized_key = ?", (job_key,)).fetchone()
    assert row["status"] == "new"
    assert row["verdict"] == lead.verdict
    conn.close()


def test_triage_existing_lead_reply_rejection_advances_status(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    """2026-07-22 post-application signal wiring: a rejection reply on an
    already-tracked lead's thread must flip that lead's status to
    'rejected' as part of the existing-lead short-circuit, not just get
    archived as an ordinary linked conversation."""
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(company="DIRECTV", title="Remote Senior Data Engineer", source_message_id="msg-0", source_label="single-jd")
    upsert_lead(conn, lead)
    job_key = lead.normalized_key
    add_job_conversation(
        conn,
        JobConversation(job_key=job_key, message_id="msg-0", thread_id="thread-rej", direction="inbound"),
    )
    conn.close()

    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(
        triage_cli,
        "fetch_message",
        lambda *a, **k: _msg(
            "msg-1",
            thread_id="thread-rej",
            body_plain="Unfortunately, we have decided to move forward with other candidates.",
        ),
    )
    monkeypatch.setattr(
        triage_cli, "triage_message", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-triage"))
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "get_or_create_label", lambda service, label: f"id-{label}")
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)
    monkeypatch.setattr(triage_cli.gmail_writer, "label_and_archive", lambda *a, **k: None)

    rc = triage_main(["--db", str(db)])
    assert rc == 0
    assert "post-application signal: status -> rejected" in capsys.readouterr().out

    conn = connect(db)
    row = conn.execute("SELECT status FROM job_leads WHERE normalized_key = ?", (job_key,)).fetchone()
    assert row["status"] == "rejected"
    conn.close()


def test_triage_links_reply_by_contact_email(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    """Same short-circuit, but matched via a sender email already on file as
    a contact for a tracked lead (no shared thread id — e.g. the recruiter
    started a new email thread but it's the same person/role)."""
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="msg-0", source_label="single-jd")
    upsert_lead(conn, lead)
    job_key = lead.normalized_key
    add_job_contact(conn, JobContact(job_key=job_key, email="cole@agency.example", role="recruiter"))
    conn.close()

    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-2"])
    monkeypatch.setattr(
        triage_cli,
        "fetch_message",
        lambda *a, **k: _msg("msg-2", from_address="cole@agency.example", body_plain="Following up on the role."),
    )
    monkeypatch.setattr(
        triage_cli, "triage_message", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not re-triage"))
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "get_or_create_label", lambda service, label: f"id-{label}")
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)
    monkeypatch.setattr(triage_cli.gmail_writer, "label_and_archive", lambda *a, **k: None)

    rc = triage_main(["--db", str(db)])
    assert rc == 0
    assert "matched an existing job via contact_email" in capsys.readouterr().out

    conn = connect(db)
    convo = conn.execute("SELECT job_key FROM job_conversations WHERE message_id = 'msg-2'").fetchone()
    assert convo["job_key"] == job_key
    conn.close()


def test_triage_link_dry_run_writes_nothing(monkeypatch, mock_gmail, tmp_path: Path, capsys):
    db = tmp_path / "leads.db"
    conn = connect(db)
    lead = JobLead(company="Acme", title="Software Engineer", source_message_id="msg-0", source_label="single-jd")
    upsert_lead(conn, lead)
    job_key = lead.normalized_key
    add_job_conversation(conn, JobConversation(job_key=job_key, message_id="msg-0", thread_id="thread-xyz", direction="inbound"))
    conn.close()

    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(triage_cli, "fetch_message", lambda *a, **k: _msg("msg-1", thread_id="thread-xyz"))

    rc = triage_main(["--dry-run", "--db", str(db)])
    assert rc == 0
    assert "[LINKED]" in capsys.readouterr().out

    conn = connect(db)
    assert conn.execute("SELECT 1 FROM job_conversations WHERE message_id = 'msg-1'").fetchone() is None
    conn.close()


def test_triage_pass_verdict_advances_skipped(monkeypatch, mock_gmail, tmp_path: Path):
    db = tmp_path / "leads.db"
    monkeypatch.setattr(triage_cli, "list_message_ids", lambda *a, **k: ["msg-1"])
    monkeypatch.setattr(triage_cli, "fetch_message", lambda *a, **k: _msg())
    monkeypatch.setattr(
        triage_cli,
        "triage_message",
        lambda *a, **k: _result(outcome=SKIP, roles=[_role(verdict="pass")]),
    )
    monkeypatch.setattr(
        triage_cli.gmail_writer,
        "get_or_create_label",
        lambda service, label: f"id-{label}",
    )
    monkeypatch.setattr(triage_cli.gmail_writer, "find_label_id", lambda *a, **k: None)
    monkeypatch.setattr(triage_cli.gmail_writer, "label_and_archive", lambda *a, **k: None)

    rc = triage_main(["--db", str(db)])
    assert rc == 0
    conn = connect(db)
    row = conn.execute("SELECT status FROM job_leads").fetchone()
    assert row["status"] == "skipped"
    conn.close()
