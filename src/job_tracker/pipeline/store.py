"""SQLite-backed dedup store for discovered job leads.

One row per unique (company, title) normalized key. Re-seeing the same role
(e.g. a digest re-sends it, or it appears via two different senders) updates
last_seen instead of creating a duplicate row.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

from job_tracker.pipeline.models import (
    JobContact,
    JobConversation,
    JobDocument,
    JobLead,
    JobMeeting,
    JobOffer,
    UnmatchedMessage,
    fold_for_key,
    normalize_key,
    utc_now_iso,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = _REPO_ROOT / "var" / "leads.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_leads (
    normalized_key TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    source_message_id TEXT,
    source_label TEXT,
    apply_url TEXT,
    extraction_confidence REAL,
    jd_resolved INTEGER,
    jd_source TEXT,
    jd_text TEXT,
    match_pct REAL,
    matched_skills TEXT,
    verdict TEXT,
    rationale TEXT,
    status TEXT DEFAULT 'new',
    first_seen TEXT,
    last_seen TEXT,
    times_seen INTEGER DEFAULT 1,
    awaiting_response_since TEXT,
    -- NULL = not yet reviewed (default forever, until a human decides via
    -- review_direct_recruiter_outreach.py) — see models.JobLead's docstring.
    direct_recruiter_outreach INTEGER
);

-- One row per email message ever sent through the LLM extraction fallback
-- (pipeline/llm_extract.py), keyed by Gmail message id. Caches the raw
-- parsed response (even when it's an empty list) so re-running the pipeline
-- over the same backlog never re-bills the Anthropic API for a message it
-- has already classified.
CREATE TABLE IF NOT EXISTS llm_extraction_cache (
    message_id TEXT PRIMARY KEY,
    model TEXT,
    roles_json TEXT NOT NULL,
    created_at TEXT
);

-- One row per email message ever run through the triage flow
-- (pipeline/triage.py, scripts/triage_recruiter_inbox.py), keyed by Gmail
-- message id. This is the message-level outcome (PURSUE/SKIP/NEEDS_REVIEW)
-- applied as a Gmail label + archive — distinct from job_leads' per-
-- (company, title) verdict, since one message can fan out into zero, one,
-- or several leads. Also doubles as the "already processed, skip it" check
-- so a re-run of the triage CLI never re-labels or re-bills a message.
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY,
    outcome TEXT NOT NULL,
    subject TEXT,
    from_address TEXT,
    lead_keys TEXT,
    label_applied TEXT,
    archived INTEGER DEFAULT 0,
    processed_at TEXT
);

-- Job CRM join tables (docs/JOB_CRM_VISION.md). Each hangs off a job_leads
-- row via job_key = job_leads.normalized_key. job_leads remains the Job
-- identity row; these answer "who's involved, what was said, what
-- documents exist, what's scheduled, what was offered."
CREATE TABLE IF NOT EXISTS job_contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    contact_ref TEXT,
    name TEXT,
    email TEXT,
    phone TEXT,
    role TEXT DEFAULT 'recruiter',
    source_message_id TEXT,
    first_contacted_at TEXT,
    last_contacted_at TEXT
);

CREATE TABLE IF NOT EXISTS job_conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    contact_id INTEGER,
    message_id TEXT,
    channel TEXT DEFAULT 'email',
    direction TEXT DEFAULT 'inbound',
    summary TEXT,
    occurred_at TEXT
);

CREATE TABLE IF NOT EXISTS job_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    doc_type TEXT NOT NULL,
    path_or_url TEXT,
    version INTEGER DEFAULT 1,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS job_meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    contact_id INTEGER,
    scheduled_at TEXT,
    kind TEXT DEFAULT 'other',
    status TEXT DEFAULT 'proposed',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS job_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key TEXT NOT NULL,
    base_salary REAL,
    bonus REAL,
    equity TEXT,
    benefits_notes TEXT,
    deadline TEXT,
    received_at TEXT,
    decision TEXT DEFAULT 'pending'
);

-- pipeline/comms_match.py's parking lot (2026-07-17) for a communication
-- that couldn't be confidently attached to any tracked job — see
-- models.UnmatchedMessage. message_id is the primary key so a re-scan is
-- naturally idempotent (INSERT OR IGNORE); resolved_job_key/resolved_at
-- stay NULL until scripts/resolve_communication.py (or a later scan that
-- resolves the same thread_id) fills them in. Rows are never deleted, even
-- once resolved, so the manual-resolution history stays auditable.
CREATE TABLE IF NOT EXISTS unmatched_messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT,
    direction TEXT DEFAULT 'inbound',
    from_address TEXT,
    to_address TEXT,
    subject TEXT,
    body_text TEXT,
    detected_at TEXT,
    resolved_job_key TEXT,
    resolved_at TEXT
);
"""

