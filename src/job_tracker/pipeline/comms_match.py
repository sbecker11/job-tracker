"""Tiered matching: attach one inbound/outbound communication to a tracked
job, or park it in the unmatched queue for manual review.

Added 2026-07-17 after 3 real recruiter replies (2 from a known contact, 1
from a brand-new one) were found sitting completely untracked — see
scripts/scan_communications.py's docstring and chat history for the
incident. Tiers, cheapest/most-certain first:

1. Thread id already linked to a job (`store.find_job_by_thread_id`) — free,
   and the dominant path once a thread has been linked once. Reinforced by
   the candidate's own workflow change: always reply within the existing
   Gmail/LinkedIn thread, and always name the company + title in outgoing
   cold-outreach bodies (see CLAUDE.md-adjacent chat history, 2026-07-17).
1b. LinkedIn conversation link (`linkedin.com/messaging/thread/<id>`) already
   on exactly one job (2026-10-14). Every LinkedIn notification is its own
   email thread and comes from a shared relay, so Tiers 1-2 can never link
   a follow-up InMail; the conversation link can.
2. Sender/recipient address already on file as a job_contacts.email — free.
3. (opt-in, costs money) LLM-extracted company/title from the body,
   fuzzy-matched against job_leads via the same machinery
   `triage_recruiter_inbox.py` uses for "multiple recruiters, same job."
   A company-only extraction (no title — e.g. "W2, GE health care, let's
   talk the rate") only auto-attaches if it fuzzy-matches EXACTLY ONE
   existing job; more than one is genuinely ambiguous and left unmatched.
4. Both a company AND a title were extracted, but neither matches any job
   already on file ("llm_new_lead" tier, added 2026-07-17 per the
   candidate's explicit instruction: "if the company and title can be
   extracted... add it as a new document for that company+title"). This
   module stays side-effect-free — it never writes to `job_leads` itself —
   so it just signals the caller (`scripts/scan_communications.py`) that a
   brand-new stub lead is warranted via `MatchOutcome.is_new_lead_candidate`.
5. Unmatched — genuinely nothing usable (no company/title at all, or a
   company-only extraction that's ambiguous against 2+ existing jobs).
   Parked in `unmatched_messages` for `scripts/resolve_communication.py`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from job_tracker.email.models import EmailMessage, ExtractedRole
from job_tracker.pipeline import store
from job_tracker.pipeline.llm_extract import DEFAULT_MODEL as DEFAULT_LLM_EXTRACT_MODEL
from job_tracker.pipeline.llm_extract import extract_roles_llm_cached
from job_tracker.pipeline.qualifying_reply import extract_role_heuristic

MatchTier = str  # "thread_id" | "linkedin_thread" | "contact_email" | "named_end_client" | "llm_company_title" | "llm_company_only" | "llm_new_lead" | "heuristic_company_title" | "heuristic_new_lead" | "unmatched"

# Recruiter/agency umbrella leads (PCC "end-client TBD", staffing firms).
# When a later message in that thread names a different existing company+title
# (the end client), file the communication on that job instead.
_AGENCY_LEAD_MARKERS = (
    "recruitment",
    "recruiting",
    "staffing",
    "end-client",
    "endclient",
    "end client",
)


@dataclass
class MatchOutcome:
    job_key: str | None
    tier: MatchTier
    reason: str = ""
    # The LLM's extraction, whenever Tier 3 actually ran (win, lose, or
    # ambiguous) — None for Tier 1/2 matches (no extraction call was made)
    # and for the "no roles at all" unmatched case. Lets the caller act on
    # what was found even when comms_match.py itself declined to resolve it
    # to an existing job.
    extracted_role: ExtractedRole | None = None

    @property
    def matched(self) -> bool:
        return self.job_key is not None

    @property
    def is_new_lead_candidate(self) -> bool:
        """True when a full (company, title) pair was extracted (LLM or free
        heuristic) but it's genuinely new — not a "couldn't tell" park."""
        return (
            self.tier in ("llm_new_lead", "heuristic_new_lead")
            and self.extracted_role is not None
        )


def outcome_from_role(conn: sqlite3.Connection, role: ExtractedRole, *, source: str) -> MatchOutcome | None:
    """Map a full/partial ExtractedRole onto match tiers. Returns None if the
    role has neither company nor title worth acting on."""
    if role.company and role.title:
        match = store.find_matching_job(conn, role.company, role.title)
        if match:
            return MatchOutcome(
                match.normalized_key,
                f"{source}_company_title",
                f"{source} extracted {role.title!r} @ {role.company!r}, matched an existing job",
                extracted_role=role,
            )
        return MatchOutcome(
            None,
            f"{source}_new_lead",
            f"{source} extracted {role.title!r} @ {role.company!r}; no existing job — eligible for a new stub lead",
            extracted_role=role,
        )
    if role.company and source == "llm":
        candidates = store.find_company_only_matches(conn, role.company)
        if len(candidates) == 1:
            return MatchOutcome(
                candidates[0].normalized_key,
                "llm_company_only",
                f"LLM extracted company {role.company!r} (no usable title); exactly one existing job for it",
                extracted_role=role,
            )
        if len(candidates) > 1:
            return MatchOutcome(
                None,
                "unmatched",
                f"LLM extracted company {role.company!r} but {len(candidates)} existing jobs match it — ambiguous",
                extracted_role=role,
            )
    return None


