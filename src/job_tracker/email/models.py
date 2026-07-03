"""Structured email and classification types."""

from __future__ import annotations

from dataclasses import dataclass, field

from job_tracker.email.labels import Label


@dataclass
class ExtractedRole:
    """One (company, title) pair fanned out of a SINGLE_JD or MULTI_JD_IN_BODY message."""

    company: str = ""
    title: str = ""
    apply_url: str = ""
    source: str = ""  # "ats_url" | "bullet_line" | "subject" | "sender_domain"
    confidence: float = 0.0


@dataclass
class EmailMessage:
    """Normalized recruiting-inbox message (Gmail reader fills this in 2a)."""

    id: str
    from_address: str
    subject: str
    body_plain: str = ""
    body_html: str = ""
    snippet: str = ""
    thread_id: str = ""
    date: str = ""
    to_address: str = ""
    label_ids: list[str] = field(default_factory=list)

    @property
    def combined_text(self) -> str:
        """Subject + bodies for heuristic matching."""
        parts = [self.subject, self.snippet, self.body_plain]
        if not self.body_plain.strip() and self.body_html.strip():
            parts.append(self.body_html)
        return "\n".join(p for p in parts if p)


@dataclass
class ClassificationResult:
    label: Label
    confidence: float
    reasons: list[str] = field(default_factory=list)
    extracted_roles: list[ExtractedRole] = field(default_factory=list)
