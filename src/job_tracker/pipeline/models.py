"""Pipeline-level record types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# The "magic best-case path" a lead travels through, end to end. `status`
# (job_leads.status) holds one of these; each stage after "new" has a
# matching `<stage>_at` timestamp column (see pipeline/store.py's
# _STAGE_DATE_COLUMNS) so the DB keeps a timeline, not just a current state.
# "skipped" is the off-ramp — a lead we (or the LLM verdict) decided not to
# pursue; it can happen at "new" or "pursued" and is terminal either way.
# Renamed 2026-07-07 (were "approved"/"passed") to match the Gmail outcome
# labels (gmail_writer.PURSUE_LABEL/SKIP_LABEL) and triage.py's PURSUE/SKIP
# constants — see store.py's migration that renamed the DB columns/values.
LEAD_STAGES: tuple[str, ...] = (
    "new",  # unprocessed
    "pursued",  # triage decided PURSUE — worth pursuing, package not made yet
    "package_generated",  # résumé + cover letter rendered
    "applied",  # application submitted
    "following_up",  # followed up with a stakeholder (recruiter, hiring manager, etc.)
    "interviewing",  # in an active interview loop
    "offered",  # an offer is on the table
    "accepted",  # offer accepted
    "started",  # start date reached
    "skipped",  # off-ramp: WE decided not to pursue, can happen at any point
    # off-ramp: THEY declined us (a rejection email was confirmed against
    # this lead — see pipeline/store.py's rejected_at/rejection_* columns
    # and find_recent_rejection()). Deliberately distinct from "skipped"
    # (that's our own pass decision) so the two are never conflated — a
    # rejected lead's rejected_at is also what
    # pipeline.store.find_recent_rejection() checks to auto-disqualify a
    # resurfacing posting for the same role within its cooldown window.
    "rejected",
)


def normalize_key(company: str, title: str) -> str:
    def clean(s: str) -> str:
        return "".join(ch for ch in s.lower() if ch.isalnum() or ch.isspace()).split()

    return " ".join(clean(company)) + "::" + " ".join(clean(title))


@dataclass
class JobLead:
    company: str
    title: str
    source_message_id: str
    source_label: str
    apply_url: str = ""
    extraction_confidence: float = 0.0
    jd_resolved: bool = False
    jd_source: str = ""  # "ats_api" | "digest_snippet" | "email_body" | ""
    jd_text: str = ""  # full JD body, whatever its source — kept for reference
    match_pct: float = 0.0
    matched_skills: list[str] = field(default_factory=list)
    verdict: str = "review"
    rationale: list[str] = field(default_factory=list)
    status: str = "new"  # one of LEAD_STAGES above
    first_seen: str = field(default_factory=utc_now_iso)
    last_seen: str = field(default_factory=utc_now_iso)

    @property
    def normalized_key(self) -> str:
        return normalize_key(self.company, self.title)


# --- Job CRM entities (docs/JOB_CRM_VISION.md) -----------------------------
# These all hang off a JobLead's normalized_key ("job_key" below). None of
# them replace JobLead — it remains the Job identity row; these are the
# join tables that answer "who's involved, what was said, what documents
# exist, what's scheduled, what was offered" for a given job.


@dataclass
class JobContact:
    """A person involved in a job (recruiter, hiring manager, referral).

    `contact_ref` points at a comms-migration `contacts/Contacts.yaml` id
    when a match was found there by email (read-only linkage — job-tracker
    never writes back); empty when the sender isn't in that address book.
    """

    job_key: str
    name: str = ""
    email: str = ""
    phone: str = ""
    role: str = "recruiter"  # recruiter | hiring_manager | referral | other
    contact_ref: str = ""
    source_message_id: str = ""
    id: int | None = None
    first_contacted_at: str = field(default_factory=utc_now_iso)
    last_contacted_at: str = field(default_factory=utc_now_iso)


@dataclass
class JobConversation:
    """One logged interaction (email, call, ...) tied to a job and contact."""

    job_key: str
    contact_id: int | None = None
    message_id: str = ""
    channel: str = "email"  # email | call | other
    direction: str = "inbound"  # inbound | outbound
    summary: str = ""
    id: int | None = None
    occurred_at: str = field(default_factory=utc_now_iso)


@dataclass
class JobDocument:
    """A versioned artifact tied to a job: JD snapshot, résumé, cover letter,
    RTR, availability sent, or anything else worth keeping."""

    job_key: str
    doc_type: str  # jd_snapshot | resume | cover_letter | rtr | availability | other
    path_or_url: str = ""
    version: int = 1
    id: int | None = None
    created_at: str = field(default_factory=utc_now_iso)


@dataclass
class JobMeeting:
    """A scheduled or completed interview/call tied to a job."""

    job_key: str
    contact_id: int | None = None
    kind: str = "other"  # phone_screen | onsite | technical | other
    status: str = "proposed"  # proposed | confirmed | completed | cancelled
    notes: str = ""
    id: int | None = None
    scheduled_at: str = ""


@dataclass
class JobOffer:
    """A final offer on a job, for side-by-side comparison (UC-7)."""

    job_key: str
    base_salary: float = 0.0
    bonus: float = 0.0
    equity: str = ""
    benefits_notes: str = ""
    deadline: str = ""
    decision: str = "pending"  # pending | accepted | declined
    id: int | None = None
    received_at: str = field(default_factory=utc_now_iso)
