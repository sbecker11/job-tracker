"""Orchestrator: classify -> extract -> resolve JD -> score -> dedup/store.

This is the piece the README marked "planned" — it wires the previously
separate classify_inbox / resolve_jd tools into one pass over a batch of
messages, and is what makes "read the recruiting inbox and tell me what's
worth pursuing" an actual one-command operation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from job_tracker.ats.jd_resolver import gather_postings, resolve as resolve_ats_jd
from job_tracker.email.classifier import classify
from job_tracker.email.labels import Label
from job_tracker.email.models import EmailMessage
from job_tracker.pipeline.extract import extract_roles
from job_tracker.pipeline.models import JobLead
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, upsert_lead
from job_tracker.scoring.scorer import score_jd

_SKIP_LABELS = {Label.NOISE, Label.REJECTION, Label.LINK_ONLY_DIGEST}


@dataclass
class PipelineSummary:
    total_messages: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    outreach_needs_reply: list[dict] = field(default_factory=list)
    needs_review: list[dict] = field(default_factory=list)
    leads: list[dict] = field(default_factory=list)  # every lead scored this run
    new_leads: int = 0

    @property
    def pursue(self) -> list[dict]:
        return [lead for lead in self.leads if lead["verdict"] == "pursue"]

    @property
    def review(self) -> list[dict]:
        return [lead for lead in self.leads if lead["verdict"] == "review"]

    @property
    def passed(self) -> list[dict]:
        return [lead for lead in self.leads if lead["verdict"] == "pass"]


def _resolve_jd_text(
    company: str,
    title: str,
    *,
    verbose: bool = False,
    postings_cache: dict[str, list] | None = None,
) -> tuple[str, bool, str]:
    """Try the public ATS APIs for the full JD; fall back to caller-supplied text.

    `postings_cache` (keyed by lowercased company name) lets a batch run reuse
    one company's already-fetched board across every title requested for it —
    without this, a 3-role digest from the same employer would hit every ATS
    endpoint 3x for identical data. Essential once processing hundreds of
    backlog emails, many of which repeat the same handful of employers.
    """
    cache_key = company.strip().lower()
    try:
        postings = None
        if postings_cache is not None:
            if cache_key not in postings_cache:
                postings_cache[cache_key] = gather_postings(company, verbose=verbose)
            postings = postings_cache[cache_key]
        result = resolve_ats_jd(company, title, verbose=verbose, postings=postings)
    except Exception:
        return "", False, ""
    match = result.get("match")
    if result.get("accepted") and match and match.get("description"):
        return match["description"], True, match.get("url", "")
    return "", False, ""


def run_pipeline(
    messages: list[EmailMessage],
    *,
    db_path: Path = DEFAULT_DB_PATH,
    resolve_full_jd: bool = True,
    ats_verbose: bool = False,
) -> PipelineSummary:
    summary = PipelineSummary(total_messages=len(messages))
    conn = connect(db_path)
    postings_cache: dict[str, list] = {}

    try:
        for message in messages:
            result = classify(message)

            if result.label in _SKIP_LABELS:
                summary.skipped[result.label.value] = summary.skipped.get(result.label.value, 0) + 1
                continue

            if result.label == Label.RECRUITER_OUTREACH:
                summary.outreach_needs_reply.append(
                    {
                        "message_id": message.id,
                        "from": message.from_address,
                        "subject": message.subject,
                        "confidence": result.confidence,
                    }
                )
                continue

            roles = extract_roles(message, result.label)
            if not roles:
                summary.needs_review.append(
                    {
                        "message_id": message.id,
                        "reason": "no roles extracted",
                        "label": result.label.value,
                        "subject": message.subject,
                    }
                )
                continue

            for role in roles:
                if not role.company or not role.title:
                    summary.needs_review.append(
                        {
                            "message_id": message.id,
                            "reason": "incomplete extraction (missing company or title)",
                            "label": result.label.value,
                            "subject": message.subject,
                            "partial": {"company": role.company, "title": role.title},
                        }
                    )
                    continue

                jd_text = ""
                jd_resolved = False
                apply_url = role.apply_url
                if resolve_full_jd:
                    jd_text, jd_resolved, resolved_url = _resolve_jd_text(
                        role.company, role.title, verbose=ats_verbose, postings_cache=postings_cache
                    )
                    apply_url = apply_url or resolved_url
                if not jd_text:
                    jd_text = message.combined_text

                score = score_jd(jd_text)

                lead = JobLead(
                    company=role.company,
                    title=role.title,
                    source_message_id=message.id,
                    source_label=result.label.value,
                    apply_url=apply_url,
                    extraction_confidence=role.confidence,
                    jd_resolved=jd_resolved,
                    jd_source="ats_api" if jd_resolved else "email_body",
                    jd_text=jd_text,
                    match_pct=score.match_pct,
                    matched_skills=score.matched_skills,
                    verdict=score.verdict,
                    rationale=score.rationale,
                )
                is_new = upsert_lead(conn, lead)
                summary.new_leads += int(is_new)
                summary.leads.append(
                    {
                        "company": lead.company,
                        "title": lead.title,
                        "apply_url": lead.apply_url,
                        "jd_resolved": lead.jd_resolved,
                        "match_pct": lead.match_pct,
                        "matched_skills": lead.matched_skills,
                        "verdict": lead.verdict,
                        "rationale": lead.rationale,
                        "is_new": is_new,
                        "source_message_id": message.id,
                    }
                )
    finally:
        conn.close()

    return summary
