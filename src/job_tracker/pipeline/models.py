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
# "passed" is the off-ramp — a lead we (or the LLM verdict) decided not to
# pursue; it can happen at "new" or "approved" and is terminal either way.
LEAD_STAGES: tuple[str, ...] = (
    "new",  # unprocessed
    "approved",  # triage decided ACCEPT — worth pursuing, package not made yet
    "package_generated",  # résumé + cover letter rendered
    "applied",  # application submitted
    "following_up",  # followed up with a stakeholder (recruiter, hiring manager, etc.)
    "interviewing",  # in an active interview loop
    "offered",  # an offer is on the table
    "accepted",  # offer accepted
    "started",  # start date reached
    "passed",  # off-ramp: decided not to pursue (or rejected), can happen at any point
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
    jd_source: str = ""  # "ats_api" | "email_body" | ""
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
