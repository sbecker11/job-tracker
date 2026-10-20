"""Decline a lead: a polite "no thank you" letter, a reasoned skip, and the
hooks Pending Actions uses when the recruiter writes back afterwards.

The pipeline never sends anything. Shawn copies the letter into LinkedIn or
Gmail; `record_decline` then logs it as an outbound conversation, skips the
lead, and stores the reason so a later reply can be judged against it.

Letters follow the ~/CLAUDE.md §12 short-form rules: "Best, Shawn" sign-off,
no compensation figures, no work-authorization statements.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from job_tracker.pipeline.models import JobConversation
from job_tracker.pipeline.store import (
    add_job_conversation,
    advance_status,
    append_lead_note,
    utc_now_iso,
)


@dataclass(frozen=True)
class DeclineReason:
    code: str
    label: str
    sentence: str
    # Words in a recruiter's reply suggesting the declined-on term changed.
    terms_changed_re: re.Pattern[str] | None


DECLINE_REASONS: tuple[DeclineReason, ...] = (
    DeclineReason(
        "not_qualified",
        "Not fully qualified",
        "After reviewing the requirements, I don't think I'm a strong enough match for what the "
        "team needs, so I'll step aside rather than take up your time.",
        re.compile(r"(?i)\b(still (a )?(good|great|strong) (fit|match)|flexib\w*|nice[- ]to[- ]have|"
                   r"not (a )?(hard|strict) requirement|open to|would still like)\b"),
    ),
    DeclineReason(
        "rate_too_low",
        "Rate too low",
        "I appreciate the details, but the rate for this engagement is below what I can accept, "
        "so I'll pass on this one.",
        re.compile(r"(?i)\b(rate|budget|increase|raise|higher|bump|more room|negotiab\w*|flexib\w*)\b"),
    ),
    DeclineReason(
        "period_too_short",
        "Period too short",
        "I'm looking for a full-time role or an engagement of at least 12 months, so this one is "
        "shorter than I can commit to.",
        re.compile(r"(?i)\b(extend\w*|extension|renew\w*|long[- ]term|12\+? months|contract[- ]to[- ]hire|"
                   r"conversion|convert|full[- ]time|perm\w*)\b"),
    ),
    DeclineReason(
        "location",
        "Must be remote or a short commute",
        "I'm only considering roles that are 100% remote, or on-site within about a 15-minute "
        "commute of Lehi, Utah, so this one isn't a fit.",
        re.compile(r"(?i)\b(remote|work from home|wfh|lehi|utah|salt lake|provo|hybrid)\b"),
    ),
    DeclineReason(
        "no_travel",
        "Can't do on-site travel",
        "The on-site travel this role requires isn't something I'm able to take on, so I'll have "
        "to pass.",
        re.compile(r"(?i)\b(no travel|travel (is )?(optional|not required)|remote|waive\w*)\b"),
    ),
    DeclineReason("other", "Other", "", None),
)

_BY_CODE = {r.code: r for r in DECLINE_REASONS}

DECLINE_CLOSING = (
    "Thanks again for thinking of me, and please keep me in mind for future roles that are a "
    "closer fit.\n\nBest,\nShawn"
)


def decline_reason(code: str) -> DeclineReason:
    try:
        return _BY_CODE[code]
    except KeyError as exc:
        raise ValueError(f"unknown decline reason {code!r} (expected one of {sorted(_BY_CODE)})") from exc


# Lead fields the pipeline fills in before a recruiter names the role/client,
# e.g. "Role TBD" or "North Grand Technology (end-client TBD)".
_PLACEHOLDER_RE = re.compile(r"(?i)\b(tbd|unknown|n/a)\b")
_PLACEHOLDER_PAREN_RE = re.compile(r"\s*\([^)]*\b(?:tbd|unknown)\b[^)]*\)", re.IGNORECASE)


def first_name(recruiter_name: str) -> str:
    """Greeting name: "Kimber Gabryszak" -> "Kimber"; blank or an email -> "there"."""
    parts = (recruiter_name or "").strip().split()
    if not parts or "@" in parts[0]:
        return "there"
    return parts[0]


def clean_role_fields(company: str = "", title: str = "") -> tuple[str, str]:
    """Drop pipeline placeholders ("Role TBD", "(end-client TBD)") before they reach a message."""
    title = (title or "").strip()
    if _PLACEHOLDER_RE.search(title):
        title = ""
    company = _PLACEHOLDER_PAREN_RE.sub("", company or "").strip()
    if _PLACEHOLDER_RE.search(company):
        company = ""
    return company, title


def decline_greeting(*, recruiter_name: str = "", company: str = "", title: str = "") -> str:
    who = first_name(recruiter_name)
    company, title = clean_role_fields(company, title)
    if title and company:
        about = f" about the {title} role at {company}"
    elif title:
        about = f" about the {title} role"
    elif company:
        about = f" about the role at {company}"
    else:
        about = ""
    end = "" if about.endswith(".") else "."
    return f"Hi {who},\n\nThank you for reaching out{about}{end}"


def reason_sentence(code: str, note: str = "") -> str:
    reason = decline_reason(code)
    note = (note or "").strip()
    if reason.code == "other":
        if not note:
            raise ValueError("decline reason 'other' needs a note")
        return note
    return reason.sentence


def draft_decline_letter(
    code: str, *, recruiter_name: str = "", company: str = "", title: str = "", note: str = ""
) -> str:
    greeting = decline_greeting(recruiter_name=recruiter_name, company=company, title=title)
    return f"{greeting} {reason_sentence(code, note)} {DECLINE_CLOSING}"


def terms_changed(code: str, reply_text: str) -> bool:
    """Cheap keyword hint that a post-decline reply revisits the declined-on term."""
    reason = _BY_CODE.get(code or "")
    if reason is None or reason.terms_changed_re is None:
        return False
    return bool(reason.terms_changed_re.search(reply_text or ""))


def draft_post_decline_reply(*, recruiter_name: str = "", company: str = "", title: str = "") -> str:
    who = first_name(recruiter_name)
    _, title = clean_role_fields("", title)
    role = f" on the {title} role" if title else ""
    return (
        f"Hi {who},\n\nThanks for getting back to me. My answer{role} is the same for now, "
        f"but I appreciate you keeping me in mind for anything that's a closer fit.\n\nBest,\nShawn"
    )


def record_decline(
    conn: sqlite3.Connection,
    normalized_key: str,
    *,
    code: str,
    note: str = "",
    letter: str = "",
    channel: str = "other",
    when: str | None = None,
) -> None:
    """Skip the lead with a structured reason; log the sent letter (if any) as outbound."""
    reason = decline_reason(code)
    row = conn.execute(
        "SELECT status FROM job_leads WHERE normalized_key = ?", (normalized_key,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no lead with normalized_key {normalized_key!r}")
    when = when or utc_now_iso()
    label = reason.label if reason.code != "other" else f"Other: {(note or '').strip()}"
    if letter:
        add_job_conversation(
            conn,
            JobConversation(
                job_key=normalized_key,
                message_id=f"decline:{normalized_key}:{when}",
                channel=channel,
                direction="outbound",
                summary=f"Declined ({label})",
                occurred_at=when,
                body_text=letter,
            ),
            awaiting_response=False,
        )
    prior = row["status"]
    advance_status(conn, normalized_key, "skipped", when=when, force=True)
    conn.execute(
        """
        UPDATE job_leads
        SET awaiting_response_since = NULL, decline_reason = ?, decline_note = ?, declined_at = ?
        WHERE normalized_key = ?
        """,
        (reason.code, (note or "").strip(), when, normalized_key),
    )
    conn.commit()
    sent = "letter sent" if letter else "no letter"
    append_lead_note(
        conn, normalized_key, f"Declined {prior} -> skipped ({sent}): {label}", when=when
    )
