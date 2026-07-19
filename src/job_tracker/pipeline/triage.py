"""Triage a single recruiter-inbox message end to end: classify -> extract ->
resolve JD -> LLM evaluate (+ auto-generate a résumé/cover letter on a
"pursue" verdict) -> decide a message-level PURSUE / SKIP / NEEDS_REVIEW
outcome.

Deliberately a different code path from `pipeline/run.py` (classify ->
extract -> resolve -> keyword-score -> store, used by `run_pipeline.py`'s
free/cheap dry-run reporting): this one always calls the LLM Match
Framework (`pipeline/llm_apply.py`), never the free keyword scorer, because
the whole point of this module is a same-session decision confident enough
to actually relabel and archive the source email — and CLAUDE.md's JD
Match Framework, not the keyword heuristic, is what that confidence should
be based on.

This module only decides; it never touches Gmail, and never persists leads
or message outcomes itself — see `scripts/triage_recruiter_inbox.py` for the
caller that does that and applies the Gmail label/archive. The one exception
is `conn`, optionally passed through to `pipeline/llm_extract.py`'s
`llm_extraction_cache` table (see `use_llm_extraction_fallback` below) so a
digest's extraction isn't re-billed on every `--force` re-triage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from job_tracker.email.classifier import classify
from job_tracker.email.labels import Label
from job_tracker.email.models import EmailMessage, ExtractedRole
from job_tracker.pipeline.extract import extract_roles
from job_tracker.pipeline.llm_apply import (
    DEFAULT_MODEL,
    DEFAULT_OUTPUT_ROOT,
    TwoTierPackageResult,
    generate_two_tier_package,
)
from job_tracker.pipeline.llm_extract import DEFAULT_MODEL as DEFAULT_LLM_EXTRACT_MODEL
from job_tracker.pipeline.llm_extract import extract_roles_llm, extract_roles_llm_cached
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.run import choose_apply_url, resolve_jd_text
from job_tracker.pipeline.store import DEFAULT_REJECTION_COOLDOWN_DAYS, find_recent_rejection, get_sibling_titles
from job_tracker.scoring.scorer import ScoreResult

# Renamed 2026-07-07 (were ACCEPT/DENY) to match the LLM Match Framework's
# own verdict language (evaluate_lead()'s "pursue"/"pass"/"review") — see
# gmail_writer.PURSUE_LABEL/SKIP_LABEL for the corresponding Gmail labels.
PURSUE = "PURSUE"
SKIP = "SKIP"
NEEDS_REVIEW = "NEEDS_REVIEW"

# A digest can list dozens of postings; evaluating every one through the full
# LLM Match Framework (Sonnet, one call per role) would multiply cost for a
# single email. Cap to the top N by extraction confidence — plenty to surface
# anything worth pursuing without unbounded spend on one message.
DEFAULT_MAX_LLM_EXTRACTED_ROLES = 8

# Messages job-tracker's own classifier is confident carry no pursuable job
# get SKIP without spending on an LLM call at all.
_SKIP_LABELS = {Label.NOISE, Label.REJECTION}
# RECRUITER_OUTREACH has no JD to score by design (personalized outreach
# text, not a posting) — always NEEDS_REVIEW, never worth an extraction
# attempt. LINK_ONLY_DIGEST used to be here too (see
# `_extract_roles_with_fallback` below for why it no longer short-circuits).
_NEEDS_REVIEW_LABELS = {Label.RECRUITER_OUTREACH}
# Labels `pipeline/extract.py`'s regex pass can meaningfully extract from.
# LINK_ONLY_DIGEST is deliberately excluded there (see its docstring) — for
# that label, the LLM extraction fallback (opt-in) is the ONLY way to get
# roles out; there is no regex pass to even try.
_REGEX_EXTRACTABLE_LABELS = {Label.SINGLE_JD, Label.MULTI_JD_IN_BODY}


@dataclass
class RoleOutcome:
    lead: JobLead
    package: TwoTierPackageResult


@dataclass
class MessageTriageResult:
    message_id: str
    subject: str
    from_address: str
    outcome: str  # PURSUE | SKIP | NEEDS_REVIEW
    reason: str
    classifier_label: str
    roles: list[RoleOutcome] = field(default_factory=list)
    extraction_issue: str = ""
    # Whether extraction is judged to have found everything there was to
    # find (or correctly determined there was nothing to find), as opposed
    # to "gave up" / "might be more beyond what we pulled out". Only
    # meaningfully consulted by callers for messages that ALSO carry a raw,
    # non-job-tracker Gmail label like the recruiting account's "Job-Digests"
    # filter label (see scripts/triage_recruiter_inbox.py) — for those, an
    # incomplete extraction should leave the source message visible in the
    # inbox rather than filing it away as if it were fully handled. True by
    # default since it's irrelevant for any message without such a label.
    extraction_complete: bool = True


def _extract_roles_with_fallback(
    message: EmailMessage,
    label: Label,
    *,
    use_llm_extraction_fallback: bool,
    llm_extraction_model: str,
    max_llm_extracted_roles: int,
    conn=None,
    llm_extract_client=None,
) -> tuple[list[ExtractedRole], str, bool]:
    """Regex extraction first (free), LLM extraction fallback second (opt-in,
    cheap Haiku, cached by message_id) — mirrors `pipeline/run.py`'s
    `use_llm_fallback` behavior, but also covers LINK_ONLY_DIGEST, which
    `pipeline/extract.py`'s regex pass never attempts at all (there's no
    regex shape general enough for arbitrary digest layouts).

    Returns (roles, extraction_source, truncated) where extraction_source is
    "regex", "llm", or "none" (nothing found either way) — used for the
    triage reason string so it's clear which path produced (or failed to
    produce) a result — and `truncated` is True only when the LLM fallback
    found MORE complete roles than `max_llm_extracted_roles` and had to cut
    the list down, i.e. there's a real chance this digest had roles left on
    the table rather than genuinely containing only what got returned.
    """
    roles: list[ExtractedRole] = extract_roles(message, label) if label in _REGEX_EXTRACTABLE_LABELS else []
    regex_found_complete = any(r.company and r.title for r in roles)
    if regex_found_complete or not use_llm_extraction_fallback:
        return roles, ("regex" if regex_found_complete else "none"), False

    if conn is not None:
        llm_roles = extract_roles_llm_cached(conn, message, model=llm_extraction_model, client=llm_extract_client)
    else:
        llm_roles = extract_roles_llm(message, model=llm_extraction_model, client=llm_extract_client)

    complete_llm_roles = [r for r in llm_roles if r.company and r.title]
    if not complete_llm_roles:
        return roles, "none", False

    complete_llm_roles.sort(key=lambda r: r.confidence, reverse=True)
    truncated = len(complete_llm_roles) > max_llm_extracted_roles
    return complete_llm_roles[:max_llm_extracted_roles], "llm", truncated


def effective_verdict(package: TwoTierPackageResult) -> str:
    """The two-tier pipeline's LLM stage only runs once the free rule-based
    pass clears its gate (see `llm_apply.generate_two_tier_package`) — for
    a role that never cleared it, the rule-based verdict IS the verdict.

    Public (no leading underscore, 2026-07-18): originally private to this
    module, but `cli/triage_recruiter_inbox.py` needed the exact same
    fallback after hitting the same `evaluation is None` bug in three
    separate places of its own — see that file's history for the crashes
    this was fixing."""
    return package.evaluation.verdict if package.evaluation is not None else package.no_llm_score.verdict


def _effective_rationale(package: TwoTierPackageResult) -> list[str]:
    if package.evaluation is not None:
        return [package.evaluation.rationale] if package.evaluation.rationale else []
    return list(package.no_llm_score.rationale)


def decide_outcome_from_verdicts(verdicts: set[str] | list[str]) -> tuple[str, str]:
    """The PURSUE > NEEDS_REVIEW > SKIP priority rule, factored out
    (2026-07-19) from `_decide_outcome` so `cli/resync_labels.py` can apply
    the exact same message-level rollup logic when re-deriving a message's
    outcome from its linked leads' CURRENT verdicts, instead of the
    verdicts of a fresh `RoleOutcome` batch. Any verdict string outside
    {"pursue", "review", "pass"} (e.g. a lead still carrying the special
    "REVIEW NEEDED" marker) is treated as "review" — see callers."""
    verdicts = set(verdicts)
    if not verdicts:
        return NEEDS_REVIEW, "no roles extracted"
    if "pursue" in verdicts:
        return PURSUE, "at least one role scored 'pursue'"
    if "review" in verdicts:
        return NEEDS_REVIEW, "at least one role scored 'review', none scored 'pursue'"
    return SKIP, "every role scored 'pass'"


def _decide_outcome(role_outcomes: list[RoleOutcome]) -> tuple[str, str]:
    if not role_outcomes:
        return NEEDS_REVIEW, "no roles extracted"
    verdicts = {effective_verdict(r.package) for r in role_outcomes}
    return decide_outcome_from_verdicts(verdicts)


def triage_message(
    message: EmailMessage,
    *,
    model: str = DEFAULT_MODEL,
    generate: bool = True,
    force_llm_review: bool = False,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    resolve_full_jd: bool = True,
    ats_verbose: bool = False,
    postings_cache: dict[str, list] | None = None,
    client=None,
    use_llm_extraction_fallback: bool = False,
    llm_extraction_model: str = DEFAULT_LLM_EXTRACT_MODEL,
    max_llm_extracted_roles: int = DEFAULT_MAX_LLM_EXTRACTED_ROLES,
    conn=None,
    llm_extract_client=None,
    rejection_cooldown_days: int = DEFAULT_REJECTION_COOLDOWN_DAYS,
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

    if classification.label in _SKIP_LABELS:
        result.outcome = SKIP
        result.reason = f"classified as {classification.label.value}: {'; '.join(classification.reasons)}"
        # Correctly identified as not job content at all — there was never
        # anything to extract, so this counts as "complete", not "gave up".
        result.extraction_complete = True
        return result

    if classification.label in _NEEDS_REVIEW_LABELS:
        result.outcome = NEEDS_REVIEW
        result.reason = f"classified as {classification.label.value} — needs a human look, not an auto-decision"
        # No JD to score by design — extraction was never even attempted.
        result.extraction_complete = False
        return result

    roles, extraction_source, extraction_truncated = _extract_roles_with_fallback(
        message,
        classification.label,
        use_llm_extraction_fallback=use_llm_extraction_fallback,
        llm_extraction_model=llm_extraction_model,
        max_llm_extracted_roles=max_llm_extracted_roles,
        conn=conn,
        llm_extract_client=llm_extract_client,
    )
    complete_roles = [r for r in roles if r.company and r.title]
    if not complete_roles:
        result.outcome = NEEDS_REVIEW
        result.extraction_complete = False
        if classification.label is Label.LINK_ONLY_DIGEST and extraction_source == "none" and not use_llm_extraction_fallback:
            result.extraction_issue = "classified as link-only-digest — needs a human look, not an auto-decision"
        elif not roles:
            result.extraction_issue = "no roles extracted"
        else:
            result.extraction_issue = "incomplete extraction (missing company or title)"
        result.reason = result.extraction_issue
        return result

    role_outcomes: list[RoleOutcome] = []
    for role in complete_roles:
        # Disqualification (2026-07-14): this exact role, at this exact
        # company, was already confirmed rejected within the cooldown
        # window (see store.find_recent_rejection/record_rejection) — most
        # often a digest re-sending the same still-open posting, or a
        # different recruiter re-surfacing it. Short-circuits straight to a
        # "pass" verdict before spending anything on JD resolution or the
        # two-tier review pipeline; `rejection_cooldown_days <= 0` disables
        # this check entirely (e.g. for callers that want it off).
        recent_rejection = (
            find_recent_rejection(conn, role.company, role.title, within_days=rejection_cooldown_days)
            if conn is not None and rejection_cooldown_days > 0
            else None
        )
        if recent_rejection is not None:
            rationale = [
                f"disqualified: {recent_rejection['company']} already rejected this role "
                f"(rejected_at={recent_rejection['rejected_at']}) — within the "
                f"{rejection_cooldown_days}-day cooldown window"
            ]
            lead = JobLead(
                company=role.company,
                title=role.title,
                source_message_id=message.id,
                source_label=classification.label.value,
                apply_url=role.apply_url,
                extraction_confidence=role.confidence,
                jd_resolved=False,
                jd_source="",
                jd_text=role.snippet or "",
                match_pct=0.0,
                matched_skills=[],
                verdict="pass",
                rationale=rationale,
            )
            package = TwoTierPackageResult(no_llm_score=ScoreResult(match_pct=0.0, verdict="pass", rationale=rationale))
            role_outcomes.append(RoleOutcome(lead=lead, package=package))
            continue

        # Sibling titles for this company, combining what's already in the
        # DB with any other role in *this same* digest/message for the same
        # company — the latter matters because upsert_lead() for those
        # siblings doesn't happen until after this whole function returns
        # (see triage_recruiter_inbox.py), so a fresh multi-role digest for
        # one company would otherwise look single-lead to every role in it.
        sibling_titles = set(get_sibling_titles(conn, role.company, exclude_title=role.title)) if conn else set()
        sibling_titles |= {
            other.title for other in complete_roles if other.company == role.company and other.title != role.title
        }
        sibling_titles_tuple = tuple(sibling_titles)
        jd_text = ""
        if resolve_full_jd:
            jd_text, jd_resolved, resolved_url = resolve_jd_text(
                role.company, role.title, verbose=ats_verbose, postings_cache=postings_cache
            )
        else:
            jd_resolved, resolved_url = False, ""
        # Fixed 2026-07-07: prefer the role's own isolated text (a bullet
        # line, a "more details"/"Ref no." chunk, or an LLM-extracted
        # excerpt — see ExtractedRole.snippet) over the ENTIRE raw email
        # before falling back to it. Without this, every role fanned out of
        # a multi-job digest whose ATS lookup failed got scored against the
        # whole digest — including every sibling listing's requirements —
        # which was silently corrupting dealbreaker/skills matching (e.g. a
        # role could "inherit" a dealbreaker keyword that only appeared in a
        # different listing in the same email).
        used_role_snippet = False
        if not jd_text and role.snippet:
            jd_text = role.snippet
            used_role_snippet = True
        if not jd_text:
            jd_text = message.combined_text

        # Prefer the ATS-resolved canonical URL over a LinkedIn tracking
        # link specifically (see choose_apply_url's docstring) — computed
        # once and reused below so the package's stamped URL and the
        # stored lead's URL can never disagree.
        chosen_apply_url = choose_apply_url(role.apply_url, resolved_url)

        # Two-tier pipeline (2026-07-11): the free rule-based pass
        # (no-LLM-review.docx) always runs; the LLM call (full-LLM-review)
        # only runs once that clears its gate. `generate=False` (the
        # `--no-generate` CLI flag) still respects that same gate — it only
        # additionally skips résumé/cover-letter generation on a pursue.
        package = generate_two_tier_package(
            jd_text,
            company=role.company,
            title=role.title,
            apply_url=chosen_apply_url,
            model=model,
            client=client,
            output_root=output_root,
            multi_lead=len(sibling_titles_tuple) > 0,
            sibling_titles=sibling_titles_tuple,
            generate=generate,
            force_llm_review=force_llm_review,
        )

        lead = JobLead(
            company=role.company,
            title=role.title,
            source_message_id=message.id,
            source_label=classification.label.value,
            apply_url=chosen_apply_url,
            extraction_confidence=role.confidence,
            jd_resolved=jd_resolved,
            jd_source=(
                "ats_api" if jd_resolved else "digest_snippet" if used_role_snippet else "email_body"
            ),
            jd_text=jd_text,
            match_pct=package.no_llm_score.match_pct,
            matched_skills=list(package.no_llm_score.matched_skills),
            verdict=effective_verdict(package),
            rationale=_effective_rationale(package),
        )
        role_outcomes.append(RoleOutcome(lead=lead, package=package))

    result.roles = role_outcomes
    result.outcome, result.reason = _decide_outcome(role_outcomes)
    # Roles were found and scored, but if the LLM extraction fallback hit its
    # cap there may be more roles in the digest than we pulled out — not
    # safe to treat as fully handled.
    result.extraction_complete = not extraction_truncated
    return result