# Columns added after the initial release. New databases get them via
# _SCHEMA above; this backfills any pre-existing var/leads.db in place so
# upgrading never requires deleting stored leads. Table name is explicit
# (rather than assumed to always be job_leads) since 2026-07-14, when the
# first migration targeting a *different* table (job_contacts.phone) showed
# up — see _apply_migrations() below.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("job_leads", "jd_text", "ALTER TABLE job_leads ADD COLUMN jd_text TEXT"),
    # LLM-based evaluation (pipeline/llm_apply.py, CLAUDE.md's JD Match
    # Framework) — kept separate from the original keyword-scorer columns
    # above (match_pct/verdict/rationale) since the two disagree often enough
    # to matter (the keyword scorer produced false "pass"es the LLM verdict
    # caught as genuine "pursue"s in testing) and both are worth keeping for
    # comparison rather than overwriting one with the other.
    ("job_leads", "llm_verdict", "ALTER TABLE job_leads ADD COLUMN llm_verdict TEXT"),
    ("job_leads", "llm_match_pct", "ALTER TABLE job_leads ADD COLUMN llm_match_pct REAL"),
    ("job_leads", "llm_dealbreaker_notes", "ALTER TABLE job_leads ADD COLUMN llm_dealbreaker_notes TEXT"),
    ("job_leads", "llm_skills_alignment", "ALTER TABLE job_leads ADD COLUMN llm_skills_alignment TEXT"),
    ("job_leads", "llm_rationale", "ALTER TABLE job_leads ADD COLUMN llm_rationale TEXT"),
    ("job_leads", "llm_eval_input_tokens", "ALTER TABLE job_leads ADD COLUMN llm_eval_input_tokens INTEGER"),
    ("job_leads", "llm_eval_output_tokens", "ALTER TABLE job_leads ADD COLUMN llm_eval_output_tokens INTEGER"),
    ("job_leads", "llm_eval_cost_usd", "ALTER TABLE job_leads ADD COLUMN llm_eval_cost_usd REAL"),
    ("job_leads", "llm_evaluated_at", "ALTER TABLE job_leads ADD COLUMN llm_evaluated_at TEXT"),
    # Richer JD-review fields (2026-07-07) — job_summary/flags/framing_guidance
    # are new; llm_dealbreaker_notes/llm_skills_alignment above now hold JSON
    # lists of dicts (check/status/notes and requirement/evidence/strength)
    # instead of flat strings, but keep their original column names since
    # they're still "the dealbreaker sweep" and "the skills alignment table"
    # per CLAUDE.md §10 — just structured instead of prose now.
    ("job_leads", "llm_job_summary", "ALTER TABLE job_leads ADD COLUMN llm_job_summary TEXT"),
    ("job_leads", "llm_flags", "ALTER TABLE job_leads ADD COLUMN llm_flags TEXT"),
    ("job_leads", "llm_framing_guidance", "ALTER TABLE job_leads ADD COLUMN llm_framing_guidance TEXT"),
    # CLAUDE.md §10 steps 4-7 (2026-07-11) — structural_verdict/next_step
    # split "does this look good on paper" from the final dealbreaker-aware
    # verdict and surface a concrete escape-hatch action when a dealbreaker
    # is soft/confirmable; cover_letter_strategy/interview_prep synthesize
    # framing_guidance into a narrative paragraph and interview talking
    # points, respectively — see llm_apply.py's _EVAL_SYSTEM_PROMPT.
    ("job_leads", "llm_structural_verdict", "ALTER TABLE job_leads ADD COLUMN llm_structural_verdict TEXT"),
    ("job_leads", "llm_next_step", "ALTER TABLE job_leads ADD COLUMN llm_next_step TEXT"),
    ("job_leads", "llm_cover_letter_strategy", "ALTER TABLE job_leads ADD COLUMN llm_cover_letter_strategy TEXT"),
    ("job_leads", "llm_interview_prep", "ALTER TABLE job_leads ADD COLUMN llm_interview_prep TEXT"),
    # Lifecycle timeline (models.LEAD_STAGES) — one nullable timestamp column
    # per stage after "new", stamped by advance_status() below whenever a
    # lead's status moves forward. Lets a lead's history stay visible (e.g.
    # "applied 2026-06-01, interviewing 2026-06-15") instead of only ever
    # showing whatever stage it's currently in.
    ("job_leads", "pursued_at", "ALTER TABLE job_leads ADD COLUMN pursued_at TEXT"),
    ("job_leads", "package_generated_at", "ALTER TABLE job_leads ADD COLUMN package_generated_at TEXT"),
    ("job_leads", "applied_at", "ALTER TABLE job_leads ADD COLUMN applied_at TEXT"),
    ("job_leads", "following_up_at", "ALTER TABLE job_leads ADD COLUMN following_up_at TEXT"),
    ("job_leads", "interviewing_at", "ALTER TABLE job_leads ADD COLUMN interviewing_at TEXT"),
    ("job_leads", "offered_at", "ALTER TABLE job_leads ADD COLUMN offered_at TEXT"),
    ("job_leads", "accepted_at", "ALTER TABLE job_leads ADD COLUMN accepted_at TEXT"),
    ("job_leads", "started_at", "ALTER TABLE job_leads ADD COLUMN started_at TEXT"),
    ("job_leads", "skipped_at", "ALTER TABLE job_leads ADD COLUMN skipped_at TEXT"),
    # Rejection tracking (2026-07-14) — "rejected" is its own LEAD_STAGES
    # off-ramp, distinct from "skipped" (see models.py). rejected_at is the
    # stage timestamp advance_status() stamps; the other three hold the
    # rejection email's own details for reference/audit, filled in whenever
    # a detected rejection is confirmed against this lead (a manual step —
    # see docs on the pending-rejection review flow).
    ("job_leads", "rejected_at", "ALTER TABLE job_leads ADD COLUMN rejected_at TEXT"),
    ("job_leads", "rejection_source", "ALTER TABLE job_leads ADD COLUMN rejection_source TEXT"),
    ("job_leads", "rejection_email_text", "ALTER TABLE job_leads ADD COLUMN rejection_email_text TEXT"),
    ("job_leads", "rejection_message_id", "ALTER TABLE job_leads ADD COLUMN rejection_message_id TEXT"),
    # Soft-delete (2026-07-16) — "deleted" LEAD_STAGES off-ramp; hides from
    # default list/pending views while keeping the row + CRM children.
    ("job_leads", "deleted_at", "ALTER TABLE job_leads ADD COLUMN deleted_at TEXT"),
    # Req no longer available (2026-07-16) — closed/filled/withdrawn.
    ("job_leads", "unavailable_at", "ALTER TABLE job_leads ADD COLUMN unavailable_at TEXT"),
    # Already hired (2026-07-16) — you took another offer, or this req hired
    # someone else. Distinct from accepted_at/started_at (this lead's offer).
    ("job_leads", "hired_at", "ALTER TABLE job_leads ADD COLUMN hired_at TEXT"),
    # "Whose turn is it" (2026-07-14) — orthogonal to `status`/LEAD_STAGES
    # (see models.py): a lead can be `applied` *and* waiting-on-them, or
    # `interviewing` *and* waiting-on-them, so this isn't a stage of its
    # own. Auto-set to now by add_job_conversation() whenever an `outbound`
    # conversation is logged (you just spoke, it's their turn), auto-cleared
    # whenever an `inbound` one comes in (they responded) — with a manual
    # override available via the same function for conversations that don't
    # cleanly fit that rule (e.g. a phone call logged after the fact).
    ("job_leads", "awaiting_response_since", "ALTER TABLE job_leads ADD COLUMN awaiting_response_since TEXT"),
    # "Has a human recruiter personally reached out about this lead" flag
    # (2026-07-21) — see models.JobLead.direct_recruiter_outreach's
    # docstring. NULL ("not yet reviewed") by default; only ever set by a
    # human via scripts/review_direct_recruiter_outreach.py.
    ("job_leads", "direct_recruiter_outreach", "ALTER TABLE job_leads ADD COLUMN direct_recruiter_outreach INTEGER"),
    ("job_contacts", "phone", "ALTER TABLE job_contacts ADD COLUMN phone TEXT"),
    # Communications archival (2026-07-17) — see models.JobConversation and
    # pipeline/comms_match.py's Tier-1 thread-id matching.
    ("job_conversations", "thread_id", "ALTER TABLE job_conversations ADD COLUMN thread_id TEXT"),
    ("job_conversations", "body_text", "ALTER TABLE job_conversations ADD COLUMN body_text TEXT"),
]

# models.LEAD_STAGES -> the timestamp column stamped when a lead enters that
# stage. "new" has none (first_seen already covers it).
_STAGE_DATE_COLUMNS: dict[str, str] = {
    "pursued": "pursued_at",
    "package_generated": "package_generated_at",
    "applied": "applied_at",
    "following_up": "following_up_at",
    "interviewing": "interviewing_at",
    "offered": "offered_at",
    "accepted": "accepted_at",
    "started": "started_at",
    "skipped": "skipped_at",
    "rejected": "rejected_at",
    "deleted": "deleted_at",
    "unavailable": "unavailable_at",
    "hired": "hired_at",
}


