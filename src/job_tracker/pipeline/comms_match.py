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

MatchTier = str  # "thread_id" | "contact_email" | "llm_company_title" | "llm_company_only" | "llm_new_lead" | "unmatched"


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
        """True only for tier 4 above: a full (company, title) pair was
        extracted with enough confidence to act on, but it's genuinely new
        — not a case of "couldn't tell", which stays parked instead."""
        return self.tier == "llm_new_lead" and self.extracted_role is not None


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
        return MatchOutcome(job_key, "thread_id", f"thread_id {message.thread_id!r} already linked to this job")

    other_address = message.from_address if direction == "inbound" else message.to_address
    if other_address.strip().lower() not in store.GENERIC_RELAY_ADDRESSES:
        job_key = store.find_job_by_contact_email(conn, other_address)
        if job_key:
            return MatchOutcome(job_key, "contact_email", f"{other_address!r} already on file as a contact")

    if not use_llm_fallback:
        return MatchOutcome(None, "unmatched", "no thread/contact match; LLM fallback not requested")

    roles = extract_roles_llm_cached(conn, message, model=llm_model)
    if not roles:
        return MatchOutcome(None, "unmatched", "no thread/contact match; LLM found no company/title in body")

    role = max(roles, key=lambda r: r.confidence)

    if role.company and role.title:
        match = store.find_matching_job(conn, role.company, role.title)
        if match:
            return MatchOutcome(
                match.normalized_key,
                "llm_company_title",
                f"LLM extracted {role.title!r} @ {role.company!r}, matched an existing job",
                extracted_role=role,
            )
        # Both company and title came through clean, just not for anything
        # already tracked — a real new lead, not a "couldn't tell" case. Not
        # resolved here (this module never writes to job_leads); the caller
        # decides whether to act on `is_new_lead_candidate`.
        return MatchOutcome(
            None,
            "llm_new_lead",
            f"LLM extracted {role.title!r} @ {role.company!r}; no existing job for it — eligible for a new stub lead",
            extracted_role=role,
        )

    if role.company:
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

    return MatchOutcome(
        None,
        "unmatched",
        "no thread/contact match; LLM extraction didn't resolve to a known job",
        extracted_role=role,
    )
