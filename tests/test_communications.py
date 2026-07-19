"""Tests for the communications-archival feature added 2026-07-17:
pipeline/comms_match.py's tiered matching, the new store.py functions it and
scan_communications.py/resolve_communication.py rely on, and those two CLIs
themselves."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from job_tracker.cli import resolve_communication as resolve_cli
from job_tracker.cli import scan_communications as scan_cli
from job_tracker.cli.resolve_communication import main as resolve_main
from job_tracker.cli.scan_communications import main as scan_main
from job_tracker.email.models import EmailMessage, ExtractedRole
from job_tracker.pipeline.comms_match import match_message_to_job
from job_tracker.pipeline.models import JobContact, JobConversation, JobLead, UnmatchedMessage
from job_tracker.pipeline.store import (
    connect,
    find_company_only_matches,
    find_job_by_contact_email,
    find_job_by_thread_id,
    get_unmatched_message,
    is_communication_seen,
    list_unmatched_messages,
    record_message_processed,
    record_unmatched_message,
    resolve_unmatched_message,
    upsert_lead,
    add_job_contact,
    add_job_conversation,
)


@pytest.fixture()
def seeded_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "leads.db"
    conn = connect(db_path)
    upsert_lead(
        conn,
        JobLead(company="Clevanoo LLC", title="Senior Full Stack AI/ML Engineer", source_message_id="m0", source_label="linkedin_message"),
    )
    conn.close()
    return db_path


def _key(db_path: Path, company: str, title: str) -> str:
    from job_tracker.pipeline.models import normalize_key

    return normalize_key(company, title)


# --- store.py -------------------------------------------------------------


def test_find_job_by_thread_id_matches_after_first_conversation(seeded_db: Path):
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_conversation(conn, JobConversation(job_key=key, direction="inbound", summary="first", thread_id="t-123"))

    assert find_job_by_thread_id(conn, "t-123") == key
    assert find_job_by_thread_id(conn, "") is None
    assert find_job_by_thread_id(conn, "nope") is None
    conn.close()


def test_find_job_by_contact_email_matches_across_jobs(seeded_db: Path):
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_contact(conn, JobContact(job_key=key, email="Radha@Clevanoo.example"))

    assert find_job_by_contact_email(conn, "radha@clevanoo.example") == key
    assert find_job_by_contact_email(conn, "") is None
    assert find_job_by_contact_email(conn, "nobody@nowhere.example") is None
    conn.close()


def test_find_company_only_matches_requires_fuzzy_company_hit(seeded_db: Path):
    conn = connect(seeded_db)
    matches = find_company_only_matches(conn, "Clevanoo LLC")
    assert len(matches) == 1
    assert matches[0].title == "Senior Full Stack AI/ML Engineer"

    assert find_company_only_matches(conn, "Totally Different Co") == []
    conn.close()


def test_find_company_only_matches_ambiguous_with_two_leads(seeded_db: Path):
    conn = connect(seeded_db)
    upsert_lead(
        conn,
        JobLead(company="Clevanoo LLC", title="Platform Engineer", source_message_id="m1", source_label="linkedin_message"),
    )
    matches = find_company_only_matches(conn, "Clevanoo LLC")
    assert len(matches) == 2
    conn.close()


def test_record_and_list_unmatched_message(seeded_db: Path):
    conn = connect(seeded_db)
    record_unmatched_message(
        conn,
        UnmatchedMessage(message_id="msg-1", direction="inbound", from_address="camilla@example.com", subject="Opportunity"),
    )
    unresolved = list_unmatched_messages(conn)
    assert len(unresolved) == 1
    assert unresolved[0]["message_id"] == "msg-1"

    # Idempotent: re-recording the same message_id is a no-op.
    record_unmatched_message(
        conn,
        UnmatchedMessage(message_id="msg-1", direction="inbound", from_address="ignored@example.com", subject="ignored"),
    )
    assert len(list_unmatched_messages(conn)) == 1
    assert get_unmatched_message(conn, "msg-1")["from_address"] == "camilla@example.com"
    conn.close()


def test_resolve_unmatched_message_creates_contact_and_conversation(seeded_db: Path):
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="msg-2",
            thread_id="t-9",
            direction="inbound",
            from_address="radha@clevanoo.example",
            subject="Re: role details",
            body_text="Here is the JD you asked for.",
        ),
    )

    conversation_id = resolve_unmatched_message(conn, "msg-2", key, contact_name="Radha Krishna")
    assert conversation_id is not None

    row = get_unmatched_message(conn, "msg-2")
    assert row["resolved_job_key"] == key
    assert row["resolved_at"] is not None

    conversations = conn.execute("SELECT * FROM job_conversations WHERE job_key = ?", (key,)).fetchall()
    assert len(conversations) == 1
    assert conversations[0]["thread_id"] == "t-9"
    assert conversations[0]["body_text"] == "Here is the JD you asked for."

    contact = conn.execute("SELECT * FROM job_contacts WHERE job_key = ?", (key,)).fetchone()
    assert contact["name"] == "Radha Krishna"
    assert contact["email"] == "radha@clevanoo.example"
    conn.close()


def test_add_job_contact_dedupes_name_only_contacts_without_email(seeded_db: Path):
    """2026-07-17 signature-backfill fix: `add_job_contact`'s dedupe key was
    email-only, so repeated calls for the same job with a name but no email
    (the common case for a recruiter whose sign-off had no labeled email/
    phone line) piled up duplicate rows instead of updating one."""
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")

    first_id = add_job_contact(conn, JobContact(job_key=key, name="Amit Gupta", role="recruiter"))
    second_id = add_job_contact(conn, JobContact(job_key=key, name="Amit Gupta", role="recruiter"))
    assert first_id == second_id

    rows = conn.execute(
        "SELECT * FROM job_contacts WHERE job_key = ? AND name = 'Amit Gupta'", (key,)
    ).fetchall()
    assert len(rows) == 1

    # A DIFFERENT name-only contact on the same job still gets its own row —
    # the dedupe key is (job_key, name), not just "any email-less contact".
    third_id = add_job_contact(conn, JobContact(job_key=key, name="Someone Else", role="recruiter"))
    assert third_id != first_id
    conn.close()


def test_resolve_unmatched_message_unknown_id_raises(seeded_db: Path):
    conn = connect(seeded_db)
    with pytest.raises(ValueError):
        resolve_unmatched_message(conn, "does-not-exist", "some::key")
    conn.close()


def test_is_communication_seen_checks_all_three_tables(seeded_db: Path):
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    assert is_communication_seen(conn, "a") is False

    record_message_processed(conn, "a", outcome="PURSUE")
    assert is_communication_seen(conn, "a") is True

    add_job_conversation(conn, JobConversation(job_key=key, direction="inbound", summary="x", message_id="b"))
    assert is_communication_seen(conn, "b") is True

    record_unmatched_message(conn, UnmatchedMessage(message_id="c", direction="inbound"))
    assert is_communication_seen(conn, "c") is True

    assert is_communication_seen(conn, "d") is False
    conn.close()


# --- pipeline/comms_match.py ----------------------------------------------


def _email(**kwargs) -> EmailMessage:
    defaults = dict(id="m1", from_address="radha@clevanoo.example", to_address="me@example.com", subject="Re: role", thread_id="")
    defaults.update(kwargs)
    return EmailMessage(**defaults)


def test_match_tier1_thread_id(seeded_db: Path):
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_conversation(conn, JobConversation(job_key=key, direction="inbound", summary="x", thread_id="thread-1"))

    outcome = match_message_to_job(conn, _email(thread_id="thread-1", from_address="unknown@x.com"), direction="inbound")
    assert outcome.matched
    assert outcome.job_key == key
    assert outcome.tier == "thread_id"
    conn.close()


def test_match_tier2_contact_email(seeded_db: Path):
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_contact(conn, JobContact(job_key=key, email="radha@clevanoo.example"))

    outcome = match_message_to_job(conn, _email(), direction="inbound")
    assert outcome.matched
    assert outcome.job_key == key
    assert outcome.tier == "contact_email"
    conn.close()


def test_match_tier2_ignores_linkedin_generic_relay_address(seeded_db: Path):
    """Regression test (2026-07-17 live finding): hit-reply@/inmail-hit-
    reply@linkedin.com are LinkedIn's own relay addresses, shared across
    every recruiter's messages — a job that happens to have one on file as
    a job_contacts.email must NOT cause every unrelated LinkedIn message to
    spuriously Tier-2-match onto it."""
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_contact(conn, JobContact(job_key=key, email="hit-reply@linkedin.com"))

    outcome = match_message_to_job(conn, _email(from_address="hit-reply@linkedin.com"), direction="inbound")
    assert not outcome.matched
    assert outcome.tier == "unmatched"
    conn.close()


def test_match_no_llm_fallback_returns_unmatched(seeded_db: Path):
    conn = connect(seeded_db)
    outcome = match_message_to_job(conn, _email(from_address="nobody@nowhere.example"), direction="inbound", use_llm_fallback=False)
    assert not outcome.matched
    assert outcome.tier == "unmatched"
    conn.close()


def test_match_tier3_llm_company_title(monkeypatch, seeded_db: Path):
    conn = connect(seeded_db)
    monkeypatch.setattr(
        "job_tracker.pipeline.comms_match.extract_roles_llm_cached",
        lambda conn, message, model=None: [
            ExtractedRole(company="Clevanoo LLC", title="Senior Full Stack AI/ML Engineer", confidence=0.9)
        ],
    )
    outcome = match_message_to_job(conn, _email(from_address="nobody@nowhere.example"), direction="inbound", use_llm_fallback=True)
    assert outcome.matched
    assert outcome.tier == "llm_company_title"
    conn.close()


def test_match_tier3_llm_company_only_single_candidate(monkeypatch, seeded_db: Path):
    conn = connect(seeded_db)
    monkeypatch.setattr(
        "job_tracker.pipeline.comms_match.extract_roles_llm_cached",
        lambda conn, message, model=None: [ExtractedRole(company="Clevanoo LLC", title="", confidence=0.6)],
    )
    outcome = match_message_to_job(conn, _email(from_address="nobody@nowhere.example"), direction="inbound", use_llm_fallback=True)
    assert outcome.matched
    assert outcome.tier == "llm_company_only"
    conn.close()


def test_match_tier3_llm_company_only_ambiguous(monkeypatch, seeded_db: Path):
    conn = connect(seeded_db)
    upsert_lead(
        conn,
        JobLead(company="Clevanoo LLC", title="Platform Engineer", source_message_id="m1", source_label="linkedin_message"),
    )
    monkeypatch.setattr(
        "job_tracker.pipeline.comms_match.extract_roles_llm_cached",
        lambda conn, message, model=None: [ExtractedRole(company="Clevanoo LLC", title="", confidence=0.6)],
    )
    outcome = match_message_to_job(conn, _email(from_address="nobody@nowhere.example"), direction="inbound", use_llm_fallback=True)
    assert not outcome.matched
    assert outcome.tier == "unmatched"
    assert "ambiguous" in outcome.reason
    conn.close()


def test_match_tier3_llm_finds_nothing(monkeypatch, seeded_db: Path):
    conn = connect(seeded_db)
    monkeypatch.setattr("job_tracker.pipeline.comms_match.extract_roles_llm_cached", lambda conn, message, model=None: [])
    outcome = match_message_to_job(conn, _email(from_address="nobody@nowhere.example"), direction="inbound", use_llm_fallback=True)
    assert not outcome.matched
    assert outcome.tier == "unmatched"
    conn.close()


def test_match_tier3_llm_new_lead_candidate(monkeypatch, seeded_db: Path):
    """2026-07-17 refinement: a full (company, title) extraction that
    matches nothing on file is a distinct outcome from "couldn't tell" —
    `is_new_lead_candidate` is what scan_communications.py uses to decide
    whether to create a stub lead instead of parking the message."""
    conn = connect(seeded_db)
    monkeypatch.setattr(
        "job_tracker.pipeline.comms_match.extract_roles_llm_cached",
        lambda conn, message, model=None: [
            ExtractedRole(
                company="Brand New Co",
                title="Staff Engineer",
                confidence=0.85,
                snippet="We need a Staff Engineer, remote, W2.",
            )
        ],
    )
    outcome = match_message_to_job(conn, _email(from_address="nobody@nowhere.example"), direction="inbound", use_llm_fallback=True)
    assert not outcome.matched
    assert outcome.tier == "llm_new_lead"
    assert outcome.is_new_lead_candidate
    assert outcome.extracted_role.company == "Brand New Co"
    assert outcome.extracted_role.title == "Staff Engineer"
    conn.close()


def test_match_tier3_llm_company_only_is_not_a_new_lead_candidate(monkeypatch, seeded_db: Path):
    """A company-only extraction (no title) never qualifies for
    `is_new_lead_candidate` — too little to safely seed a new lead from."""
    conn = connect(seeded_db)
    monkeypatch.setattr(
        "job_tracker.pipeline.comms_match.extract_roles_llm_cached",
        lambda conn, message, model=None: [ExtractedRole(company="Totally New Co", title="", confidence=0.6)],
    )
    outcome = match_message_to_job(conn, _email(from_address="nobody@nowhere.example"), direction="inbound", use_llm_fallback=True)
    assert not outcome.matched
    assert not outcome.is_new_lead_candidate
    assert outcome.tier == "unmatched"
    conn.close()


# --- cli/scan_communications.py -------------------------------------------


def _raw_message(mid: str, *, from_addr: str, subject: str, thread_id: str = "") -> dict:
    return {
        "id": mid,
        "threadId": thread_id or mid,
        "snippet": "",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
            ],
            "mimeType": "text/plain",
            "body": {"data": ""},
        },
    }


def _raw_message_with_body(mid: str, *, from_addr: str, subject: str, body: str, thread_id: str = "") -> dict:
    """Like `_raw_message`, but with a real, base64url-encoded plain-text
    body — needed for anything exercising `pipeline/signature.py`, since
    that parses `message.combined_text`, not the headers `_raw_message`
    alone provides."""
    import base64

    encoded = base64.urlsafe_b64encode(body.encode("utf-8")).decode("ascii")
    return {
        "id": mid,
        "threadId": thread_id or mid,
        "snippet": "",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": from_addr},
                {"name": "To", "value": "me@example.com"},
                {"name": "Subject", "value": subject},
            ],
            "mimeType": "text/plain",
            "body": {"data": encoded},
        },
    }


class _FakeGmailService:
    """Doubles both `messages()` and `labels()` (2026-07-19: scan_communications.py
    gained `gmail.modify` write access to label Linked/NeedsFollowup traffic) —
    `modify_calls`/`created_labels` let tests assert exactly what got written
    without a real Gmail connection."""

    def __init__(self, inbound_ids: list[str], messages: dict[str, dict], sent_ids: list[str] | None = None):
        self.inbound_ids = inbound_ids
        self.sent_ids = sent_ids or []
        self._messages = messages
        self.modify_calls: list[dict] = []
        self._next_label_id = 1
        self._labels: dict[str, str] = {}

    def users(self):
        return self

    def messages(self):
        return self

    def labels(self):
        return self

    def list(self, **kwargs):
        # `messages().list(q=...)` and `labels().list()` both land on this
        # same double (both go through `users()` -> `.messages()`/`.labels()`
        # -> `.list`) — distinguished only by the absence of a `q` kwarg,
        # since `gmail_writer.get_or_create_label`'s `labels().list()` never
        # passes one.
        if "q" not in kwargs:
            return SimpleNamespace(execute=lambda: {"labels": []})
        query = kwargs.get("q", "")
        ids = self.sent_ids if "in:sent" in query else self.inbound_ids
        return SimpleNamespace(execute=lambda: {"messages": [{"id": i} for i in ids]})

    def create(self, **kwargs):
        name = kwargs["body"]["name"]
        label_id = self._labels.setdefault(name, f"Label_{self._next_label_id}")
        self._next_label_id += 1
        return SimpleNamespace(execute=lambda: {"id": label_id, "name": name})

    def get(self, **kwargs):
        return SimpleNamespace(execute=lambda: self._messages[kwargs["id"]])

    def modify(self, **kwargs):
        self.modify_calls.append(kwargs)
        return SimpleNamespace(execute=lambda: {})


@pytest.fixture()
def mock_gmail(monkeypatch):
    def _install(service):
        monkeypatch.setattr(scan_cli, "default_credentials_path", lambda *a, **k: Path("/tmp/c.json"))
        monkeypatch.setattr(scan_cli, "default_token_path", lambda *a, **k: Path("/tmp/t.json"))
        monkeypatch.setattr(scan_cli, "get_gmail_service", lambda *a, **k: service)
        monkeypatch.setattr(scan_cli, "get_gmail_service_writable", lambda *a, **k: service)
        return service

    return _install


def test_scan_communications_links_via_thread_id(mock_gmail, seeded_db: Path, capsys):
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_conversation(conn, JobConversation(job_key=key, direction="inbound", summary="x", thread_id="thread-abc"))
    conn.close()

    raw = _raw_message("msg-1", from_addr="hit-reply@linkedin.com", subject="Message replied: Radha", thread_id="thread-abc")
    service = mock_gmail(_FakeGmailService(["msg-1"], {"msg-1": raw}))

    rc = scan_main(["--db", str(seeded_db)])
    assert rc == 0

    conn = connect(seeded_db)
    convos = conn.execute("SELECT * FROM job_conversations WHERE job_key = ?", (key,)).fetchall()
    assert len(convos) == 2  # the seeded one + the newly linked one
    assert any(c["message_id"] == "msg-1" for c in convos)
    conn.close()


def test_scan_communications_enriches_contact_from_signature(mock_gmail, seeded_db: Path):
    """2026-07-17 signature-parsing follow-up: a Tier-1 (thread_id) match
    still has a generic relay `From:` address, but the message body has a
    real recruiter sign-off — that should end up as a JobContact, not
    nothing, even though the header alone gives us no usable email."""
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_conversation(conn, JobConversation(job_key=key, direction="inbound", summary="x", thread_id="thread-sig"))
    conn.close()

    body = (
        "Exciting opportunity for your skills\r\n"
        "\r\n      Priya Nair\r\n        Reply\r\n"
        "        https://www.linkedin.com/messaging/thread/2-sig==/\r\n"
        "\r\nHi Shawn,\r\n\r\nBest regards,\r\nPriya\r\n\r\n"
        "Priya Nair\r\nTechnical Recruiter\r\nAcme Staffing\r\n"
        "Email: priya.nair@acmestaffing.example | Cell: 212-555-0100\r\n"
    )
    raw = _raw_message_with_body(
        "msg-sig", from_addr="inmail-hit-reply@linkedin.com", subject="Message replied: follow-up",
        body=body, thread_id="thread-sig",
    )
    mock_gmail(_FakeGmailService(["msg-sig"], {"msg-sig": raw}))

    rc = scan_main(["--db", str(seeded_db)])
    assert rc == 0

    conn = connect(seeded_db)
    contact = conn.execute(
        "SELECT * FROM job_contacts WHERE job_key = ? AND lower(email) = ?",
        (key, "priya.nair@acmestaffing.example"),
    ).fetchone()
    assert contact is not None
    assert contact["name"] == "Priya Nair"
    assert contact["phone"] == "212-555-0100"
    conn.close()


def test_scan_communications_parks_unmatched_inbound(mock_gmail, seeded_db: Path):
    raw = _raw_message("msg-2", from_addr="hit-reply@linkedin.com", subject="Message replied: Camilla")
    mock_gmail(_FakeGmailService(["msg-2"], {"msg-2": raw}))

    rc = scan_main(["--db", str(seeded_db)])
    assert rc == 0

    conn = connect(seeded_db)
    unresolved = list_unmatched_messages(conn)
    assert len(unresolved) == 1
    assert unresolved[0]["message_id"] == "msg-2"
    conn.close()


def test_scan_communications_skips_already_seen(mock_gmail, seeded_db: Path):
    conn = connect(seeded_db)
    record_message_processed(conn, "msg-3", outcome="SKIP")
    conn.close()

    raw = _raw_message("msg-3", from_addr="hit-reply@linkedin.com", subject="Already handled")
    mock_gmail(_FakeGmailService(["msg-3"], {"msg-3": raw}))

    rc = scan_main(["--db", str(seeded_db)])
    assert rc == 0

    conn = connect(seeded_db)
    assert list_unmatched_messages(conn) == []
    conn.close()


def test_scan_communications_outbound_unmatched_is_skipped_not_parked(mock_gmail, seeded_db: Path):
    raw = _raw_message("sent-1", from_addr="me@example.com", subject="Following up")
    mock_gmail(_FakeGmailService([], {"sent-1": raw}, sent_ids=["sent-1"]))

    rc = scan_main(["--db", str(seeded_db), "--include-sent"])
    assert rc == 0

    conn = connect(seeded_db)
    assert list_unmatched_messages(conn) == []
    convos = conn.execute("SELECT * FROM job_conversations").fetchall()
    assert len(convos) == 0
    conn.close()


def test_scan_communications_dry_run_writes_nothing(mock_gmail, seeded_db: Path):
    raw = _raw_message("msg-4", from_addr="hit-reply@linkedin.com", subject="A reply")
    mock_gmail(_FakeGmailService(["msg-4"], {"msg-4": raw}))

    rc = scan_main(["--db", str(seeded_db), "--dry-run", "--json"])
    assert rc == 0

    conn = connect(seeded_db)
    assert list_unmatched_messages(conn) == []
    conn.close()


def test_scan_communications_creates_new_lead_and_archives_txt(mock_gmail, monkeypatch, seeded_db: Path, tmp_path: Path):
    """2026-07-17 refinement: "if the company and title can be extracted...
    add it as a new document for that company+title - in the DB and in the
    filesystem folder" — no ATS lookup, no LLM review, no package; just the
    stub lead + the archived message."""
    monkeypatch.setattr(
        "job_tracker.pipeline.comms_match.extract_roles_llm_cached",
        lambda conn, message, model=None: [
            ExtractedRole(
                company="Brand New Co",
                title="Staff Engineer",
                confidence=0.85,
                apply_url="https://example.com/apply",
                snippet="We need a Staff Engineer, remote, W2.",
            )
        ],
    )
    raw = _raw_message("msg-new", from_addr="hit-reply@linkedin.com", subject="Exciting opportunity")
    mock_gmail(_FakeGmailService(["msg-new"], {"msg-new": raw}))

    output_root = tmp_path / "resumes"
    rc = scan_main(["--db", str(seeded_db), "--llm-fallback", "--output-root", str(output_root)])
    assert rc == 0

    conn = connect(seeded_db)
    lead = conn.execute("SELECT * FROM job_leads WHERE company = 'Brand New Co'").fetchone()
    assert lead is not None
    assert lead["title"] == "Staff Engineer"
    assert lead["status"] == "new"
    assert "Staff Engineer, remote, W2" in lead["jd_text"]
    assert lead["jd_resolved"] == 1

    doc = conn.execute(
        "SELECT * FROM job_documents WHERE job_key = ? AND doc_type = 'email_txt'", (lead["normalized_key"],)
    ).fetchone()
    assert doc is not None
    txt_path = Path(doc["path_or_url"])
    assert txt_path.exists()
    # The .txt archive is the raw message (headers + body), not the LLM's
    # extracted snippet — that snippet lives in the lead's jd_text instead.
    assert "Message-Id: msg-new" in txt_path.read_text()
    assert txt_path.is_relative_to(output_root)

    convo = conn.execute("SELECT * FROM job_conversations WHERE job_key = ?", (lead["normalized_key"],)).fetchone()
    assert convo["message_id"] == "msg-new"
    conn.close()


def test_scan_communications_updates_jd_text_on_existing_new_lead(mock_gmail, monkeypatch, seeded_db: Path, tmp_path: Path):
    """A follow-up message that extraction fuzzy-matches onto an EXISTING
    (still status='new') lead should append its excerpt to jd_text and
    still archive the raw message — "attempt to extract job-lead data from
    the follow-up message and update the job-lead as needed"."""
    monkeypatch.setattr(
        "job_tracker.pipeline.comms_match.extract_roles_llm_cached",
        lambda conn, message, model=None: [
            ExtractedRole(
                company="Clevanoo LLC",
                title="Senior Full Stack AI/ML Engineer",
                confidence=0.9,
                snippet="Confirmed: W2, end client is GE Healthcare.",
            )
        ],
    )
    raw = _raw_message("msg-followup", from_addr="hit-reply@linkedin.com", subject="Message replied: Radha")
    mock_gmail(_FakeGmailService(["msg-followup"], {"msg-followup": raw}))

    output_root = tmp_path / "resumes"
    rc = scan_main(["--db", str(seeded_db), "--llm-fallback", "--output-root", str(output_root)])
    assert rc == 0

    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    lead = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    assert "Confirmed: W2, end client is GE Healthcare." in lead["jd_text"]

    doc = conn.execute(
        "SELECT * FROM job_documents WHERE job_key = ? AND doc_type = 'email_txt'", (key,)
    ).fetchone()
    assert doc is not None
    assert Path(doc["path_or_url"]).exists()
    conn.close()


