"""SQLite-backed dedup store for discovered job leads.

One row per unique (company, title) normalized key. Re-seeing the same role
(e.g. a digest re-sends it, or it appears via two different senders) updates
last_seen instead of creating a duplicate row.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from job_tracker.pipeline.models import (
    JobContact,
    JobConversation,
    JobDocument,
    JobLead,
    JobMeeting,
    JobOffer,
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
    times_seen INTEGER DEFAULT 1
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
"""

# Columns added after the initial release. New databases get them via
# _SCHEMA above; this backfills any pre-existing var/leads.db in place so
# upgrading never requires deleting stored leads.
_MIGRATIONS: list[tuple[str, str]] = [
    ("jd_text", "ALTER TABLE job_leads ADD COLUMN jd_text TEXT"),
    # LLM-based evaluation (pipeline/llm_apply.py, CLAUDE.md's JD Match
    # Framework) — kept separate from the original keyword-scorer columns
    # above (match_pct/verdict/rationale) since the two disagree often enough
    # to matter (the keyword scorer produced false "pass"es the LLM verdict
    # caught as genuine "pursue"s in testing) and both are worth keeping for
    # comparison rather than overwriting one with the other.
    ("llm_verdict", "ALTER TABLE job_leads ADD COLUMN llm_verdict TEXT"),
    ("llm_match_pct", "ALTER TABLE job_leads ADD COLUMN llm_match_pct REAL"),
    ("llm_dealbreaker_notes", "ALTER TABLE job_leads ADD COLUMN llm_dealbreaker_notes TEXT"),
    ("llm_skills_alignment", "ALTER TABLE job_leads ADD COLUMN llm_skills_alignment TEXT"),
    ("llm_rationale", "ALTER TABLE job_leads ADD COLUMN llm_rationale TEXT"),
    ("llm_eval_input_tokens", "ALTER TABLE job_leads ADD COLUMN llm_eval_input_tokens INTEGER"),
    ("llm_eval_output_tokens", "ALTER TABLE job_leads ADD COLUMN llm_eval_output_tokens INTEGER"),
    ("llm_eval_cost_usd", "ALTER TABLE job_leads ADD COLUMN llm_eval_cost_usd REAL"),
    ("llm_evaluated_at", "ALTER TABLE job_leads ADD COLUMN llm_evaluated_at TEXT"),
    # Richer JD-review fields (2026-07-07) — job_summary/flags/framing_guidance
    # are new; llm_dealbreaker_notes/llm_skills_alignment above now hold JSON
    # lists of dicts (check/status/notes and requirement/evidence/strength)
    # instead of flat strings, but keep their original column names since
    # they're still "the dealbreaker sweep" and "the skills alignment table"
    # per CLAUDE.md §10 — just structured instead of prose now.
    ("llm_job_summary", "ALTER TABLE job_leads ADD COLUMN llm_job_summary TEXT"),
    ("llm_flags", "ALTER TABLE job_leads ADD COLUMN llm_flags TEXT"),
    ("llm_framing_guidance", "ALTER TABLE job_leads ADD COLUMN llm_framing_guidance TEXT"),
    # CLAUDE.md §10 steps 4-7 (2026-07-11) — structural_verdict/next_step
    # split "does this look good on paper" from the final dealbreaker-aware
    # verdict and surface a concrete escape-hatch action when a dealbreaker
    # is soft/confirmable; cover_letter_strategy/interview_prep synthesize
    # framing_guidance into a narrative paragraph and interview talking
    # points, respectively — see llm_apply.py's _EVAL_SYSTEM_PROMPT.
    ("llm_structural_verdict", "ALTER TABLE job_leads ADD COLUMN llm_structural_verdict TEXT"),
    ("llm_next_step", "ALTER TABLE job_leads ADD COLUMN llm_next_step TEXT"),
    ("llm_cover_letter_strategy", "ALTER TABLE job_leads ADD COLUMN llm_cover_letter_strategy TEXT"),
    ("llm_interview_prep", "ALTER TABLE job_leads ADD COLUMN llm_interview_prep TEXT"),
    # Lifecycle timeline (models.LEAD_STAGES) — one nullable timestamp column
    # per stage after "new", stamped by advance_status() below whenever a
    # lead's status moves forward. Lets a lead's history stay visible (e.g.
    # "applied 2026-06-01, interviewing 2026-06-15") instead of only ever
    # showing whatever stage it's currently in.
    ("pursued_at", "ALTER TABLE job_leads ADD COLUMN pursued_at TEXT"),
    ("package_generated_at", "ALTER TABLE job_leads ADD COLUMN package_generated_at TEXT"),
    ("applied_at", "ALTER TABLE job_leads ADD COLUMN applied_at TEXT"),
    ("following_up_at", "ALTER TABLE job_leads ADD COLUMN following_up_at TEXT"),
    ("interviewing_at", "ALTER TABLE job_leads ADD COLUMN interviewing_at TEXT"),
    ("offered_at", "ALTER TABLE job_leads ADD COLUMN offered_at TEXT"),
    ("accepted_at", "ALTER TABLE job_leads ADD COLUMN accepted_at TEXT"),
    ("started_at", "ALTER TABLE job_leads ADD COLUMN started_at TEXT"),
    ("skipped_at", "ALTER TABLE job_leads ADD COLUMN skipped_at TEXT"),
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
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(job_leads)")}
    for column, ddl in _MIGRATIONS:
        if column not in existing:
            conn.execute(ddl)
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


def upsert_lead(conn: sqlite3.Connection, lead: JobLead) -> bool:
    """Insert a new lead or refresh an existing one. Returns True if new."""
    key = lead.normalized_key
    existing = conn.execute(
        "SELECT normalized_key, status FROM job_leads WHERE normalized_key = ?", (key,)
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO job_leads (
                normalized_key, company, title, source_message_id, source_label,
                apply_url, extraction_confidence, jd_resolved, jd_source, jd_text,
                match_pct, matched_skills, verdict, rationale, status,
                first_seen, last_seen, times_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
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


def list_leads(conn: sqlite3.Connection, *, verdict: str | None = None) -> list[sqlite3.Row]:
    if verdict:
        return list(
            conn.execute(
                "SELECT * FROM job_leads WHERE verdict = ? ORDER BY match_pct DESC, last_seen DESC",
                (verdict,),
            )
        )
    return list(conn.execute("SELECT * FROM job_leads ORDER BY match_pct DESC, last_seen DESC"))


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
    """Insert a JobContact, or — if `email` already exists for this job_key —
    just bump `last_contacted_at` and return the existing row's id. This is
    what makes UC-1 (ingest) and UC-2 (dedupe) safe to call repeatedly for
    the same sender without piling up duplicate contact rows."""
    email = (contact.email or "").strip().lower()
    if email:
        existing = conn.execute(
            "SELECT id FROM job_contacts WHERE job_key = ? AND lower(email) = ?",
            (contact.job_key, email),
        ).fetchone()
        if existing is not None:
            conn.execute(
                "UPDATE job_contacts SET last_contacted_at = ? WHERE id = ?",
                (utc_now_iso(), existing["id"]),
            )
            conn.commit()
            return existing["id"]

    cursor = conn.execute(
        """
        INSERT INTO job_contacts (
            job_key, contact_ref, name, email, role, source_message_id,
            first_contacted_at, last_contacted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact.job_key,
            contact.contact_ref,
            contact.name,
            contact.email,
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


def add_job_conversation(conn: sqlite3.Connection, conversation: JobConversation) -> int:
    cursor = conn.execute(
        """
        INSERT INTO job_conversations (
            job_key, contact_id, message_id, channel, direction, summary, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            conversation.job_key,
            conversation.contact_id,
            conversation.message_id,
            conversation.channel,
            conversation.direction,
            conversation.summary,
            conversation.occurred_at,
        ),
    )
    conn.commit()
    return cursor.lastrowid


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
