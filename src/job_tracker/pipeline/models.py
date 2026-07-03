"""Pipeline-level record types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    match_pct: float = 0.0
    matched_skills: list[str] = field(default_factory=list)
    verdict: str = "review"
    rationale: list[str] = field(default_factory=list)
    status: str = "new"  # "new" | "pursuing" | "passed" | "applied"
    first_seen: str = field(default_factory=utc_now_iso)
    last_seen: str = field(default_factory=utc_now_iso)

    @property
    def normalized_key(self) -> str:
        return normalize_key(self.company, self.title)