def test_scan_communications_does_not_touch_jd_text_past_new_status(mock_gmail, monkeypatch, seeded_db: Path, tmp_path: Path):
    """Once a lead has moved past status='new' (a human has triaged it),
    a follow-up extraction must not silently rewrite jd_text — same guard
    `store.upsert_lead` applies everywhere else."""
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    conn.execute("UPDATE job_leads SET status = 'pursued', jd_text = 'original JD' WHERE normalized_key = ?", (key,))
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "job_tracker.pipeline.comms_match.extract_roles_llm_cached",
        lambda conn, message, model=None: [
            ExtractedRole(
                company="Clevanoo LLC",
                title="Senior Full Stack AI/ML Engineer",
                confidence=0.9,
                snippet="Confirmed: W2, end client is GE Healthcare.",
            )
        ],
    )
    raw = _raw_message("msg-followup2", from_addr="hit-reply@linkedin.com", subject="Message replied: Radha")
    mock_gmail(_FakeGmailService(["msg-followup2"], {"msg-followup2": raw}))

    rc = scan_main(["--db", str(seeded_db), "--llm-fallback", "--output-root", str(tmp_path / "resumes")])
    assert rc == 0

    conn = connect(seeded_db)
    lead = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (key,)).fetchone()
    assert lead["jd_text"] == "original JD"
    # The conversation itself still gets logged — only the JD text is protected.
    convo = conn.execute("SELECT * FROM job_conversations WHERE job_key = ? AND message_id = 'msg-followup2'", (key,)).fetchone()
    assert convo is not None
    conn.close()


