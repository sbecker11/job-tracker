"""Triage a single recruiter-inbox message end to end: classify -> extract ->
resolve JD -> LLM evaluate (+ auto-generate a résumé/cover letter on a
"pursue" verdict) -> decide a message-level ACCEPT / DENY / NEEDS_REVIEW
outcome.

Deliberately a different code path from `pipeline/run.py` (classify ->
extract -> resolve -> keyword-score -> store, used by `run_pipeline.py`'s
free/cheap dry-run reporting): this one always calls the LLM Match
Framework (`pipeline/llm_apply.py`), never the free keyword scorer, because
the whole point of this module is a same-session decision confident enough
to actually relabel and archive the source email — and CLAUDE.md's JD
Match Framework, not the keyword heuristic, is what that confidence should
be based on.

This module only decides; it never touches Gmail or the DB itself — see
`scripts/triage_recruiter_inbox.py` for the caller that persists leads,
records the message outcome, and applies the Gmail label/archive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from job_tracker.email.classifier import classify
from job_tracker.email.labels import Label
from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.extract import extract_roles
from job_tracker.pipeline.llm_apply import DEFAULT_MODEL, DEFAULT_OUTPUT_ROOT, PackageResult, generate_package
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.run import resolve_jd_text

ACCEPT = "ACCEPT"
DENY = "DENY"
NEEDS_REVIEW = "NEEDS_REVIEW"

# Messages job-tracker's own classifier is confident carry no pursuable job
# get DENY without spending on an LLM call at all.
_DENY_LABELS = {Label.NOISE, Label.REJECTION}
# Everything else ambiguous (a digest our classifier didn't expect on this
# curated account, or outreach with no JD to score) goes to NEEDS_REVIEW
# rather than being silently discarded either way.
_NEEDS_REVIEW_LABELS = {Label.LINK_ONLY_DIGEST, Label.RECRUITER_OUTREACH}


@dataclass
class RoleOutcome:
    lead: JobLead
    package: PackageResult


@dataclass
class MessageTriageResult:
    message_id: str
    subject: str
    from_address: str
    outcome: str  # ACCEPT | DENY | NEEDS_REVIEW
    reason: str
    classifier_label: str
    roles: list[RoleOutcome] = field(default_factory=list)
    extraction_issue: str = ""


def _decide_outcome(role_outcomes: list[RoleOutcome]) -> tuple[str, str]:
    if not role_outcomes:
        return NEEDS_REVIEW, "no roles extracted"
    verdicts = {r.package.evaluation.verdict for r in role_outcomes}
    if "pursue" in verdicts:
        return ACCEPT, "at least one role scored 'pursue'"
    if "review" in verdicts:
        return NEEDS_REVIEW, "at least one role scored 'review', none scored 'pursue'"
    return DENY, "every role scored 'pass'"


def triage_message(
    message: EmailMessage,
    *,
    model: str = DEFAULT_MODEL,
    generate: bool = True,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    resolve_full_jd: bool = True,
    ats_verbose: bool = False,
    postings_cache: dict[str, list] | None = None,
    client=None,
) -> MessageTriageResult:
    result = MessageTriageResult(
        message_id=message.id,
        subject=message.subject,
        from_address=message.from_address,
        outcome=NEEDS_REVIEW,
        reason="",
        classifier_label="",
    )

    classification = classify(message)
    result.classifier_label = classification.label.value

    if classification.label in _DENY_LABELS:
        result.outcome = DENY
        result.reason = f"classified as {classification.label.value}: {'; '.join(classification.reasons)}"
        return result

    if classification.label in _NEEDS_REVIEW_LABELS:
        result.outcome = NEEDS_REVIEW
        result.reason = f"classified as {classification.label.value} — needs a human look, not an auto-decision"
        return result

    roles = extract_roles(message, classification.label)
    complete_roles = [r for r in roles if r.company and r.title]
    if not complete_roles:
        result.outcome = NEEDS_REVIEW
        result.extraction_issue = "no roles extracted" if not roles else "incomplete extraction (missing company or title)"
        result.reason = result.extraction_issue
        return result

    role_outcomes: list[RoleOutcome] = []
    for role in complete_roles:
        jd_text = ""
        if resolve_full_jd:
            jd_text, jd_resolved, resolved_url = resolve_jd_text(
                role.company, role.title, verbose=ats_verbose, postings_cache=postings_cache
            )
        else:
            jd_resolved, resolved_url = False, ""
        if not jd_text:
            jd_text = message.combined_text

        package = (
            generate_package(
                jd_text,
                company=role.company,
                title=role.title,
                model=model,
                client=client,
                output_root=output_root,
            )
            if generate
            else _evaluate_only(jd_text, company=role.company, title=role.title, model=model, client=client)
        )

        lead = JobLead(
            company=role.company,
            title=role.title,
            source_message_id=message.id,
            source_label=classification.label.value,
            apply_url=role.apply_url or resolved_url,
            extraction_confidence=role.confidence,
            jd_resolved=jd_resolved,
            jd_source="ats_api" if jd_resolved else "email_body",
            jd_text=jd_text,
            verdict=package.evaluation.verdict,
            rationale=[package.evaluation.rationale] if package.evaluation.rationale else [],
        )
        role_outcomes.append(RoleOutcome(lead=lead, package=package))

    result.roles = role_outcomes
    result.outcome, result.reason = _decide_outcome(role_outcomes)
    return result


def _evaluate_only(jd_text: str, *, company: str, title: str, model: str, client=None) -> PackageResult:
    """`generate=False` path: score with the LLM but never spend on
    generation, regardless of verdict — used by `--no-generate`."""
    from job_tracker.pipeline.llm_apply import evaluate_lead

    evaluation = evaluate_lead(jd_text, company=company, title=title, model=model, client=client)
    return PackageResult(evaluation=evaluation)