def _migrate_pursued_skipped_rename(conn: sqlite3.Connection) -> None:
    """One-time migration (2026-07-07): LEAD_STAGES' "approved"/"passed"
    stages were renamed to "pursued"/"skipped" to match the Gmail PURSUE/SKIP
    outcome labels (see gmail_writer.PURSUE_LABEL/SKIP_LABEL). Renames the
    matching timestamp columns in place (preserving their data, rather than
    letting `_apply_migrations`' generic add-missing-column loop below add
    fresh empty pursued_at/skipped_at columns and orphan the old ones) and
    rewrites any already-stored status values to match. Must run before that
    loop. Idempotent: a no-op on every run after the first for a given DB,
    including brand-new ones that never had the old columns at all.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(job_leads)")}
    if "approved_at" in existing and "pursued_at" not in existing:
        conn.execute("ALTER TABLE job_leads RENAME COLUMN approved_at TO pursued_at")
        conn.execute("UPDATE job_leads SET status = 'pursued' WHERE status = 'approved'")
    if "passed_at" in existing and "skipped_at" not in existing:
        conn.execute("ALTER TABLE job_leads RENAME COLUMN passed_at TO skipped_at")
        conn.execute("UPDATE job_leads SET status = 'skipped' WHERE status = 'passed'")
    conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    _migrate_pursued_skipped_rename(conn)
    existing_by_table: dict[str, set[str]] = {}
    for table, column, ddl in _MIGRATIONS:
        if table not in existing_by_table:
            existing_by_table[table] = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing_by_table[table]:
            conn.execute(ddl)
            existing_by_table[table].add(column)
    conn.commit()


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    _apply_migrations(conn)
    return conn


def canonicalize_company_casing(conn: sqlite3.Connection, company: str) -> str:
    """Reuse whichever casing is already on file for this company (e.g. an
    incoming "NiCE" becomes "NICE" if "NICE" is already stored), rather than
    letting two casing variants of the same normalized_key-company-prefix
    sit side by side (the exact bug fixed manually on 2026-07-21 for
    NICE/NiCE and Latter-Day/Latter-day). Added 2026-07-21 to stop it from
    recurring, at ingestion time, automatically.

    Exact match only (case-insensitive, punctuation-insensitive — the same
    fold `normalize_key()` itself applies via `fold_for_key()`) — a company
    only gets its casing rewritten here if doing so is 100% risk-free,
    i.e. it doesn't change the resulting normalized_key at all. Deliberately
    NOT a fuzzy or corporate-suffix-stripped match (e.g. "Scribd" vs.
    "Scribd, Inc.") — that's a different, riskier question ("is this
    actually the same company, just written differently?") that changes
    which normalized_key a lead lands under, so it belongs to the
    human-reviewed `find_duplicate_companies.py` + `merge_leads.py` flow
    instead of silent auto-rewriting here.
    """
    folded = fold_for_key(company)
    if not folded:
        return company
    row = conn.execute(
        "SELECT company FROM job_leads WHERE normalized_key LIKE ? || '::%' LIMIT 1",
        (folded,),
    ).fetchone()
    if row is not None and row["company"] != company:
        return row["company"]
    return company


def upsert_lead(conn: sqlite3.Connection, lead: JobLead) -> bool:
    """Insert a new lead or refresh an existing one. Returns True if new."""
    key = lead.normalized_key
    existing = conn.execute(
        "SELECT normalized_key, status FROM job_leads WHERE normalized_key = ?", (key,)
    ).fetchone()

    if existing is None:
        lead.company = canonicalize_company_casing(conn, lead.company)
        key = lead.normalized_key  # unchanged by construction, but stay honest
        conn.execute(
            """
            INSERT INTO job_leads (
                normalized_key, company, title, source_message_id, source_label,
                apply_url, extraction_confidence, jd_resolved, jd_source, jd_text,
                match_pct, matched_skills, verdict, rationale, status,
                first_seen, last_seen, times_seen, direct_recruiter_outreach
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                key,
                lead.company,
                lead.title,
                lead.source_message_id,
                lead.source_label,
                lead.apply_url,
                lead.extraction_confidence,
                int(lead.jd_resolved),
                lead.jd_source,
                lead.jd_text,
                lead.match_pct,
                json.dumps(lead.matched_skills),
                lead.verdict,
                json.dumps(lead.rationale),
                lead.status,
                lead.first_seen,
                lead.last_seen,
                None if lead.direct_recruiter_outreach is None else int(lead.direct_recruiter_outreach),
            ),
        )
        conn.commit()
        return True

    # Preserve any manual status the user already set (e.g. "pursued"),
    # just bump last_seen/times_seen and refresh scoring (and the JD text it
    # was based on) if it's still "new". Once a human has triaged a lead,
    # a re-send of the same digest shouldn't silently overwrite their record.
    conn.execute(
        """
        UPDATE job_leads
        SET last_seen = ?,
            times_seen = times_seen + 1,
            match_pct = CASE WHEN status = 'new' THEN ? ELSE match_pct END,
            matched_skills = CASE WHEN status = 'new' THEN ? ELSE matched_skills END,
            verdict = CASE WHEN status = 'new' THEN ? ELSE verdict END,
            rationale = CASE WHEN status = 'new' THEN ? ELSE rationale END,
            jd_resolved = CASE WHEN status = 'new' THEN ? ELSE jd_resolved END,
            jd_source = CASE WHEN status = 'new' THEN ? ELSE jd_source END,
            jd_text = CASE WHEN status = 'new' THEN ? ELSE jd_text END
        WHERE normalized_key = ?
        """,
        (
            utc_now_iso(),
            lead.match_pct,
            json.dumps(lead.matched_skills),
            lead.verdict,
            json.dumps(lead.rationale),
            int(lead.jd_resolved),
            lead.jd_source,
            lead.jd_text,
            key,
        ),
    )
    conn.commit()
    return False


def get_llm_cache(conn: sqlite3.Connection, message_id: str) -> list[dict] | None:
    """Return the cached LLM extraction items for `message_id`, or None on a
    cache miss (never called yet for this message)."""
    row = conn.execute(
        "SELECT roles_json FROM llm_extraction_cache WHERE message_id = ?", (message_id,)
    ).fetchone()
    if row is None:
        return None
    return json.loads(row["roles_json"])


def set_llm_cache(conn: sqlite3.Connection, message_id: str, model: str, items: list[dict]) -> None:
    conn.execute(
        """
        INSERT INTO llm_extraction_cache (message_id, model, roles_json, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            model = excluded.model,
            roles_json = excluded.roles_json,
            created_at = excluded.created_at
        """,
        (message_id, model, json.dumps(items), utc_now_iso()),
    )
    conn.commit()


def list_leads(
    conn: sqlite3.Connection,
    *,
    verdict: str | None = None,
    include_deleted: bool = False,
) -> list[sqlite3.Row]:
    """All leads, optionally filtered by keyword-scorer verdict.

    Soft-deleted / unavailable / hired leads (`status` in
    `deleted`/`unavailable`/`hired`) are omitted by default so day-to-day
    review CLIs and pending-actions don't keep resurfacing duplicates, junk,
    closed reqs, or leads closed because someone was already hired. Pass
    `include_deleted=True` (or filter `--status deleted` /
    `--status unavailable` / `--status hired` in list_leads.py) when you
    need them back.
    """
    clauses: list[str] = []
    params: list[str] = []
    if verdict:
        clauses.append("verdict = ?")
        params.append(verdict)
    if not include_deleted:
        clauses.append("status NOT IN ('deleted', 'unavailable', 'hired')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return list(
        conn.execute(
            f"SELECT * FROM job_leads {where} ORDER BY match_pct DESC, last_seen DESC",
            params,
        )
    )


def get_job(conn: sqlite3.Connection, company: str, title: str) -> sqlite3.Row | None:
    """Exact-match lookup by (company, title) — the "find the job I meant"
    helper shared by the manual CLIs (log_contact.py, attach_document.py,
    generate_message.py) that identify a job by company/title rather than
    by normalized_key directly."""
    return conn.execute(
        "SELECT * FROM job_leads WHERE normalized_key = ?", (normalize_key(company, title),)
    ).fetchone()


def get_sibling_titles(conn: sqlite3.Connection, company: str, *, exclude_title: str | None = None) -> list[str]:
    """All distinct titles already tracked for this company (any status),
    optionally excluding one — used to decide the on-disk artifact layout
    (see llm_apply.py's `_job_folder`): a company with only one tracked
    lead gets a flat `<Company>/` folder; once a second lead exists,
    both get their own `<Company>/<Company>_<Title>/` subfolder so files
    from different roles at the same company never collide."""
    rows = conn.execute("SELECT DISTINCT title FROM job_leads WHERE company = ?", (company,)).fetchall()
    titles = [r[0] for r in rows]
    if exclude_title is not None:
        titles = [t for t in titles if t != exclude_title]
    return titles