def test_scan_communications_labels_linked_and_archives_matched_message(mock_gmail, seeded_db: Path):
    """2026-07-19: a message this scan successfully resolves gets
    JobTracker/Linked and is archived (INBOX removed) — see gmail_writer's
    LINKED_LABEL docstring for why."""
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_conversation(conn, JobConversation(job_key=key, direction="inbound", summary="x", thread_id="thread-linked"))
    conn.close()

    raw = _raw_message("msg-linked", from_addr="hit-reply@linkedin.com", subject="Message replied: Radha", thread_id="thread-linked")
    service = mock_gmail(_FakeGmailService(["msg-linked"], {"msg-linked": raw}))

    rc = scan_main(["--db", str(seeded_db)])
    assert rc == 0

    calls = [c for c in service.modify_calls if c["id"] == "msg-linked"]
    assert len(calls) == 1
    from job_tracker.email import gmail_writer

    assert "INBOX" in calls[0]["body"]["removeLabelIds"]
    linked_label_id = service._labels[gmail_writer.LINKED_LABEL]
    assert calls[0]["body"]["addLabelIds"] == [linked_label_id]


def test_scan_communications_labels_needs_followup_without_archiving_when_parked(mock_gmail, seeded_db: Path):
    """A message parked in the unmatched queue gets JobTracker/NeedsFollowup
    but stays in the inbox (not archived) — it's exactly what still needs a
    human's attention, unlike a fully-Linked message."""
    raw = _raw_message("msg-parked", from_addr="hit-reply@linkedin.com", subject="Message replied: Camilla")
    service = mock_gmail(_FakeGmailService(["msg-parked"], {"msg-parked": raw}))

    rc = scan_main(["--db", str(seeded_db)])
    assert rc == 0

    calls = [c for c in service.modify_calls if c["id"] == "msg-parked"]
    assert len(calls) == 1
    from job_tracker.email import gmail_writer

    assert calls[0]["body"]["removeLabelIds"] == []
    needs_followup_id = service._labels[gmail_writer.NEEDS_FOLLOWUP_LABEL]
    assert calls[0]["body"]["addLabelIds"] == [needs_followup_id]


