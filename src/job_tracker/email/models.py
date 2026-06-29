"""Structured email and classification types."""

from __future__ import annotations

from dataclasses import dataclass, field

from job_tracker.email.labels import Label


@dataclass
class ExtractedRole:
    """Stub for fan-out (2c); populated in a later commit."""

    company: str = ""
    title: str = ""


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
