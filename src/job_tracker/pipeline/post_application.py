"""Detect and act on post-application signals in a message that has already
been matched to an EXISTING tracked job lead — as opposed to `email/
classifier.py`'s `classify()`, which only ever runs on a fresh, unmatched
message deciding whether there's a pursuable job in it at all.

Added 2026-07-22 per the candidate's explicit request to auto-track four
categories of post-application correspondence against a job-lead:

1. confirmation of application received  -> APPLICATION_RECEIVED
2. rejection of submitted application     -> REJECTION
3. invitation/congratulations re: interviews -> INTERVIEW_INVITE
4. recruiter next-steps discussion        -> NEXT_STEPS (no status change)

Callers (`cli/scan_communications.py`, `cli/triage_recruiter_inbox.py`'s
existing-lead short-circuit, `cli/triage_imap_inbox.py`) already resolve a
message to a `job_key` via `pipeline.comms_match.match_message_to_job` before
this module ever runs — `classify_post_application()` only decides WHICH of
the four signals (if any) the message carries, and `apply_post_application_
signal()` translates that into a `pipeline/store.py` write, guarded so an
out-of-order or late-arriving message can never move a lead BACKWARD in its
pipeline (e.g. a stray "we've received your application" digest re-send
arriving after the candidate is already interviewing must not reset status
to "applied").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from re import Pattern

from job_tracker.email.classifier import detect_rejection_reasons

# Confirmation that an application was received/is being reviewed — the
# generic ATS auto-reply shape ("Thank you for applying... we've received
# your information... we'll be in touch"). Deliberately distinct from (and
# checked AFTER) rejection/interview patterns below, since either of those
# can co-occur with "thank you for applying" as a preamble and should win.
_APPLICATION_RECEIVED_PATTERNS: list[tuple[str, Pattern[str]]] = [
    ("body: thank you for applying", re.compile(r"thank you for (?:your )?(?:application|applying)", re.I)),
    ("body: application received", re.compile(r"(?:we(?:'ve| have)|your application (?:has been|was))\s+received", re.I)),
    ("body: reviewing your application", re.compile(r"review(?:ing)? your application", re.I)),
    ("body: application submitted successfully", re.compile(r"application (?:has been |was )?submitted", re.I)),
    ("body: received your information", re.compile(r"received your (?:information|resume|application materials)", re.I)),
]

# A genuine invite to interview, or congratulations for advancing to one —
# distinct from a rejection (checked first, always wins) and from a vague
# "let's set up time to chat" cold-outreach pitch (RECRUITER_OUTREACH territory
# in classifier.py, which never reaches this module since it has no matched
# job_key to begin with).
_INTERVIEW_INVITE_PATTERNS: list[tuple[str, Pattern[str]]] = [
    ("body: invite to interview", re.compile(r"invit(?:e|ing|ation)\s+(?:you\s+)?(?:to|for)\s+(?:an?\s+)?interview", re.I)),
    ("body: schedule an interview/call", re.compile(r"schedule\s+(?:an?\s+)?(?:interview|call|phone screen)", re.I)),
    ("body: move forward to interview", re.compile(r"move\s+forward\s+to\s+(?:the\s+)?(?:next\s+step|interview)", re.I)),
    ("body: advance to next round", re.compile(r"advance(?:d)?\s+to\s+the\s+(?:next\s+round|interview)", re.I)),
    ("body: congratulations re: interview", re.compile(r"congratulations.{0,60}(?:interview|next round|next step)", re.I)),
    ("body: like to set up an interview", re.compile(r"(?:like|love)\s+to\s+set\s+up\s+(?:an?\s+)?interview", re.I)),
    ("body: phone screen", re.compile(r"phone\s+screen", re.I)),
    ("body: technical interview", re.compile(r"technical\s+interview", re.I)),
]


class PostApplicationLabel(str, Enum):
    """One label per already-linked message; priority order is defined in
    `classify_post_application()` below (rejection > interview > application
    received > next steps)."""

    REJECTION = "rejection"
    INTERVIEW_INVITE = "interview_invite"
    APPLICATION_RECEIVED = "application_received"
    NEXT_STEPS = "next_steps"


@dataclass
class PostApplicationClassification:
    label: PostApplicationLabel
    confidence: float
    reasons: list[str] = field(default_factory=list)


def _find_patterns(text: str, patterns: list[tuple[str, Pattern[str]]]) -> list[str]:
    return [name for name, pat in patterns if pat.search(text)]


def classify_post_application(text: str) -> PostApplicationClassification:
    """Classify a message ALREADY matched to an existing job lead into one of
    the four post-application signals. `text` should be the message's
    combined subject+body (e.g. `EmailMessage.combined_text`).

    Order matters: a rejection phrase always wins even if the same message
    also opens with "thank you for applying" (a common soft-decline
    preamble) or mentions an earlier interview. An interview-invite phrase
    wins over a plain application-received confirmation. Anything matching
    neither is NEXT_STEPS — the safe, no-status-change default for ordinary
    recruiter correspondence on an already-tracked lead (scheduling notes,
    rate discussions, general updates).
    """
    rejection_hits = detect_rejection_reasons(text)
    if rejection_hits:
        return PostApplicationClassification(PostApplicationLabel.REJECTION, 0.9, rejection_hits)

    interview_hits = _find_patterns(text, _INTERVIEW_INVITE_PATTERNS)
    if interview_hits:
        return PostApplicationClassification(PostApplicationLabel.INTERVIEW_INVITE, 0.85, interview_hits)

    received_hits = _find_patterns(text, _APPLICATION_RECEIVED_PATTERNS)
    if received_hits:
        return PostApplicationClassification(PostApplicationLabel.APPLICATION_RECEIVED, 0.8, received_hits)

    return PostApplicationClassification(
        PostApplicationLabel.NEXT_STEPS, 0.5, ["no rejection/interview/application-received phrase matched"]
    )


# The subset of models.LEAD_STAGES this module is allowed to move a lead
# FORWARD through automatically, in pipeline order. Deliberately excludes
# every off-ramp (skipped/rejected/deleted/unavailable/hired) — those are
# either applied by this module itself (rejected, via REJECTION) or reserved
# for a human/other tooling (skipped/deleted/unavailable/hired), never
# silently overwritten by an auto-detected signal.
_FORWARD_STAGES: tuple[str, ...] = (
    "new",
    "pursued",
    "package_generated",
    "applied",
    "following_up",
    "interviewing",
    "offered",
    "accepted",
    "started",
)

# What each signal advances a lead TO, when safe to do so automatically.
_SIGNAL_TARGET_STAGE: dict[PostApplicationLabel, str] = {
    PostApplicationLabel.APPLICATION_RECEIVED: "applied",
    PostApplicationLabel.INTERVIEW_INVITE: "interviewing",
}


def _forward_stage_rank(stage: str | None) -> int | None:
    """The stage's index in `_FORWARD_STAGES`, or None for a stage outside
    it entirely (a terminal off-ramp — skipped/rejected/deleted/
    unavailable/hired — or an unrecognized/missing status). None is never
    "less than" or "greater than" any real rank in the comparison below —
    callers must check for it explicitly — so a terminal lead can never be
    silently advanced (nor blocked from a *deliberate* one-off write like
    `record_rejection`, which doesn't go through this function at all)."""
    try:
        return _FORWARD_STAGES.index(stage) if stage is not None else None
    except ValueError:
        return None


def apply_post_application_signal(
    conn,
    job_key: str,
    classification: PostApplicationClassification,
    *,
    message_id: str = "",
    email_text: str = "",
    when: str | None = None,
) -> str:
    """Apply `classification`'s signal to the tracked lead at `job_key`,
    respecting the forward-only guard above. Returns a short human-readable
    action string for logging ("" when nothing changed — either NEXT_STEPS,
    or a real signal that was a no-op because the lead is already at/past
    that stage or sitting in a terminal off-ramp).

    REJECTION is the one signal applied regardless of current stage rank
    (short of already being "rejected") — `store.record_rejection` is
    idempotent-ish (just re-advances/re-stamps), and a rejection arriving
    for a lead in ANY forward stage is real, actionable signal worth
    recording rather than silently dropping.
    """
    from job_tracker.pipeline import store

    current_status = store.get_lead_status(conn, job_key)

    if classification.label == PostApplicationLabel.REJECTION:
        if current_status == "rejected":
            return ""
        store.record_rejection(
            conn, job_key, source="email", email_text=email_text, message_id=message_id, when=when
        )
        return f"status -> rejected (was {current_status!r})"

    target_stage = _SIGNAL_TARGET_STAGE.get(classification.label)
    if target_stage is None:
        return ""  # NEXT_STEPS — archive-only, no status change by design

    current_rank = _forward_stage_rank(current_status)
    if current_rank is None:
        return ""  # lead is in a terminal off-ramp (or missing) — never auto-advance out of it
    target_rank = _forward_stage_rank(target_stage)
    if current_rank >= target_rank:
        return ""  # already at/past this stage — no-op

    store.advance_status(conn, job_key, target_stage, when=when)
    return f"status -> {target_stage} (was {current_status!r})"