def test_scan_communications_dry_run_never_calls_gmail_modify(mock_gmail, seeded_db: Path):
    raw = _raw_message("msg-dry", from_addr="hit-reply@linkedin.com", subject="A reply")
    service = mock_gmail(_FakeGmailService(["msg-dry"], {"msg-dry": raw}))

    rc = scan_main(["--db", str(seeded_db), "--dry-run"])
    assert rc == 0
    assert service.modify_calls == []


def test_scan_communications_outbound_messages_are_never_labeled(mock_gmail, seeded_db: Path):
    """Even a successfully Tier-1-matched outbound (Sent-folder) message
    never gets labeled — Sent isn't reviewed for "still needs my
    attention," see module docstring."""
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    add_job_conversation(conn, JobConversation(job_key=key, direction="inbound", summary="x", thread_id="thread-sent"))
    conn.close()

    raw = _raw_message("sent-linked", from_addr="me@example.com", subject="Following up", thread_id="thread-sent")
    service = mock_gmail(_FakeGmailService([], {"sent-linked": raw}, sent_ids=["sent-linked"]))

    rc = scan_main(["--db", str(seeded_db), "--include-sent"])
    assert rc == 0
    assert service.modify_calls == []


# --- cli/resolve_communication.py -----------------------------------------


def test_resolve_communication_list_shows_pending(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    record_unmatched_message(conn, UnmatchedMessage(message_id="u1", direction="inbound", subject="Hi", from_address="a@b.com"))
    conn.close()

    rc = resolve_main(["--db", str(seeded_db), "--list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "u1" in out
    assert "Hi" in out


def test_resolve_communication_attaches_to_existing_job(seeded_db: Path):
    conn = connect(seeded_db)
    record_unmatched_message(
        conn,
        UnmatchedMessage(message_id="u2", direction="inbound", subject="JD details", from_address="radha@clevanoo.example", body_text="body"),
    )
    conn.close()

    rc = resolve_main(
        [
            "--db", str(seeded_db), "--message-id", "u2",
            "--company", "Clevanoo LLC", "--title", "Senior Full Stack AI/ML Engineer",
            "--contact-name", "Radha Krishna",
        ]
    )
    assert rc == 0

    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    convo = conn.execute("SELECT * FROM job_conversations WHERE job_key = ?", (key,)).fetchone()
    assert convo["message_id"] == "u2"
    conn.close()


def test_resolve_communication_unknown_job_without_create_errors(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    record_unmatched_message(conn, UnmatchedMessage(message_id="u3", direction="inbound", subject="Pitch"))
    conn.close()

    rc = resolve_main(["--db", str(seeded_db), "--message-id", "u3", "--company", "Brand New Co", "--title", "New Role"])
    assert rc == 1
    assert "No job found" in capsys.readouterr().err


def test_resolve_communication_create_makes_stub_lead(seeded_db: Path):
    conn = connect(seeded_db)
    record_unmatched_message(conn, UnmatchedMessage(message_id="u4", direction="inbound", subject="Vague pitch", from_address="camilla@example.com"))
    conn.close()

    rc = resolve_main(
        ["--db", str(seeded_db), "--message-id", "u4", "--company", "Brand New Co", "--title", "New Role", "--create"]
    )
    assert rc == 0

    conn = connect(seeded_db)
    lead = conn.execute("SELECT * FROM job_leads WHERE company = 'Brand New Co'").fetchone()
    assert lead is not None
    convo = conn.execute("SELECT * FROM job_conversations WHERE job_key = ?", (lead["normalized_key"],)).fetchone()
    assert convo["message_id"] == "u4"
    conn.close()


def test_resolve_communication_auto_detects_signature_when_no_contact_flags_given(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    body = (
        "\r\n      Priya Nair\r\n        Reply\r\n"
        "        https://www.linkedin.com/messaging/thread/2-sig==/\r\n"
        "\r\nBest regards,\r\nPriya\r\n\r\n"
        "Priya Nair\r\nTechnical Recruiter\r\nAcme Staffing\r\n"
        "Email: priya.nair@acmestaffing.example | Cell: 212-555-0100\r\n"
    )
    record_unmatched_message(
        conn, UnmatchedMessage(message_id="u8", direction="inbound", subject="pitch", body_text=body)
    )
    conn.close()

    rc = resolve_main(
        ["--db", str(seeded_db), "--message-id", "u8", "--company", "Brand New Co", "--title", "New Role", "--create"]
    )
    assert rc == 0
    assert "Auto-detected from message body" in capsys.readouterr().out

    conn = connect(seeded_db)
    lead = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Brand New Co'").fetchone()
    contact = conn.execute(
        "SELECT * FROM job_contacts WHERE job_key = ? AND lower(email) = ?",
        (lead["normalized_key"], "priya.nair@acmestaffing.example"),
    ).fetchone()
    assert contact is not None
    assert contact["name"] == "Priya Nair"
    assert contact["phone"] == "212-555-0100"
    conn.close()


def test_resolve_communication_explicit_contact_flags_override_signature(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    body = "Email: priya.nair@acmestaffing.example"
    record_unmatched_message(
        conn, UnmatchedMessage(message_id="u9", direction="inbound", subject="pitch", body_text=body)
    )
    conn.close()

    rc = resolve_main(
        [
            "--db", str(seeded_db), "--message-id", "u9", "--company", "Brand New Co 2", "--title", "New Role",
            "--create", "--contact-name", "Manual Name", "--contact-email", "manual@example.com",
        ]
    )
    assert rc == 0
    assert "Auto-detected from message body" not in capsys.readouterr().out

    conn = connect(seeded_db)
    lead = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Brand New Co 2'").fetchone()
    contact = conn.execute("SELECT * FROM job_contacts WHERE job_key = ?", (lead["normalized_key"],)).fetchone()
    assert contact["name"] == "Manual Name"
    assert contact["email"] == "manual@example.com"
    conn.close()


def test_resolve_communication_no_auto_signature_flag_skips_detection(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    body = "Email: priya.nair@acmestaffing.example"
    record_unmatched_message(
        conn, UnmatchedMessage(message_id="u10", direction="inbound", subject="pitch", body_text=body)
    )
    conn.close()

    rc = resolve_main(
        [
            "--db", str(seeded_db), "--message-id", "u10", "--company", "Brand New Co 3", "--title", "New Role",
            "--create", "--no-auto-signature",
        ]
    )
    assert rc == 0
    assert "Auto-detected from message body" not in capsys.readouterr().out

    conn = connect(seeded_db)
    lead = conn.execute("SELECT normalized_key FROM job_leads WHERE company = 'Brand New Co 3'").fetchone()
    contact = conn.execute("SELECT * FROM job_contacts WHERE job_key = ?", (lead["normalized_key"],)).fetchone()
    assert contact is None
    conn.close()


def test_resolve_communication_missing_args_errors(seeded_db: Path):
    with pytest.raises(SystemExit):
        resolve_main(["--db", str(seeded_db), "--message-id", "u5"])


def test_resolve_communication_unknown_message_id(seeded_db: Path, capsys):
    rc = resolve_main(
        ["--db", str(seeded_db), "--message-id", "nope", "--company", "Clevanoo LLC", "--title", "Senior Full Stack AI/ML Engineer"]
    )
    assert rc == 1
    assert "No unmatched_messages row" in capsys.readouterr().err


def test_resolve_communication_show_prints_full_text(seeded_db: Path, capsys):
    conn = connect(seeded_db)
    record_unmatched_message(
        conn,
        UnmatchedMessage(
            message_id="u7",
            thread_id="t-7",
            direction="inbound",
            from_address="radha@clevanoo.example",
            to_address="me@example.com",
            subject="Re: role details",
            body_text="A" * 500,  # longer than --list's ~160-char preview
        ),
    )
    conn.close()

    rc = resolve_main(["--db", str(seeded_db), "--message-id", "u7", "--show"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "message_id: u7" in out
    assert "radha@clevanoo.example" in out
    assert "Re: role details" in out
    assert "A" * 500 in out


def test_resolve_communication_show_unknown_message_id(seeded_db: Path, capsys):
    rc = resolve_main(["--db", str(seeded_db), "--message-id", "nope", "--show"])
    assert rc == 1
    assert "No unmatched_messages row" in capsys.readouterr().err


def test_resolve_communication_show_requires_message_id(seeded_db: Path):
    with pytest.raises(SystemExit):
        resolve_main(["--db", str(seeded_db), "--show"])


def test_resolve_communication_already_resolved(seeded_db: Path):
    conn = connect(seeded_db)
    key = _key(seeded_db, "Clevanoo LLC", "Senior Full Stack AI/ML Engineer")
    record_unmatched_message(conn, UnmatchedMessage(message_id="u6", direction="inbound", subject="x"))
    resolve_unmatched_message(conn, "u6", key)
    conn.close()

    rc = resolve_main(
        ["--db", str(seeded_db), "--message-id", "u6", "--company", "Clevanoo LLC", "--title", "Senior Full Stack AI/ML Engineer"]
    )
    assert rc == 1