def list_leads_needing_llm_eval(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Leads with JD text on file that haven't been through the LLM-based
    evaluator (pipeline/llm_apply.evaluate_lead) yet — the batch this repo's
    evaluate-backlog CLI should spend money on."""
    return list(
        conn.execute(
            """
            SELECT * FROM job_leads
            WHERE llm_verdict IS NULL AND jd_text IS NOT NULL AND jd_text != ''
            ORDER BY first_seen ASC
            """
        )
    )


def list_leads_awaiting_full_llm_review(conn: sqlite3.Connection, min_match_pct: float) -> list[sqlite3.Row]:
    """Mirrors `render_pending_actions.py`'s "Awaiting full-LLM-review" bucket
    criteria exactly (2026-07-19): still `status='new'` (nothing past initial
    triage has happened, so no human decision to preserve), a real rule-based
    verdict (not the special "REVIEW NEEDED" unresolved-JD marker — that's a
    different dashboard bucket entirely), JD text on file to actually
    evaluate, no full-LLM-review yet, and the free rule-based score already
    at or above the cost gate (`config/framework.yaml`'s
    `llm_review_min_pct`, passed in by the caller rather than hardcoded here
    so `scoring.scorer`'s copy of that threshold stays the single source of
    truth).

    Added to close a real gap found live (2026-07-19): leads land here via
    two paths — a normal digest whose rule-based score cleared the gate but
    whose LLM call hadn't run yet at triage time, or (more often, in
    practice) `scan_communications.py`'s deliberate "no happy path" stub-lead
    creation (see that module's docstring), which creates a lead with only a
    free rule-based score and explicitly stops there. Either way, nothing
    in `run_cycle.sh` used to ever revisit these — some sat here for 12+ days
    with a 100% match and zero further action. `cli/process_awaiting_llm_review.py`
    is the automated sweep that now closes the loop every hour."""
    return list(
        conn.execute(
            """
            SELECT * FROM job_leads
            WHERE status = 'new'
              AND verdict != 'REVIEW NEEDED'
              AND (llm_verdict IS NULL OR llm_verdict = '')
              AND jd_text IS NOT NULL AND jd_text != ''
              AND match_pct >= ?
              AND deleted_at IS NULL
            ORDER BY first_seen ASC
            """,
            (min_match_pct,),
        )
    )


def advance_status(
    conn: sqlite3.Connection, normalized_key: str, stage: str, *, when: str | None = None
) -> None:
    """Move a lead to `stage` (one of models.LEAD_STAGES) and, unless it's
    "new", stamp the matching `<stage>_at` timestamp column with `when`
    (defaults to now). Never rewrites an already-set stage timestamp if
    called again for the same stage (e.g. re-running the triage CLI over an
    already-pursued lead), so the timeline records the *first* time a lead
    reached that stage.
    """
    from job_tracker.pipeline.models import LEAD_STAGES

    if stage not in LEAD_STAGES:
        raise ValueError(f"unknown lead stage {stage!r}; must be one of {LEAD_STAGES}")

    date_column = _STAGE_DATE_COLUMNS.get(stage)
    if date_column is None:
        conn.execute("UPDATE job_leads SET status = ? WHERE normalized_key = ?", (stage, normalized_key))
    else:
        conn.execute(
            f"""
            UPDATE job_leads
            SET status = ?, {date_column} = COALESCE({date_column}, ?)
            WHERE normalized_key = ?
            """,
            (stage, when or utc_now_iso(), normalized_key),
        )
    conn.commit()


def record_rejection(
    conn: sqlite3.Connection,
    normalized_key: str,
    *,
    source: str = "",
    email_text: str = "",
    message_id: str = "",
    when: str | None = None,
) -> None:
    """Confirm a detected rejection against a specific lead: advances it to
    the "rejected" stage (stamping rejected_at, same as any other
    advance_status() call) and fills in the rejection's own audit details.
    Always a deliberate, one-at-a-time call — never invoked automatically
    from the triage pipeline itself (see find_recent_rejection() for the
    read-side disqualification check that consumes what this writes)."""
    advance_status(conn, normalized_key, "rejected", when=when)
    conn.execute(
        """
        UPDATE job_leads
        SET rejection_source = ?, rejection_email_text = ?, rejection_message_id = ?
        WHERE normalized_key = ?
        """,
        (source, email_text, message_id, normalized_key),
    )
    conn.commit()


def mark_lead_deleted(
    conn: sqlite3.Connection,
    normalized_key: str,
    *,
    when: str | None = None,
    reason: str = "",
) -> None:
    """Soft-delete a lead: set status='deleted' (stamping deleted_at) and
    optionally log a conversation note with `reason`. CRM children are kept.
    Clears awaiting_response_since so deleted leads don't show up as waiting.
    """
    _mark_lead_hidden(
        conn,
        normalized_key,
        stage="deleted",
        when=when,
        reason=reason,
        summary_prefix="deleted",
    )


def mark_lead_unavailable(
    conn: sqlite3.Connection,
    normalized_key: str,
    *,
    when: str | None = None,
    reason: str = "",
) -> None:
    """Mark a lead no-longer-available (req closed/filled/withdrawn):
    status='unavailable' (stamping unavailable_at). CRM children kept.
    Clears awaiting_response_since.
    """
    _mark_lead_hidden(
        conn,
        normalized_key,
        stage="unavailable",
        when=when,
        reason=reason or "no longer available",
        summary_prefix="unavailable",
    )


def mark_lead_hired(
    conn: sqlite3.Connection,
    normalized_key: str,
    *,
    when: str | None = None,
    reason: str = "",
) -> None:
    """Mark a lead already-hired (you took another offer, or this req hired
    someone else): status='hired' (stamping hired_at). Distinct from
    accepted/started on *this* lead's offer. CRM children kept.
    Clears awaiting_response_since.
    """
    _mark_lead_hidden(
        conn,
        normalized_key,
        stage="hired",
        when=when,
        reason=reason or "already hired",
        summary_prefix="hired",
    )


def _mark_lead_hidden(
    conn: sqlite3.Connection,
    normalized_key: str,
    *,
    stage: str,
    when: str | None,
    reason: str,
    summary_prefix: str,
) -> None:
    advance_status(conn, normalized_key, stage, when=when)
    conn.execute(
        "UPDATE job_leads SET awaiting_response_since = NULL WHERE normalized_key = ?",
        (normalized_key,),
    )
    if reason:
        from job_tracker.pipeline.models import JobConversation

        add_job_conversation(
            conn,
            JobConversation(
                job_key=normalized_key,
                channel="other",
                direction="other",
                summary=f"{summary_prefix}: {reason}",
                occurred_at=when or utc_now_iso(),
            ),
            awaiting_response=False,
        )
    else:
        conn.commit()


def rename_company(conn: sqlite3.Connection, *, from_company: str, to_company: str) -> int:
    """Relabel every `job_leads.company` cell that exactly matches
    `from_company` to `to_company` — for the common
    `find_duplicate_companies.py` case where two spellings really are the
    same company but the flagged leads are genuinely *different job
    postings* (different titles), so merging them into one lead would
    wrongly discard one posting's own identity and CRM history. Compare
    `merge_leads()`, which is for two rows that are the same posting.

    Does NOT touch `normalized_key` on any row (each lead's key, and every
    CRM child row's `job_key`, is completely unaffected) — this only
    changes the display string and, therefore, which company-folder
    `render_pending_actions.py`'s `_lead_folder_and_count` groups a lead
    under going forward. Nothing on the filesystem is touched — if a
    renamed lead already had a package folder generated under the old
    company name, move those files to the new folder by hand.

    Matches `from_company` by exact string, not a fold/fuzzy match — pass
    the precise spelling `find_duplicate_companies.py` printed. Returns the
    number of rows updated.
    """
    cur = conn.execute("UPDATE job_leads SET company = ? WHERE company = ?", (to_company, from_company))
    conn.commit()
    return cur.rowcount


def merge_leads(conn: sqlite3.Connection, *, keep_key: str, absorb_key: str) -> dict[str, int]:
    """Merge two leads that are actually the same real job/company — e.g. a
    `find_duplicate_companies.py`-flagged pair like "Scribd" / "Scribd, Inc."
    that `canonicalize_company_casing()` couldn't safely reconcile on its
    own (different normalized_key, so it's a human call). Written for
    `cli/merge_leads.py` (2026-07-21); not reachable from the ingestion
    pipeline.

    Every CRM child row (contacts, conversations, documents, meetings,
    offers) currently under `absorb_key` is re-keyed to `keep_key`, and any
    `unmatched_messages.resolved_job_key` pointer is updated too, so nothing
    is silently orphaned. The surviving `keep_key` row is enriched with
    whatever the absorbed row had that it didn't — the earlier of the two
    `first_seen`, the later of the two `last_seen`, summed `times_seen`, the
    absorbed row's `direct_recruiter_outreach` decision if the survivor was
    still undecided, and the absorbed row's `jd_text` if the survivor's was
    empty. Every other field on the surviving row (status, verdict, match_pct,
    etc.) is left exactly as-is — pick which key to `--keep` based on which
    one has the more-advanced status/real history.

    The absorbed `job_leads` row itself is then hard-deleted. Deliberately
    does NOT touch anything on the filesystem — if `absorb_key` had its own
    documents folder on disk, review/move those files manually.

    Raises ValueError if `keep_key == absorb_key` or either key isn't a
    real row. Returns per-table reassignment counts plus `absorbed_lead: 1`.
    """
    if keep_key == absorb_key:
        raise ValueError("--keep and --absorb must refer to two different leads")

    keep_row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (keep_key,)).fetchone()
    absorb_row = conn.execute("SELECT * FROM job_leads WHERE normalized_key = ?", (absorb_key,)).fetchone()
    if keep_row is None:
        raise ValueError(f"--keep key not found in job_leads: {keep_key!r}")
    if absorb_row is None:
        raise ValueError(f"--absorb key not found in job_leads: {absorb_key!r}")

    new_dro = keep_row["direct_recruiter_outreach"]
    if new_dro is None:
        new_dro = absorb_row["direct_recruiter_outreach"]
    conn.execute(
        """
        UPDATE job_leads
        SET first_seen = ?,
            last_seen = ?,
            times_seen = ?,
            direct_recruiter_outreach = ?,
            jd_text = ?
        WHERE normalized_key = ?
        """,
        (
            min(keep_row["first_seen"], absorb_row["first_seen"]),
            max(keep_row["last_seen"], absorb_row["last_seen"]),
            (keep_row["times_seen"] or 0) + (absorb_row["times_seen"] or 0),
            new_dro,
            keep_row["jd_text"] or absorb_row["jd_text"],
            keep_key,
        ),
    )

    counts = {
        "contacts": conn.execute(
            "UPDATE job_contacts SET job_key = ? WHERE job_key = ?", (keep_key, absorb_key)
        ).rowcount,
        "conversations": conn.execute(
            "UPDATE job_conversations SET job_key = ? WHERE job_key = ?", (keep_key, absorb_key)
        ).rowcount,
        "documents": conn.execute(
            "UPDATE job_documents SET job_key = ? WHERE job_key = ?", (keep_key, absorb_key)
        ).rowcount,
        "meetings": conn.execute(
            "UPDATE job_meetings SET job_key = ? WHERE job_key = ?", (keep_key, absorb_key)
        ).rowcount,
        "offers": conn.execute(
            "UPDATE job_offers SET job_key = ? WHERE job_key = ?", (keep_key, absorb_key)
        ).rowcount,
        "unmatched_messages": conn.execute(
            "UPDATE unmatched_messages SET resolved_job_key = ? WHERE resolved_job_key = ?",
            (keep_key, absorb_key),
        ).rowcount,
    }
    conn.execute("DELETE FROM job_leads WHERE normalized_key = ?", (absorb_key,))
    conn.commit()
    counts["absorbed_lead"] = 1
    return counts


def purge_lead(conn: sqlite3.Connection, normalized_key: str) -> dict[str, int]:
    """Hard-delete a lead and all CRM children (contacts, conversations,
    documents, meetings, offers). Irreversible. Returns per-table delete counts.
    """
    counts = {
        "conversations": conn.execute(
            "DELETE FROM job_conversations WHERE job_key = ?", (normalized_key,)
        ).rowcount,
        "contacts": conn.execute(
            "DELETE FROM job_contacts WHERE job_key = ?", (normalized_key,)
        ).rowcount,
        "documents": conn.execute(
            "DELETE FROM job_documents WHERE job_key = ?", (normalized_key,)
        ).rowcount,
        "meetings": conn.execute(
            "DELETE FROM job_meetings WHERE job_key = ?", (normalized_key,)
        ).rowcount,
        "offers": conn.execute(
            "DELETE FROM job_offers WHERE job_key = ?", (normalized_key,)
        ).rowcount,
        "leads": conn.execute(
            "DELETE FROM job_leads WHERE normalized_key = ?", (normalized_key,)
        ).rowcount,
    }
    conn.commit()
    return counts


def is_message_processed(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM processed_messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is not None


def processed_at(conn: sqlite3.Connection, message_id: str) -> str | None:
    """The message's `processed_at` timestamp, or None if never processed.
    Used to resume a `--force` batch that was interrupted partway through
    (see `--force-since` in `cli/triage_recruiter_inbox.py`) without
    re-billing whatever it already got through before stopping."""
    row = conn.execute(
        "SELECT processed_at FROM processed_messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row[0] if row else None


def list_processed_messages_with_leads(conn: sqlite3.Connection) -> list[dict]:
    """Every `processed_messages` row that has at least one linked lead
    (`lead_keys` non-empty) — i.e. everything `cli/resync_labels.py` could
    possibly have a reason to relabel. NEEDS_REVIEW messages with no
    extracted roles at all (`lead_keys == []`) are excluded here since
    there's no lead verdict to resync against; they stay however they were
    left at initial triage. `lead_keys` comes back already JSON-decoded."""
    rows = conn.execute(
        "SELECT message_id, outcome, label_applied, lead_keys FROM processed_messages "
        "WHERE lead_keys IS NOT NULL AND lead_keys != '' AND lead_keys != '[]'"
    ).fetchall()
    result = []
    for row in rows:
        keys = json.loads(row["lead_keys"] or "[]")
        if keys:
            result.append({"message_id": row["message_id"], "outcome": row["outcome"], "lead_keys": keys})
    return result


def record_message_processed(
    conn: sqlite3.Connection,
    message_id: str,
    *,
    outcome: str,
    subject: str = "",
    from_address: str = "",
    lead_keys: list[str] | None = None,
    label_applied: str = "",
    archived: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO processed_messages (
            message_id, outcome, subject, from_address, lead_keys,
            label_applied, archived, processed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(message_id) DO UPDATE SET
            outcome = excluded.outcome,
            subject = excluded.subject,
            from_address = excluded.from_address,
            lead_keys = excluded.lead_keys,
            label_applied = excluded.label_applied,
            archived = excluded.archived,
            processed_at = excluded.processed_at
        """,
        (
            message_id,
            outcome,
            subject,
            from_address,
            json.dumps(lead_keys or []),
            label_applied,
            int(archived),
            utc_now_iso(),
        ),
    )
    conn.commit()


# --- Job CRM: contacts, conversations, documents, meetings, offers --------
# (docs/JOB_CRM_VISION.md §4). Kept in this file rather than a separate
# module since they share `conn` and the same simple insert/list shape as
# everything above.


def add_job_contact(conn: sqlite3.Connection, contact: JobContact) -> int:
    """Insert a JobContact, or — if a matching row already exists for this
    job_key — just bump `last_contacted_at` (and backfill `name`/`phone` if
    this call supplies one the stored row doesn't have yet — e.g. a manual
    `log_contact.py` call filling in a phone number for a contact
    auto-created from an email that never had one) and return the existing
    row's id. This is what makes UC-1 (ingest) and UC-2 (dedupe) safe to call
    repeatedly for the same sender without piling up duplicate contact rows.

    Two dedupe keys, tried in order: (job_key, email) when this call has an
    email; falling back to (job_key, name) among the job's other
    email-less contacts when it doesn't — added 2026-07-17 after
    `pipeline/signature.py` backfilling from several messages for the same
    job (each with a name but no email) started piling up duplicate
    name-only rows that the email-only key never caught."""
    email = (contact.email or "").strip().lower()
    name = (contact.name or "").strip().lower()
    existing = None
    if email:
        existing = conn.execute(
            "SELECT id, name, phone FROM job_contacts WHERE job_key = ? AND lower(email) = ?",
            (contact.job_key, email),
        ).fetchone()
    if existing is None and not email and name:
        existing = conn.execute(
            "SELECT id, name, phone FROM job_contacts WHERE job_key = ? AND lower(name) = ? "
            "AND (email IS NULL OR email = '')",
            (contact.job_key, name),
        ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE job_contacts SET last_contacted_at = ?, name = ?, phone = ? WHERE id = ?",
            (
                utc_now_iso(),
                contact.name or existing["name"],
                contact.phone or existing["phone"],
                existing["id"],
            ),
        )
        conn.commit()
        return existing["id"]

    cursor = conn.execute(
        """
        INSERT INTO job_contacts (
            job_key, contact_ref, name, email, phone, role, source_message_id,
            first_contacted_at, last_contacted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact.job_key,
            contact.contact_ref,
            contact.name,
            contact.email,
            contact.phone,
            contact.role,
            contact.source_message_id,
            contact.first_contacted_at,
            contact.last_contacted_at,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def list_job_contacts(conn: sqlite3.Connection, job_key: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM job_contacts WHERE job_key = ? ORDER BY first_contacted_at ASC",
            (job_key,),
        )
    )


def list_all_contacts(conn: sqlite3.Connection, *, company: str | None = None) -> list[sqlite3.Row]:
    """Every JobContact across every job, joined with the job's own
    company/title — the basis for `list_contacts.py`'s report, since
    `job_contacts` alone has no company column (it's implicit via job_key)."""
    sql = """
        SELECT jc.*, jl.company AS job_company, jl.title AS job_title
        FROM job_contacts jc
        JOIN job_leads jl ON jl.normalized_key = jc.job_key
    """
    params: tuple = ()
    if company:
        sql += " WHERE lower(jl.company) LIKE ?"
        params = (f"%{company.lower()}%",)
    sql += " ORDER BY jl.company ASC, jc.first_contacted_at ASC"
    return list(conn.execute(sql, params))


def add_job_conversation(
    conn: sqlite3.Connection, conversation: JobConversation, *, awaiting_response: bool | None = None
) -> int:
    """Insert a JobConversation and update the job's `awaiting_response_since`
    ("whose turn is it" — see the migration comment in _MIGRATIONS) as a
    side effect: an `outbound` conversation (you spoke) sets it to this
    conversation's `occurred_at`; an `inbound` one (they spoke) clears it.
    `direction == "other"` leaves it untouched by default. `awaiting_response`
    overrides that inference outright (True -> set, False -> clear,
    regardless of direction) for cases direction alone doesn't capture well
    — e.g. a phone call you logged after the fact where you left a
    voicemail and are still the one waiting."""
    cursor = conn.execute(
        """
        INSERT INTO job_conversations (
            job_key, contact_id, message_id, channel, direction, summary, occurred_at,
            thread_id, body_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conversation.job_key,
            conversation.contact_id,
            conversation.message_id,
            conversation.channel,
            conversation.direction,
            conversation.summary,
            conversation.occurred_at,
            conversation.thread_id,
            conversation.body_text,
        ),
    )

    if awaiting_response is True:
        waiting_since: str | None = conversation.occurred_at
    elif awaiting_response is False:
        waiting_since = None
    elif conversation.direction == "outbound":
        waiting_since = conversation.occurred_at
    elif conversation.direction == "inbound":
        waiting_since = None
    else:
        waiting_since = "__unchanged__"

    if waiting_since != "__unchanged__":
        conn.execute(
            "UPDATE job_leads SET awaiting_response_since = ? WHERE normalized_key = ?",
            (waiting_since, conversation.job_key),
        )

    conn.commit()
    return cursor.lastrowid


def set_awaiting_response(
    conn: sqlite3.Connection, normalized_key: str, waiting: bool, *, when: str | None = None
) -> None:
    """Directly set/clear a job's `awaiting_response_since` — the standalone
    escape hatch for callers that aren't logging a conversation at the same
    time (e.g. `log_contact.py --meeting` recording a completed interview:
    no new JobConversation row, but you're now waiting on feedback)."""
    conn.execute(
        "UPDATE job_leads SET awaiting_response_since = ? WHERE normalized_key = ?",
        (when or utc_now_iso() if waiting else None, normalized_key),
    )
    conn.commit()


def set_direct_recruiter_outreach(conn: sqlite3.Connection, normalized_key: str, value: bool | None) -> None:
    """The only writer of `job_leads.direct_recruiter_outreach` (2026-07-21
    redesign — see models.JobLead's docstring): a human's explicit
    yes/no/undecided, from `cli/review_direct_recruiter_outreach.py`'s
    interactive prompt (`True`/`False` only) or `cli/set_direct_recruiter_
    outreach.py`'s one-shot CLI (all three, incl. `None` to explicitly
    reset back to undecided — added 2026-07-21 for the dashboard's inline
    tri-state selector; see `directRecruiterCellHtml()` in
    render_pending_actions.py). Deliberately not reachable from the
    ingestion pipeline itself."""
    conn.execute(
        "UPDATE job_leads SET direct_recruiter_outreach = ? WHERE normalized_key = ?",
        (None if value is None else int(value), normalized_key),
    )
    conn.commit()


def list_undecided_direct_recruiter_outreach(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every lead a human hasn't yet reviewed for `direct_recruiter_outreach`
    — the review queue `scripts/review_direct_recruiter_outreach.py` walks,
    oldest-first (see `first_seen`)."""
    return list(
        conn.execute(
            "SELECT normalized_key, company, title, status, source_label, jd_text, first_seen FROM job_leads "
            "WHERE direct_recruiter_outreach IS NULL ORDER BY first_seen ASC"
        )
    )


def list_job_conversations(conn: sqlite3.Connection, job_key: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM job_conversations WHERE job_key = ? ORDER BY occurred_at ASC",
            (job_key,),
        )
    )


def latest_conversation_at(conn: sqlite3.Connection, job_key: str) -> str | None:
    """Most recent `occurred_at` for a job — the basis for UC-6 follow-up
    nudges ("this job has gone quiet since <date>")."""
    row = conn.execute(
        "SELECT MAX(occurred_at) AS latest FROM job_conversations WHERE job_key = ?",
        (job_key,),
    ).fetchone()
    return row["latest"] if row else None


def add_job_document(conn: sqlite3.Connection, document: JobDocument) -> int:
    """Insert a JobDocument. If `version` wasn't set explicitly (still the
    dataclass default of 1) and this job already has a document of the same
    `doc_type`, auto-increment instead of colliding — e.g. a second résumé
    revision for the same job becomes version 2, not another version 1."""
    version = document.version
    if version == 1:
        row = conn.execute(
            "SELECT MAX(version) AS max_version FROM job_documents WHERE job_key = ? AND doc_type = ?",
            (document.job_key, document.doc_type),
        ).fetchone()
        if row and row["max_version"]:
            version = row["max_version"] + 1

    cursor = conn.execute(
        """
        INSERT INTO job_documents (job_key, doc_type, path_or_url, version, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (document.job_key, document.doc_type, document.path_or_url, version, document.created_at),
    )
    conn.commit()
    return cursor.lastrowid


def list_job_documents(conn: sqlite3.Connection, job_key: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM job_documents WHERE job_key = ? ORDER BY doc_type ASC, version ASC",
            (job_key,),
        )
    )


def add_job_meeting(conn: sqlite3.Connection, meeting: JobMeeting) -> int:
    cursor = conn.execute(
        """
        INSERT INTO job_meetings (job_key, contact_id, scheduled_at, kind, status, notes)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (meeting.job_key, meeting.contact_id, meeting.scheduled_at, meeting.kind, meeting.status, meeting.notes),
    )
    conn.commit()
    return cursor.lastrowid


def list_job_meetings(conn: sqlite3.Connection, job_key: str) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM job_meetings WHERE job_key = ? ORDER BY scheduled_at ASC",
            (job_key,),
        )
    )


def add_job_offer(conn: sqlite3.Connection, offer: JobOffer) -> int:
    cursor = conn.execute(
        """
        INSERT INTO job_offers (
            job_key, base_salary, bonus, equity, benefits_notes, deadline, received_at, decision
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            offer.job_key,
            offer.base_salary,
            offer.bonus,
            offer.equity,
            offer.benefits_notes,
            offer.deadline,
            offer.received_at,
            offer.decision,
        ),
    )
    conn.commit()
    return cursor.lastrowid


def list_job_offers(conn: sqlite3.Connection, job_key: str | None = None) -> list[sqlite3.Row]:
    """All offers, or just those for one job. UC-7 (offer comparison) calls
    this once per job in `offered` status and renders them side by side."""
    if job_key:
        return list(
            conn.execute(
                "SELECT * FROM job_offers WHERE job_key = ? ORDER BY received_at ASC", (job_key,)
            )
        )
    return list(conn.execute("SELECT * FROM job_offers ORDER BY received_at ASC"))


# --- UC-2: "multiple recruiters, same job" dedupe --------------------------
# Proposed default from docs/JOB_CRM_VISION.md §6: fuzzy-match normalized
# (company, title) using the same SequenceMatcher approach
# contacts/store.py already uses for organization-name dedup
# (organizations_match_for_dedup, ratio >= 0.92 = confident match).

AUTO_MATCH_THRESHOLD = 0.92
AMBIGUOUS_THRESHOLD = 0.75


@dataclass
class JobMatch:
    normalized_key: str
    company: str
    title: str
    company_ratio: float
    title_ratio: float
    combined_score: float


def _ratio(a: str, b: str) -> float:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def find_similar_jobs(conn: sqlite3.Connection, company: str, title: str) -> list[JobMatch]:
    """Every existing job_leads row that's at least AMBIGUOUS_THRESHOLD
    similar on both company and title, sorted best match first. An exact
    normalized_key match (identical after normalize_key's punctuation/case
    stripping) is always included first with a perfect score."""
    exact_key = normalize_key(company, title)
    matches: list[JobMatch] = []
    for row in conn.execute("SELECT normalized_key, company, title FROM job_leads"):
        if row["normalized_key"] == exact_key:
            matches.append(JobMatch(row["normalized_key"], row["company"], row["title"], 1.0, 1.0, 1.0))
            continue
        company_ratio = _ratio(company, row["company"])
        title_ratio = _ratio(title, row["title"])
        combined = min(company_ratio, title_ratio)
        if combined >= AMBIGUOUS_THRESHOLD:
            matches.append(JobMatch(row["normalized_key"], row["company"], row["title"], company_ratio, title_ratio, combined))
    matches.sort(key=lambda m: m.combined_score, reverse=True)
    return matches


def find_matching_job(conn: sqlite3.Connection, company: str, title: str) -> JobMatch | None:
    """The single best match, only if it clears AUTO_MATCH_THRESHOLD — safe
    to auto-merge a new contact/conversation onto. Callers should use
    `find_similar_jobs` directly if they want to surface AMBIGUOUS_THRESHOLD
    candidates for manual confirmation instead of ignoring them (see
    JOB_CRM_VISION.md UC-2)."""
    candidates = find_similar_jobs(conn, company, title)
    if candidates and candidates[0].combined_score >= AUTO_MATCH_THRESHOLD:
        return candidates[0]
    return None


def find_company_only_matches(conn: sqlite3.Connection, company: str) -> list[JobMatch]:
    """Every job_leads row whose company fuzzy-matches `company` at
    AUTO_MATCH_THRESHOLD, ignoring title entirely — the fallback for
    `pipeline/comms_match.py`'s Tier 2 when a reply names an employer
    (e.g. "GE health care") but no title (a bare `find_similar_jobs` call
    would score title_ratio against an empty string and never clear
    AMBIGUOUS_THRESHOLD, even for the right company). Callers should only
    auto-attach on exactly one match; more than one is genuinely ambiguous
    (which of this company's tracked roles is this about?) and belongs in
    the unmatched queue for a human to pick."""
    matches: list[JobMatch] = []
    for row in conn.execute("SELECT normalized_key, company, title FROM job_leads"):
        company_ratio = _ratio(company, row["company"])
        if company_ratio >= AUTO_MATCH_THRESHOLD:
            matches.append(JobMatch(row["normalized_key"], row["company"], row["title"], company_ratio, 0.0, company_ratio))
    matches.sort(key=lambda m: m.combined_score, reverse=True)
    return matches


def get_lead_labeling_info(conn: sqlite3.Connection, keys: list[str]) -> dict[str, sqlite3.Row]:
    """`{normalized_key: row}` for every key in `keys` that still exists,
    with just the columns `cli/resync_labels.py` needs (`llm_verdict`,
    `verdict`, `status`) to re-derive a message's CURRENT outcome. A lead
    can vanish between initial triage and a later resync (e.g. `delete_lead.py`)
    — silently dropped from the result rather than raising, so one deleted
    sibling in a multi-role digest doesn't block resyncing the rest."""
    if not keys:
        return {}
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT normalized_key, llm_verdict, verdict, status FROM job_leads "
        f"WHERE normalized_key IN ({placeholders})",
        keys,
    ).fetchall()
    return {row["normalized_key"]: row for row in rows}


# --- Communications archival (pipeline/comms_match.py, 2026-07-17) --------
# Tier-1 matching: once any message on a thread, or from a known contact's
# address, has been linked to a job once, every later message on that same
# thread/address attaches for free — no fuzzy-matching or LLM call needed.

# LinkedIn's own relay addresses — every InMail/message-reply notification
# comes FROM one of these regardless of which actual recruiter sent it.
# Found live 2026-07-17: an earlier job_contacts row had one of these on
# file as "the contact," which made comms_match's Tier 2 spuriously match
# EVERY unrelated LinkedIn message onto that one job. Never store one of
# these as a job_contacts.email (scan_communications.py,
# resolve_unmatched_message below), and never let a match against one count
# as a real contact-identity match (comms_match.match_message_to_job).
GENERIC_RELAY_ADDRESSES = frozenset(
    {
        "hit-reply@linkedin.com",
        "inmail-hit-reply@linkedin.com",
        "messaging-digest-noreply@linkedin.com",
    }
)


def find_job_by_thread_id(conn: sqlite3.Connection, thread_id: str) -> str | None:
    """The job_key of the most recent job_conversations row already using
    this thread_id, if any. Empty/blank thread_id never matches (some
    senders omit it) — deliberately not treated as a wildcard."""
    if not thread_id:
        return None
    row = conn.execute(
        "SELECT job_key FROM job_conversations WHERE thread_id = ? ORDER BY occurred_at DESC LIMIT 1",
        (thread_id,),
    ).fetchone()
    return row["job_key"] if row else None


def find_job_by_contact_email(conn: sqlite3.Connection, email: str) -> str | None:
    """The job_key of a job_contacts row already on file for this email
    address, across ALL jobs (unlike add_job_contact's dedupe check, which
    is scoped to one job_key). If the same address is a contact on more
    than one job (e.g. a recruiter who's pitched you twice for different
    roles), the most recently contacted one wins — better than refusing to
    guess, since Tier 2/3 would have even less to go on."""
    email = (email or "").strip().lower()
    if not email:
        return None
    row = conn.execute(
        "SELECT job_key FROM job_contacts WHERE lower(email) = ? ORDER BY last_contacted_at DESC LIMIT 1",
        (email,),
    ).fetchone()
    return row["job_key"] if row else None


def is_communication_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    """True if `message_id` has already been handled by any path that
    touches communications: the recruiter-inbox triage flow
    (processed_messages), a resolved-or-unmatched conversation
    (job_conversations.message_id), or a prior comms scan that already
    parked it (unmatched_messages). scripts/scan_communications.py checks
    this before matching so a re-run never double-logs the same message."""
    for table, column in (
        ("processed_messages", "message_id"),
        ("job_conversations", "message_id"),
        ("unmatched_messages", "message_id"),
    ):
        row = conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (message_id,)).fetchone()
        if row is not None:
            return True
    return False


def record_unmatched_message(conn: sqlite3.Connection, msg: UnmatchedMessage) -> None:
    """Park a communication that couldn't be matched to any job. Idempotent
    on message_id (INSERT OR IGNORE) — a re-scan that finds the same
    unresolved message again is a no-op rather than an error."""
    conn.execute(
        """
        INSERT OR IGNORE INTO unmatched_messages (
            message_id, thread_id, direction, from_address, to_address,
            subject, body_text, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            msg.message_id,
            msg.thread_id,
            msg.direction,
            msg.from_address,
            msg.to_address,
            msg.subject,
            msg.body_text,
            msg.detected_at,
        ),
    )
    conn.commit()


def get_unmatched_message(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM unmatched_messages WHERE message_id = ?", (message_id,)
    ).fetchone()


def list_unmatched_messages(conn: sqlite3.Connection, *, include_resolved: bool = False) -> list[sqlite3.Row]:
    where = "" if include_resolved else "WHERE resolved_at IS NULL"
    return list(
        conn.execute(f"SELECT * FROM unmatched_messages {where} ORDER BY detected_at DESC")
    )


def resolve_unmatched_message(
    conn: sqlite3.Connection,
    message_id: str,
    job_key: str,
    *,
    contact_name: str = "",
    contact_email: str = "",
    contact_phone: str = "",
    contact_role: str = "recruiter",
    when: str | None = None,
) -> int:
    """Turn a parked `unmatched_messages` row into a real JobContact +
    JobConversation on `job_key` (scripts/resolve_communication.py's write
    side), then stamp resolved_job_key/resolved_at on the original row —
    kept, not deleted, as an audit trail of what was once unmatched and how
    it got resolved. Returns the new JobConversation's id.

    Raises ValueError if `message_id` isn't a known unmatched message (call
    `get_unmatched_message` first if you need to distinguish "already
    resolved" from "never existed")."""
    row = get_unmatched_message(conn, message_id)
    if row is None:
        raise ValueError(f"no unmatched_messages row for message_id={message_id!r}")

    fallback_address = row["from_address"] if row["direction"] == "inbound" else ""
    if fallback_address and fallback_address.strip().lower() in GENERIC_RELAY_ADDRESSES:
        fallback_address = ""
    contact_id = None
    if contact_name or contact_email or contact_phone or fallback_address:
        contact_id = add_job_contact(
            conn,
            JobContact(
                job_key=job_key,
                name=contact_name,
                email=contact_email or fallback_address,
                phone=contact_phone,
                role=contact_role,
                source_message_id=message_id,
            ),
        )

    conversation_id = add_job_conversation(
        conn,
        JobConversation(
            job_key=job_key,
            contact_id=contact_id,
            message_id=message_id,
            channel="email",
            direction=row["direction"],
            summary=row["subject"] or "(resolved communication)",
            thread_id=row["thread_id"] or "",
            body_text=row["body_text"] or "",
        ),
    )

    conn.execute(
        "UPDATE unmatched_messages SET resolved_job_key = ?, resolved_at = ? WHERE message_id = ?",
        (job_key, when or utc_now_iso(), message_id),
    )
    conn.commit()
    return conversation_id


# --- Rejection cooldown / disqualification --------------------------------
# A company that just rejected someone for a specific role re-posting (or a
# different recruiter re-surfacing) that exact same role within a short
# window is, in practice, not worth re-spending an LLM evaluation (or a
# human's attention) on — pipeline/triage.py checks this before running the
# two-tier review pipeline for a role and short-circuits straight to a
# "pass" verdict when it fires.
DEFAULT_REJECTION_COOLDOWN_DAYS = 90


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def find_recent_rejection(
    conn: sqlite3.Connection,
    company: str,
    title: str,
    *,
    within_days: int = DEFAULT_REJECTION_COOLDOWN_DAYS,
    now: str | None = None,
) -> sqlite3.Row | None:
    """The most recent `status = 'rejected'` job_leads row fuzzy-matching
    (company, title) whose `rejected_at` falls within the last `within_days`
    days, or None if there isn't one.

    Uses the same AUTO_MATCH_THRESHOLD bar as find_matching_job() (an exact
    normalized_key always wins outright; otherwise both company and title
    similarity must clear 0.92) — deliberately strict, since a false
    positive here silently disqualifies what might be a genuinely new,
    separate opening at the same company rather than a re-post/re-outreach
    for the one that was already declined.
    """
    now_dt = _parse_iso(now) or datetime.now(timezone.utc)
    cutoff = now_dt - timedelta(days=within_days)
    exact_key = normalize_key(company, title)

    best: tuple[float, sqlite3.Row] | None = None
    for row in conn.execute(
        "SELECT * FROM job_leads WHERE status = 'rejected' AND rejected_at IS NOT NULL"
    ):
        rejected_at = _parse_iso(row["rejected_at"])
        if rejected_at is None or rejected_at < cutoff:
            continue
        if row["normalized_key"] == exact_key:
            return row
        combined = min(_ratio(company, row["company"]), _ratio(title, row["title"]))
        if combined >= AUTO_MATCH_THRESHOLD and (best is None or combined > best[0]):
            best = (combined, row)
    return best[1] if best else None


def update_llm_evaluation(conn: sqlite3.Connection, normalized_key: str, evaluation) -> None:
    """Persist an llm_apply.EvaluationResult onto a stored lead — including the
    full JD-review data (job_summary/dealbreaker_checks/skills_alignment/flags/
    framing_guidance), not just verdict+rationale, so `render_jd_review()` can
    be reconstructed later purely from the DB (see list_leads.py --show-review)."""
    metrics = evaluation.metrics
    conn.execute(
        """
        UPDATE job_leads
        SET llm_verdict = ?,
            llm_match_pct = ?,
            llm_job_summary = ?,
            llm_dealbreaker_notes = ?,
            llm_skills_alignment = ?,
            llm_flags = ?,
            llm_rationale = ?,
            llm_framing_guidance = ?,
            llm_structural_verdict = ?,
            llm_next_step = ?,
            llm_cover_letter_strategy = ?,
            llm_interview_prep = ?,
            llm_eval_input_tokens = ?,
            llm_eval_output_tokens = ?,
            llm_eval_cost_usd = ?,
            llm_evaluated_at = ?
        WHERE normalized_key = ?
        """,
        (
            evaluation.verdict,
            evaluation.match_pct,
            evaluation.job_summary,
            json.dumps(evaluation.dealbreaker_checks),
            json.dumps(evaluation.skills_alignment),
            json.dumps(evaluation.flags),
            evaluation.rationale,
            json.dumps(evaluation.framing_guidance),
            evaluation.structural_verdict,
            evaluation.next_step,
            evaluation.cover_letter_strategy,
            json.dumps(evaluation.interview_prep),
            metrics.input_tokens if metrics else None,
            metrics.output_tokens if metrics else None,
            metrics.cost_usd if metrics else None,
            utc_now_iso(),
            normalized_key,
        ),
    )
    conn.commit()
