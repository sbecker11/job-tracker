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
"""

# Columns added after the initial release. New databases get them via
# _SCHEMA above; this backfills any pre-existing var/leads.db in place so
# upgrading never requires deleting stored leads.
_MIGRATIONS: list[tuple[str, str]] = [
    ("jd_text", "ALTER TABLE job_leads ADD COLUMN jd_text TEXT"),
]


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
    conn.execute(_SCHEMA)
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

    # Preserve any manual status the user already set (e.g. "pursuing"),
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


def list_leads(conn: sqlite3.Connection, *, verdict: str | None = None) -> list[sqlite3.Row]:
    if verdict:
        return list(
            conn.execute(
                "SELECT * FROM job_leads WHERE verdict = ? ORDER BY match_pct DESC, last_seen DESC",
                (verdict,),
            )
        )
    return list(conn.execute("SELECT * FROM job_leads ORDER BY match_pct DESC, last_seen DESC"))
