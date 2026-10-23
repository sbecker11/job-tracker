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
    # The specific slice of the source email's text that describes THIS role
    # — a listing chunk, bullet line, or (for a genuinely single-job message)
    # the whole body — as opposed to `message.combined_text`, which is the
    # ENTIRE email. Populated whenever an extraction path can isolate one
    # (added 2026-07-07 after a false dealbreaker hit on a Bugcrowd role fired
    # from a *different* job's "Angular" mention living elsewhere in the same
    # multi-job digest): `pipeline/triage.py` scores against this instead of
    # `message.combined_text` when it's non-empty, so scoring one role in a
    # digest never gets contaminated by a sibling listing's content. Left
    # empty when an extraction path can't isolate a per-role chunk (e.g. the
    # bare "one row per ATS link" fallback) — callers fall back to the full
    # message text in that case, same as before this field existed.
    snippet: str = ""


def normalize_rfc_id(value: str) -> str:
    """One Message-Id in "<local@domain>" form; empty when there is none."""
    value = (value or "").strip()
    if not value:
        return ""
    if not value.startswith("<"):
        value = f"<{value}"
    if not value.endswith(">"):
        value = f"{value}>"
    return value


def rfc_id_list(*headers: str) -> list[str]:
    """Message-Ids named in References / In-Reply-To headers, de-duplicated."""
    out: list[str] = []
    for header in headers:
        for token in (header or "").replace(",", " ").split():
            norm = normalize_rfc_id(token)
            if norm and norm not in out:
                out.append(norm)
    return out


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
    # LinkedIn hit-reply notifications put the per-thread email bridge in
    # Reply-To (uuid@reply.linkedin.com). Empty when the header is absent.
    reply_to: str = ""
    label_ids: list[str] = field(default_factory=list)
    # RFC 822 Message-Id ("<...>") and the ids it replies to (References +
    # In-Reply-To). Thread ids differ between Gmail and IMAP mailboxes; these
    # don't, so they tie the two copies of one thread to the same lead (MAT-4).
    rfc_message_id: str = ""
    references: list[str] = field(default_factory=list)

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