def _is_agency_or_tbd_lead(conn: sqlite3.Connection, job_key: str) -> bool:
    row = conn.execute(
        "SELECT company, title FROM job_leads WHERE normalized_key = ?", (job_key,)
    ).fetchone()
    if not row:
        return False
    blob = f"{row['company'] or ''} {row['title'] or ''}".lower()
    return any(marker in blob for marker in _AGENCY_LEAD_MARKERS)


def existing_jobs_named_in_text(conn: sqlite3.Connection, text: str) -> list[str]:
    """Keys of existing leads whose company AND title both appear in `text`.

    Used so a PCC/agency thread that later names "Metabody" + "Staff AI Engineer"
    files onto that end-client lead. Title must be reasonably specific (≥8 chars)
    so "Engineer" alone cannot hijack matching.
    """
    blob = (text or "").lower()
    if len(blob) < 8:
        return []
    keys: list[str] = []
    seen: set[str] = set()
    try:
        rows = conn.execute(
            "SELECT normalized_key, company, title FROM job_leads "
            "WHERE ifnull(deleted_at, '') = ''"
        )
    except sqlite3.OperationalError:
        rows = conn.execute("SELECT normalized_key, company, title FROM job_leads")
    for row in rows:
        company = (row["company"] or "").strip()
        title = (row["title"] or "").strip()
        if len(company) < 3 or len(title) < 8:
            continue
        if company.lower() in blob and title.lower() in blob:
            key = row["normalized_key"]
            if key not in seen:
                seen.add(key)
                keys.append(key)
    return keys


def _prefer_named_end_client(
    conn: sqlite3.Connection,
    job_key: str | None,
    message: EmailMessage,
    *,
    tier: MatchTier,
    reason: str,
) -> MatchOutcome:
    """If the body uniquely names a different existing job, and the thread/
    contact hit is an agency/TBD umbrella, file on the named end-client job."""
    named = existing_jobs_named_in_text(conn, message.combined_text or "")
    if len(named) == 1 and named[0] != job_key:
        if job_key is None or _is_agency_or_tbd_lead(conn, job_key):
            return MatchOutcome(
                named[0],
                "named_end_client",
                f"body names existing job {named[0]!r}; superseded {tier} match {job_key!r}",
            )
    if job_key:
        return MatchOutcome(job_key, tier, reason)
    return MatchOutcome(None, "unmatched", reason)


def match_message_to_job(
    conn: sqlite3.Connection,
    message: EmailMessage,
    *,
    direction: str = "inbound",
    use_llm_fallback: bool = False,
    llm_model: str = DEFAULT_LLM_EXTRACT_MODEL,
) -> MatchOutcome:
    """Try each tier in order; return the first that resolves to a job, or
    an "unmatched" outcome if none do. `direction` says which side of
    `message` is "the other party" whose address Tier 2 should check
    (inbound: `from_address`; outbound: `to_address`)."""
    job_key = store.find_job_by_thread_id(conn, message.thread_id)
    if job_key:
        return _prefer_named_end_client(
            conn,
            job_key,
            message,
            tier="thread_id",
            reason=f"thread_id {message.thread_id!r} already linked to this job",
        )

    li_thread = store.linkedin_thread_key(message.combined_text or "")
    job_key = store.find_job_by_linkedin_thread(conn, li_thread)
    if job_key:
        return _prefer_named_end_client(
            conn,
            job_key,
            message,
            tier="linkedin_thread",
            reason=f"LinkedIn conversation {li_thread[:24]}… already linked to this job",
        )

    other_address = message.from_address if direction == "inbound" else message.to_address
    if other_address.strip().lower() not in store.GENERIC_RELAY_ADDRESSES:
        job_key = store.find_job_by_contact_email(conn, other_address)
        if job_key:
            return _prefer_named_end_client(
                conn,
                job_key,
                message,
                tier="contact_email",
                reason=f"{other_address!r} already on file as a contact",
            )

    # Free heuristic before paid LLM (2026-08-04): structured InMails with
    # Position: + agency Inc line become stub leads without API spend.
    heuristic = extract_role_heuristic(message.combined_text or "", subject=message.subject or "")
    if heuristic is not None:
        outcome = outcome_from_role(conn, heuristic, source="heuristic")
        if outcome is not None:
            return outcome

    if not use_llm_fallback:
        return MatchOutcome(None, "unmatched", "no thread/contact/heuristic match; LLM fallback not requested")

    roles = extract_roles_llm_cached(conn, message, model=llm_model)
    if not roles:
        return MatchOutcome(None, "unmatched", "no thread/contact match; LLM found no company/title in body")

    role = max(roles, key=lambda r: r.confidence)
    outcome = outcome_from_role(conn, role, source="llm")
    if outcome is not None:
        return outcome

    return MatchOutcome(
        None,
        "unmatched",
        "no thread/contact match; LLM extraction didn't resolve to a known job",
        extracted_role=role,
    )
