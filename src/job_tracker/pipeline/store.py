"""SQLite-backed dedup store for discovered job leads.

One row per unique (company, title) normalized key. Re-seeing the same role
(e.g. a digest re-sends it, or it appears via two different senders) updates
last_seen instead of creating a duplicate row.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from job_tracker.pipeline.models import JobLead, utc_now_iso

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
-- message id. This is the message-level outcome (ACCEPT/DENY/NEEDS_REVIEW)
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
    # Lifecycle timeline (models.LEAD_STAGES) — one nullable timestamp column
    # per stage after "new", stamped by advance_status() below whenever a
    # lead's status moves forward. Lets a lead's history stay visible (e.g.
    # "applied 2026-06-01, interviewing 2026-06-15") instead of only ever
    # showing whatever stage it's currently in.
    ("approved_at", "ALTER TABLE job_leads ADD COLUMN approved_at TEXT"),
    ("package_generated_at", "ALTER TABLE job_leads ADD COLUMN package_generated_at TEXT"),
    ("applied_at", "ALTER TABLE job_leads ADD COLUMN applied_at TEXT"),
    ("following_up_at", "ALTER TABLE job_leads ADD COLUMN following_up_at TEXT"),
    ("interviewing_at", "ALTER TABLE job_leads ADD COLUMN interviewing_at TEXT"),
    ("offered_at", "ALTER TABLE job_leads ADD COLUMN offered_at TEXT"),
    ("accepted_at", "ALTER TABLE job_leads ADD COLUMN accepted_at TEXT"),
    ("started_at", "ALTER TABLE job_leads ADD COLUMN started_at TEXT"),
    ("passed_at", "ALTER TABLE job_leads ADD COLUMN passed_at TEXT"),
]

# models.LEAD_STAGES -> the timestamp column stamped when a lead enters that
# stage. "new" has none (first_seen already covers it).
_STAGE_DATE_COLUMNS: dict[str, str] = {
    "approved": "approved_at",
    "package_generated": "package_generated_at",
    "applied": "applied_at",
    "following_up": "following_up_at",
    "interviewing": "interviewing_at",
    "offered": "offered_at",
    "accepted": "accepted_at",
    "started": "started_at",
    "passed": "passed_at",
}


def _apply_migrations(conn: sqlite3.Connection) -> None:
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

    # Preserve any manual status the user already set (e.g. "approved"),
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
    already-approved lead), so the timeline records the *first* time a lead
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


def update_llm_evaluation(conn: sqlite3.Connection, normalized_key: str, evaluation) -> None:
    """Persist an llm_apply.EvaluationResult onto a stored lead."""
    metrics = evaluation.metrics
    conn.execute(
        """
        UPDATE job_leads
        SET llm_verdict = ?,
            llm_match_pct = ?,
            llm_dealbreaker_notes = ?,
            llm_skills_alignment = ?,
            llm_rationale = ?,
            llm_eval_input_tokens = ?,
            llm_eval_output_tokens = ?,
            llm_eval_cost_usd = ?,
            llm_evaluated_at = ?
        WHERE normalized_key = ?
        """,
        (
            evaluation.verdict,
            evaluation.match_pct,
            json.dumps(evaluation.dealbreaker_notes),
            json.dumps(evaluation.skills_alignment),
            evaluation.rationale,
            metrics.input_tokens if metrics else None,
            metrics.output_tokens if metrics else None,
            metrics.cost_usd if metrics else None,
            utc_now_iso(),
            normalized_key,
        ),
    )
    conn.commit()
