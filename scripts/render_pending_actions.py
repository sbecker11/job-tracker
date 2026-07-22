#!/usr/bin/env python3
"""Regenerate `var/pending-actions.html` from the current state of `leads.db`.

Replaces the ad-hoc "hand-rebuild the embedded JS data arrays in a one-off
heredoc" process used in prior sessions with one reusable, re-runnable
command:

    python scripts/render_pending_actions.py

By default this also refreshes every `status='new'` lead's rule-based
`match_pct`/`verdict`/`matched_skills` with the CURRENT scorer
(`scoring/scorer.py`) before rendering — necessary after the 2026-07-11
JD-relative rescale, since leads scored by an older `run_pipeline.py` run
still carry `match_pct` on the old "vs. whole career vocabulary" scale, not
the current "vs. this JD's own recognizable tech vocabulary" one. Pass
`--no-rescore` to render from whatever is already stored instead.

The output is a fully static, bookmarkable HTML file (open with
`file://.../var/pending-actions.html`) — no server, no live DB access from
the page itself; re-run this script any time the backlog changes.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _safe_filename  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, list_unmatched_messages  # noqa: E402
from job_tracker.scoring.scorer import DEFAULT_FRAMEWORK_PATH, load_framework, score_jd  # noqa: E402

DEFAULT_OUTPUT_HTML = _REPO_ROOT / "var" / "pending-actions.html"

# The same gate `scoring.scorer.should_run_llm_review()` uses to decide
# whether a `status='new'` lead is worth spending a real LLM call on.
# Read fresh from config/framework.yaml (not hardcoded) so this page can
# never silently drift out of sync with the actual pipeline threshold —
# it's what separates "awaiting LLM review" (cleared the gate, just hasn't
# been evaluated yet) from "not prioritized" (below the gate, LLM review
# will never automatically run on it).
LLM_REVIEW_GATE_PCT = (load_framework(DEFAULT_FRAMEWORK_PATH).get("thresholds") or {}).get("llm_review_min_pct", 70)

# Single source of truth for the "this lead is getting stale" amber-highlight
# threshold — interpolated into both the JS (STALE_DAYS) and the hint text
# below so the two can never drift apart.
STALE_DAYS_THRESHOLD = 21

_LEAD_COLUMNS = (
    "normalized_key, company, title, status, jd_text, jd_resolved, "
    "match_pct, matched_skills, verdict, rationale, "
    "llm_verdict, llm_match_pct, first_seen, apply_url, direct_recruiter_outreach"
)


def _rescore_new_leads(conn) -> int:
    """Recompute the free rule-based score for every `status='new'` lead
    that has `jd_text` on file, using the current `scoring.scorer.score_jd`.
    Never touches leads past "new" (mirrors `store.upsert_lead`'s own
    `CASE WHEN status = 'new'` guard) — once a human has acted on a lead,
    its stored score is a historical record, not something to silently
    rewrite out from under them. Returns the number of rows updated.

    Also skips `verdict = 'REVIEW NEEDED'` leads (bug fixed 2026-07-12):
    that verdict is a deliberate manual marker meaning "JD couldn't be
    resolved, needs a human" (see PRIMER.md's link-only-digest policy) —
    it's distinct from the scorer's normal pursue/review/pass output and
    must survive until a human clears it, even if the lead still carries
    some non-empty thin/stub `jd_text` that would otherwise make it look
    reCoverable to the query below."""
    rows = conn.execute(
        "SELECT normalized_key, jd_text FROM job_leads "
        "WHERE status = 'new' AND jd_text IS NOT NULL AND jd_text != '' AND verdict != 'REVIEW NEEDED'"
    ).fetchall()
    updated = 0
    for key, jd_text in rows:
        score = score_jd(jd_text)
        conn.execute(
            "UPDATE job_leads SET match_pct = ?, matched_skills = ?, verdict = ?, rationale = ? WHERE normalized_key = ?",
            (score.match_pct, json.dumps(score.matched_skills), score.verdict, json.dumps(score.rationale), key),
        )
        updated += 1
    conn.commit()
    return updated


def _lead_folder_and_count(output_root: Path, *, company: str, title: str, multi_lead: bool) -> tuple[str, str, int]:
    """This lead's package folder + the company root folder (both relative
    to `output_root`) plus a file count scoped to just the package folder.

    Mirrors `llm_apply._job_folder`'s naming rules (flat `<Company>/` for a
    single-lead company, nested `<Company>/<Company>_<Title>/` once a
    second lead exists) without its mkdir/migration side effects, since
    this only reads state to render a static page. 0 files if the package
    folder doesn't exist yet (e.g. a multi-lead company whose sibling
    hasn't triggered the on-disk migration out of the old flat layout yet
    — self-heals next time the real pipeline runs for that lead).

    Returns `(package_rel, company_rel, file_count)` so the page can link
    the company name to the shared company root and the title to this
    lead's own package folder.
    """
    company_safe = _safe_filename(company)
    package_rel = (
        f"{company_safe}/{_safe_filename(f'{company}_{title}')}" if multi_lead else company_safe
    )
    lead_dir = output_root / package_rel
    count = sum(1 for p in lead_dir.rglob("*") if p.is_file()) if lead_dir.is_dir() else 0
    return package_rel, company_safe, count


def _has_resume_and_cover(folder: Path) -> bool:
    """True if `folder` already contains both a résumé and a cover-letter
    docx — the two artifacts `llm_apply.generate_package()` writes on a
    *pursue* verdict (see CLAUDE.md §11). Matched case-insensitively by
    substring ("resume" / "cover") rather than the exact
    `Shawn_Becker_Resume_...` / `Shawn_Becker_coverLetter_...` naming, since
    the cover-letter file's casing has drifted slightly in practice (e.g.
    `coverLetter` vs `Cover_Letter`) and this only needs to answer "did the
    package actually get written," not enforce the naming convention
    itself. Used to build the "Ready to apply" section below — a DB status
    of `package_generated` on its own is a claim, not proof; this checks
    the claim against what's actually on disk."""
    if not folder.is_dir():
        return False
    names = [p.name.lower() for p in folder.glob("*.docx")]
    return any("resume" in n for n in names) and any("cover" in n for n in names)


def _fmt_pct(pct: float | None) -> float:
    return round(pct or 0.0, 1)


def _company_label(company: str, count: int) -> str:
    return company if count <= 1 else f"{company} (x{count})"


def _age_days(first_seen: str | None, now: datetime) -> int:
    """Whole days since `first_seen` (job_leads.first_seen, set once at
    ingest by upsert_lead and never touched again) — the basis for the
    "value decays with age" sort/display added 2026-07-15: a lead sitting
    unreviewed gets less useful the longer it sits (the posting may fill,
    the JD may go stale, a digest re-send may already be a re-post rather
    than new), so surfacing the oldest unreviewed leads first is more
    actionable than match-score-only ordering. Falls back to 0 (today) for
    the rare row missing/unparsable first_seen rather than raising, since
    this only drives a display sort, not a disqualification decision."""
    if not first_seen:
        return 0
    try:
        seen = datetime.fromisoformat(first_seen)
    except ValueError:
        return 0
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=now.tzinfo)
    return max(0, (now - seen).days)


def render(conn, *, output_root: Path, now: datetime) -> dict:
    """Builds a "sales funnel toward Ready to apply" (added 2026-07-15,
    replacing the earlier flatter needs-review/auto-skipped/unresolved
    split): every `status='new'` or `status='package_generated'` lead lands
    in exactly one bucket below, ordered by how close it is to the one
    manual action that matters — submitting an application with generated
    documents:

        JD unresolved -> awaiting LLM review -> needs your decision ->
        needs your decision (forced package) -> READY TO APPLY (target)

    Leads that were never going to clear the LLM-review gate (rule score
    below LLM_REVIEW_GATE_PCT) or that the LLM already said "pass" on are
    deliberately NOT part of this funnel — they're low-priority chaff, not
    something blocking your target action, and get folded into
    `not_prioritized` (rendered as a single small footnote link, not a
    funnel stage). Everything past `package_generated` (pursued/applied/
    interviewing/etc.) is a separate concern — tracking responses on leads
    you've *already* submitted — and lands in `manual_handled` instead,
    unchanged from before."""
    rows = [dict(r) for r in conn.execute(f"SELECT {_LEAD_COLUMNS} FROM job_leads")]

    # Per-company distinct titles across ALL rows/statuses (mirrors
    # store.get_sibling_titles' scope) — computed once here instead of one
    # DB round-trip per row, purely to decide flat-vs-subfolder layout below.
    company_titles: defaultdict[str, set[str]] = defaultdict(set)
    for r in rows:
        company_titles[r["company"]].add(r["title"])

    jd_unresolved: list[dict] = []
    awaiting_llm_review: list[dict] = []
    needs_decision: list[dict] = []
    needs_decision_forced: list[dict] = []
    ready_to_apply: list[dict] = []
    not_prioritized: list[dict] = []
    manual_status: defaultdict[str, Counter] = defaultdict(Counter)

    for r in rows:
        status = r["status"]
        if status not in ("new", "package_generated"):
            manual_status[status][r["company"]] += 1
            continue

        multi_lead = len(company_titles[r["company"]]) > 1
        folder_path, company_folder_path, fc = _lead_folder_and_count(
            output_root, company=r["company"], title=r["title"], multi_lead=multi_lead
        )
        entry = {
            "company": r["company"],
            "title": r["title"],
            "normalizedKey": r["normalized_key"],
            "fileCount": fc,
            "folderPath": folder_path,
            "companyFolderPath": company_folder_path,
            "ageDays": _age_days(r["first_seen"], now),
            "applyUrl": r["apply_url"] or "",
            # Tri-state, preserved as-is (not coerced to bool) so the
            # dashboard can render three distinct, inline-editable states —
            # see models.JobLead.direct_recruiter_outreach's docstring and
            # directRecruiterCellHtml() below: True -> "Yes" (gold),
            # None (not yet reviewed) -> "Undecided" (dim), False
            # (reviewed, confirmed not direct) -> "No" (dim). Lets you see
            # the size of the still-undecided backlog, AND change the
            # decision, directly on the dashboard, without running the
            # interactive review CLI.
            "directRecruiter": (
                None if r["direct_recruiter_outreach"] is None else bool(r["direct_recruiter_outreach"])
            ),
        }

        if status == "package_generated":
            # "Ready to apply" needs proof, not just the DB's claim: both
            # docx files actually present on disk (_has_resume_and_cover).
            # Anything short of that — a non-pursue verdict that got a
            # package anyway via --force, OR a pursue verdict whose files
            # are somehow missing — needs a human decision (submit anyway,
            # regenerate, or discard), so it's "forced", not "ready".
            pct = _fmt_pct(r["llm_match_pct"])
            if r["llm_verdict"] == "pursue" and _has_resume_and_cover(output_root / folder_path):
                ready_to_apply.append({**entry, "matchPct": pct})
            else:
                needs_decision_forced.append({**entry, "matchPct": pct, "verdict": r["llm_verdict"] or "review"})
            continue

        # status == "new" from here down.
        if r["verdict"] == "REVIEW NEEDED":
            jd_unresolved.append(entry)
        elif r["llm_verdict"] in ("review", "pursue"):
            # A full LLM review already ran and came back review (the
            # normal case) or — rarely — pursue but the lead is somehow
            # still stuck at "new" instead of having auto-generated a
            # package (shouldn't happen; surfaced here rather than hidden
            # so a pipeline bug would actually be visible).
            needs_decision.append({**entry, "matchPct": _fmt_pct(r["llm_match_pct"]), "verdict": r["llm_verdict"]})
        elif not r["llm_verdict"] and (r["match_pct"] or 0) >= LLM_REVIEW_GATE_PCT:
            # Cleared the cheap-score gate but the real LLM call hasn't run
            # yet — purely a "wait for the pipeline" (or run it manually)
            # state, not something requiring a judgment call.
            awaiting_llm_review.append({**entry, "matchPct": _fmt_pct(r["match_pct"])})
        else:
            # Either the LLM already said "pass", or the rule-based score
            # never cleared the gate in the first place — not worth
            # spending attention on individually.
            not_prioritized.append(entry)

    manual_handled = [
        {
            "status": status,
            "count": sum(counts.values()),
            "companies": [_company_label(c, n) for c, n in sorted(counts.items())],
        }
        for status, counts in sorted(manual_status.items())
    ]

    # Oldest-first by default everywhere — a lead's value decays with age
    # (see _age_days' docstring), so the thing most in danger of going
    # stale unreviewed belongs at the top, not just the highest-scoring one.
    # The main table remains client-side re-sortable by any column (see the
    # JS below); these server-side orders are just its initial state.
    jd_unresolved.sort(key=lambda l: (-l["ageDays"], l["company"].lower()))
    awaiting_llm_review.sort(key=lambda l: (-l["ageDays"], -l["matchPct"]))
    needs_decision.sort(key=lambda l: (-l["ageDays"], -l["matchPct"]))
    needs_decision_forced.sort(key=lambda l: (-l["ageDays"], -l["matchPct"]))
    ready_to_apply.sort(key=lambda l: (-l["ageDays"], -l["matchPct"]))

    unmatched_communications = [
        {
            "messageId": r["message_id"],
            "direction": r["direction"],
            "fromAddress": r["from_address"] or "",
            "toAddress": r["to_address"] or "",
            "subject": r["subject"] or "(no subject)",
            "preview": (r["body_text"] or "").strip().replace("\n", " ")[:180],
            # Full text too (not just the 180-char preview) — this page is a
            # static file with no live DB access, so the only way to read a
            # message in full from the dashboard itself is to have it already
            # embedded; the table row's "Preview" cell expands to show it
            # (see renderUnmatchedCommunications()/`.preview-cell` below).
            "body": r["body_text"] or "",
            "ageDays": _age_days(r["detected_at"], now),
        }
        for r in list_unmatched_messages(conn)
    ]
    unmatched_communications.sort(key=lambda m: -m["ageDays"])

    _funnel_buckets = (jd_unresolved, awaiting_llm_review, needs_decision, needs_decision_forced, ready_to_apply)
    direct_recruiter_count = sum(1 for bucket in _funnel_buckets for lead in bucket if lead["directRecruiter"])
    # Whole-DB, not just the funnel buckets above — the review queue
    # (review_direct_recruiter_outreach.py) walks every lead regardless of
    # status, so this should match what that command would actually show.
    # Static — only known server-side (leads with e.g. status='applied'
    # aren't loaded into any of the funnel-bucket JS arrays at all, so this
    # number can't be recomputed client-side after an inline edit; see
    # direct_recruiter_undecided_visible_count below for the subset that
    # can).
    direct_recruiter_undecided_count = sum(1 for r in rows if r["direct_recruiter_outreach"] is None)
    # Just the funnel buckets shown in the tables above — every lead here
    # has a live <select> (directRecruiterCellHtml()), so this *can* be
    # recomputed client-side after each inline edit (recomputeDirectRecruiterCounts()
    # below), unlike the whole-DB figure above.
    direct_recruiter_undecided_visible_count = sum(
        1 for bucket in _funnel_buckets for lead in bucket if lead["directRecruiter"] is None
    )

    return {
        "jd_unresolved": jd_unresolved,
        "awaiting_llm_review": awaiting_llm_review,
        "needs_decision": needs_decision,
        "needs_decision_forced": needs_decision_forced,
        "ready_to_apply": ready_to_apply,
        "direct_recruiter_count": direct_recruiter_count,
        "direct_recruiter_undecided_count": direct_recruiter_undecided_count,
        "direct_recruiter_undecided_visible_count": direct_recruiter_undecided_visible_count,
        "not_prioritized_count": len(not_prioritized),
        "manual_handled": manual_handled,
        # scripts/scan_communications.py's parking lot (2026-07-17) — a
        # LinkedIn reply (or Sent-folder message) that couldn't be
        # auto-linked to any tracked job. Deliberately NOT part of the
        # lead funnel above (it's about communications, not leads — a row
        # here might resolve onto a lead already sitting in any funnel
        # stage, or onto a brand-new one that doesn't exist yet), but
        # still surfaced prominently since it's real, actionable signal
        # sitting untracked otherwise. See resolve_communication.py.
        "unmatched_communications": unmatched_communications,
        "total_leads": len(rows),
        "generated_at": now,
    }


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Pending job-tracker actions</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --border: #2a2e37;
    --text: #e6e8ec;
    --text-secondary: #9aa0ac;
    --text-tertiary: #6b7280;
    --warning: #d9a441;
    --danger: #d9534f;
    --success: #4caf7d;
    --info: #4a90d9;
    --accent: #6c7ee1;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    padding: 32px;
  }
  .wrap { max-width: 1100px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .subtitle { color: var(--text-secondary); font-size: 13px; margin-bottom: 24px; }
  .subtitle code { color: var(--text); background: var(--panel); padding: 1px 5px; border-radius: 4px; }
  .funnel-caption { font-size: 12px; color: var(--text-tertiary); margin-bottom: 8px; }
  .funnel { display: flex; align-items: stretch; gap: 0; margin-bottom: 8px; overflow-x: auto; }
  .funnel-box {
    flex: 1 1 0;
    min-width: 140px;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 14px;
    background: var(--panel);
    cursor: pointer;
    transition: border-color 0.15s;
  }
  .funnel-box:hover { border-color: var(--accent); }
  .funnel-box .value { font-size: 26px; font-weight: 700; }
  .funnel-box .label { font-size: 11.5px; color: var(--text-secondary); margin-top: 4px; line-height: 1.35; }
  .funnel-box.target { border: 2px solid var(--success); background: rgba(76,175,125,0.08); }
  .funnel-box.target .value { color: var(--success); }
  .funnel-box.blocker .value { color: var(--warning); }
  .funnel-box.blocker-far .value { color: var(--danger); }
  .funnel-arrow {
    flex: 0 0 auto;
    display: flex;
    align-items: center;
    padding: 0 6px;
    color: var(--text-tertiary);
    font-size: 16px;
  }
  .funnel-note { font-size: 12px; color: var(--text-tertiary); margin: 4px 0 20px; }
  .funnel-note a { color: var(--info); cursor: pointer; text-decoration: underline; }
  .callout {
    border: 1px solid var(--border);
    border-left: 3px solid var(--info);
    background: var(--panel);
    border-radius: 6px;
    padding: 12px 14px;
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 20px;
  }
  .callout.flag { border-left-color: var(--warning); }
  .callout .title { color: var(--text); font-weight: 600; margin-bottom: 4px; font-size: 13px; }
  h2 { font-size: 15px; margin: 0 0 12px; display: flex; align-items: center; justify-content: space-between; }
  .pills { display: flex; gap: 6px; }
  .pill {
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-secondary);
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    cursor: pointer;
  }
  .pill.active { background: var(--accent); color: white; border-color: var(--accent); }
  input[type="text"] {
    width: 320px;
    padding: 7px 10px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--text);
    font-size: 13px;
    margin-bottom: 12px;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  thead th {
    text-align: left;
    color: var(--text-secondary);
    font-weight: 500;
    border-bottom: 1px solid var(--border);
    padding: 8px 10px;
    position: sticky;
    top: 0;
    background: var(--bg);
  }
  th.num, td.num { text-align: right; }
  thead th[data-sort] { cursor: pointer; user-select: none; }
  thead th[data-sort]:hover { color: var(--text); }
  thead th[data-sort].sorted { color: var(--text); }
  thead th[data-sort] .arrow { color: var(--accent); margin-left: 3px; }
  td.age { color: var(--text-secondary); }
  td.age.stale { color: var(--warning); font-weight: 600; }
  tbody tr { border-bottom: 1px solid var(--border); }
  tbody tr:nth-child(odd) { background: rgba(255,255,255,0.02); }
  tbody tr.high { background: rgba(74,144,217,0.08); }
  tbody tr.pursue { background: rgba(217,83,79,0.10); }
  td { padding: 8px 10px; vertical-align: middle; }
  td.company { font-weight: 600; }
  .direct-cell { text-align: center; padding-left: 4px; padding-right: 4px; }
  .direct-select {
    font-size: 12px;
    background: transparent;
    color: var(--text-secondary);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 3px;
    cursor: pointer;
    max-width: 62px;
  }
  .direct-select option { background: var(--panel); color: var(--text); }
  /* Yes is deliberately the loudest of the three states (2026-07-21) — a
     confirmed direct-recruiter lead is worth catching at a glance while
     scanning down a table, so it gets a solid filled badge instead of just
     a colored outline; Undecided and No both stay muted/outline-only since
     neither needs to draw the eye. */
  .direct-select-undecided { color: var(--text-tertiary); opacity: 0.6; border-color: var(--border); }
  .direct-select-yes {
    color: #241900;
    background: var(--warning);
    border-color: var(--warning);
    font-weight: 700;
    opacity: 1;
  }
  .direct-select-no { color: var(--text-tertiary); opacity: 0.85; border-color: var(--border); }
  td.title { color: var(--text-secondary); }
  .table-scroll { max-height: 520px; overflow-y: auto; border: 1px solid var(--border); border-radius: 8px; }
  .table-scroll.short { max-height: 340px; }
  .copy-btn {
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 12px;
    cursor: pointer;
  }
  .copy-btn:hover { border-color: var(--accent); }
  .copy-btn.copied { color: var(--success); border-color: var(--success); }
  .apply-btn {
    display: inline-block;
    border: 1px solid var(--success);
    background: rgba(76,175,125,0.08);
    color: var(--success);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 12px;
    text-decoration: none;
    white-space: nowrap;
  }
  .apply-btn:hover { background: rgba(76,175,125,0.18); }
  .apply-btn-disabled {
    display: inline-block;
    border: 1px solid var(--border);
    color: var(--text-tertiary);
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 12px;
    white-space: nowrap;
    cursor: default;
  }
  .hint { font-size: 12px; color: var(--text-tertiary); margin-top: 10px; }
  .divider { border: none; border-top: 1px solid var(--border); margin: 28px 0; }
  /* Overrides the card-level `summary`/`details` rules below for the
     inline "click to read the full message" toggle in the unmatched-
     communications table — those are sized for a whole collapsible card,
     not one table cell. */
  .preview-cell details { border: none; background: transparent; margin: 0; }
  .preview-cell summary {
    padding: 0;
    display: list-item;
    list-style: revert;
    cursor: pointer;
    color: var(--text);
    font-size: 13px;
  }
  .preview-cell summary::marker { color: var(--info); }
  .preview-cell .preview-full {
    margin-top: 8px;
    padding: 10px 12px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--text-secondary);
    max-height: 360px;
    overflow-y: auto;
  }
  .preview-cell .preview-full strong { color: var(--text); font-weight: 600; }
  .preview-cell .preview-body { white-space: pre-wrap; margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border); }
  details { border: 1px solid var(--border); border-radius: 8px; background: var(--panel); margin-bottom: 12px; }
  summary { padding: 12px 14px; cursor: pointer; font-size: 14px; display: flex; align-items: center; justify-content: space-between; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  /* Row-level pill (table 3's "Priority" column: High/Medium/Low) — kept
     as the original muted outline style. Distinct from
     `.header-count-pill` below, which is what section-header row counts
     use; the two happened to share one class/name before 2026-07-21, but
     giving section headers a filled blue look would have also (wrongly)
     recolored every row's Priority label. */
  .count-pill { border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 12px; color: var(--text-secondary); }
  /* Filled blue, matching the active "All (N)" priority-filter pill on
     table 3 (`.pill.active` above) — every section-header count now uses
     the same loud, consistent style rather than #3's alone standing out
     as the only filled pill on the page (2026-07-21). */
  .header-count-pill { border: 1px solid var(--accent); border-radius: 999px; padding: 2px 8px; font-size: 12px; background: var(--accent); color: white; font-weight: 600; }
  .verdict-badge { border-radius: 999px; padding: 1px 8px; font-size: 11px; }
  .verdict-badge.pursue { color: var(--danger); border: 1px solid var(--danger); }
  .verdict-badge.review { color: var(--text-secondary); border: 1px solid var(--border); }
  .card-body { padding: 0 14px 14px; }
  .manual-row { display: flex; gap: 8px; align-items: baseline; padding: 6px 0; }
  .manual-status { border: 1px solid var(--border); border-radius: 999px; padding: 1px 8px; font-size: 11px; color: var(--text-secondary); white-space: nowrap; }
  .footer-note { font-size: 11px; color: var(--text-tertiary); margin-top: 32px; }
  .company-link, .title-link { color: var(--text); text-decoration: none; }
  .company-link:hover, .title-link:hover { text-decoration: underline; color: var(--info); }
  .file-count { color: var(--text-tertiary); font-weight: 400; font-size: 11px; margin-left: 4px; }
  .page-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 8px; }
  .page-header h1 { margin: 0; }
  .header-actions { display: flex; align-items: center; gap: 14px; flex-shrink: 0; }
  .auto-refresh-label {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    color: var(--text-secondary);
    cursor: pointer;
    white-space: nowrap;
  }
  .auto-refresh-label input { cursor: pointer; }
  .auto-refresh-status { color: var(--text-tertiary); font-variant-numeric: tabular-nums; }
  .regen-btn {
    flex-shrink: 0;
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--text);
    padding: 7px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-family: inherit;
    text-decoration: none;
    white-space: nowrap;
    cursor: pointer;
  }
  .regen-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--info); }
  .regen-btn:disabled { color: var(--text-tertiary); cursor: default; }
  .regen-spinner {
    flex-shrink: 0;
    width: 16px;
    height: 16px;
    border: 2px solid var(--border);
    border-top-color: var(--info);
    border-radius: 50%;
    animation: regen-spin 0.7s linear infinite;
  }
  .regen-spinner[hidden] { display: none; }
  @keyframes regen-spin {
    to { transform: rotate(360deg); }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="page-header">
    <h1>Pending job-tracker actions</h1>
    <div class="header-actions">
      <label class="auto-refresh-label"
             title="Reloads this tab from disk on an interval to pick up the hourly recruiting-automation run automatically. Free — never triggers a re-render or rescore, just re-reads whatever's already on disk.">
        <input type="checkbox" id="auto-refresh-toggle" />
        Auto-refresh <span class="auto-refresh-status" id="auto-refresh-status"></span>
      </label>
      <span class="regen-spinner" id="regen-spinner" hidden aria-hidden="true"></span>
      <button class="regen-btn" id="regen-btn"
         title="Re-run scripts/render_pending_actions.py (via local RefreshPending helper), then reload this same tab">Regenerate page</button>
    </div>
  </div>
  <div class="subtitle">
    Live snapshot of <code>leads.db</code>, regenerated ${GENERATED_AT} via
    <code>scripts/render_pending_actions.py</code>.<br/>
    Static snapshot &mdash; not live-synced. Use <strong>Regenerate page</strong> (or re-run that script) after further changes,
    or leave <strong>Auto-refresh</strong> on to pick up the hourly automation run on its own.
  </div>

  <div class="funnel-caption">
    <strong>Ready to apply</strong> (target) is on the far left. Boxes 2-5 are things currently blocking
    leads from getting there; box 6 (unmatched communications) is a separate signal &mdash; a recruiter
    reply that couldn't be auto-linked to any lead yet. Click any box to jump to its list below.
  </div>
  <div class="funnel" id="funnel"></div>
  <div class="funnel-note" id="funnel-note"></div>

  <hr class="divider" />

  <details open id="section-ready-to-apply">
    <summary>1. Ready to apply &mdash; docs generated, nothing done with it yet <span class="header-count-pill" id="ready-to-apply-count"></span></summary>
    <div class="card-body">
      <div class="table-scroll short">
        <table>
          <thead><tr><th>Company</th><th>Title</th><th class="num">Match %</th><th class="num">Age (days)</th><th></th><th>Apply</th></tr></thead>
          <tbody id="ready-to-apply-body"></tbody>
        </table>
      </div>
      <div class="hint">
        Full-LLM-review verdict is <strong>PURSUE</strong>, status is still <code>package_generated</code>
        (not yet <code>pursued</code>/<code>applied</code>/<code>skipped</code>/<code>rejected</code>),
        and both a r&eacute;sum&eacute; and cover letter are confirmed present on disk &mdash; not just
        claimed by the DB status. <strong>Action: click Apply to open the posting in a new tab, submit
        the application, then advance its status.</strong> "No link" means no apply URL was ever
        captured for that lead &mdash; check its JobDescription.docx or go find the posting manually.
      </div>
    </div>
  </details>

  <details open id="section-needs-decision-forced">
    <summary>2. Needs your decision &mdash; package already generated on a non-PURSUE verdict <span class="header-count-pill" id="needs-decision-forced-count"></span></summary>
    <div class="card-body">
      <div class="table-scroll short">
        <table>
          <thead><tr><th>Company</th><th>Title</th><th>Verdict</th><th class="num">Match %</th><th class="num">Age (days)</th><th></th></tr></thead>
          <tbody id="needs-decision-forced-body"></tbody>
        </table>
      </div>
      <div class="hint">
        Someone (you, in an earlier session) ran <code>apply_package.py --force</code> on these despite
        the LLM saying "review" (or missing entirely), so documents already exist. <strong>Action: read
        the stored review, then either submit anyway (and it'll behave like #1) or set status to
        <code>skipped</code> to drop it.</strong>
      </div>
    </div>
  </details>

  <details open id="section-needs-decision">
    <summary>
      <span id="table-heading">3. Needs your decision &mdash; full-LLM-review says "review"</span>
      <span class="pills" id="priority-pills"></span>
    </summary>
    <div class="card-body">
      <input type="text" id="search" placeholder="Filter by company or title&hellip;" />
      <div class="table-scroll">
        <table>
          <thead>
            <tr id="table-header-row">
              <th data-sort="company">Company</th>
              <th data-sort="title">Title</th>
              <th class="num" data-sort="matchPct">Match %</th>
              <th data-sort="verdict">Verdict</th>
              <th class="num" data-sort="ageDays">Age (days)</th>
              <th></th>
              <th>Priority</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody id="table-body"></tbody>
        </table>
      </div>
      <div class="hint">
        A real LLM review already ran and came back ambiguous &mdash; <strong>your decision, not the
        pipeline's</strong>: pursue it (which auto-generates the package and moves it to #1) or pass (set
        status to <code>skipped</code>). Rows shaded red are the rare case the LLM said PURSUE but it's
        somehow still stuck here instead of already having a package &mdash; worth checking why. Sorted
        oldest-first by default &mdash; click any column header to re-sort, click again to reverse. Age
        turns amber at ${STALE_DAYS_THRESHOLD}+ days. "Copy prompt" copies a ready-to-paste request for a
        new Cursor chat, pre-loaded with that company/title, so the agent can pull its full stored review
        and act on your decision for you.
      </div>
    </div>
  </details>

  <hr class="divider" />

  <details id="section-awaiting-llm-review">
    <summary>4. Awaiting full-LLM-review &mdash; cleared the score gate, real review hasn't run yet <span class="header-count-pill" id="awaiting-llm-review-count"></span></summary>
    <div class="card-body">
      <div class="table-scroll short">
        <table>
          <thead><tr><th>Company</th><th>Title</th><th class="num">Match %</th><th class="num">Age (days)</th><th></th></tr></thead>
          <tbody id="awaiting-llm-review-body"></tbody>
        </table>
      </div>
      <div class="hint">
        Rule-based score already cleared ${LLM_REVIEW_GATE_PCT}% (the cost gate for spending a real LLM
        call &mdash; see <code>config/framework.yaml</code>'s <code>llm_review_min_pct</code>), but the
        automated pipeline hasn't evaluated it yet. <strong>Action: nothing manual required &mdash; the
        next hourly cycle picks these up &mdash; or run <code>triage_recruiter_inbox.py</code> yourself
        to force it now.</strong>
      </div>
    </div>
  </details>

  <details open id="section-jd-unresolved">
    <summary>5. JD unresolved &mdash; no usable job-description text yet <span class="header-count-pill" id="jd-unresolved-count"></span></summary>
    <div class="card-body">
      <div class="table-scroll short">
        <table>
          <thead><tr><th>Company</th><th>Title</th><th class="num">Age (days)</th><th></th></tr></thead>
          <tbody id="jd-unresolved-body"></tbody>
        </table>
      </div>
      <div class="hint">
        Link-following and a company-careers-page search both failed to turn up a full JD (2026-07-11
        policy &mdash; see <code>~/CLAUDE.md</code> &sect;11 / PRIMER.md). <strong>Action: go find and
        paste in the real posting text</strong> &mdash; nothing downstream can happen without it.
      </div>
    </div>
  </details>

  <div class="funnel-note" id="not-prioritized-note"></div>

  <hr class="divider" />

  <details id="section-unmatched-communications">
    <summary>6. Unmatched communications &mdash; couldn't auto-link to a tracked job <span class="header-count-pill" id="unmatched-communications-count"></span></summary>
    <div class="card-body">
      <div class="table-scroll short">
        <table>
          <thead><tr><th>Direction</th><th>Subject</th><th>From / To</th><th>Preview</th><th class="num">Age (days)</th></tr></thead>
          <tbody id="unmatched-communications-body"></tbody>
        </table>
      </div>
      <div class="hint">
        Found by <code>scripts/scan_communications.py</code> (LinkedIn message replies routed to
        <code>Category/social</code> that <code>triage_recruiter_inbox.py</code> never sees, plus optionally
        your own Sent-folder replies) but couldn't be matched to any job by thread id, known contact
        address, or (if <code>--llm-fallback</code> was used) an LLM-extracted company/title.
        <strong>Action:</strong> run <code>python3 scripts/resolve_communication.py --list</code> to see the
        full text, then <code>resolve_communication.py --message-id &lt;id&gt; --company "&hellip;" --title
        "&hellip;"</code> (add <code>--create</code> if it's a genuinely new lead with no JD yet).
      </div>
    </div>
  </details>

  <hr class="divider" />

  <details>
    <summary>Tracking submitted applications &mdash; already past "package generated"</summary>
    <div class="card-body">
      <div class="hint" style="margin-top:0;">
        Not part of the funnel above &mdash; these already got submitted (or otherwise resolved) at some
        point. Kept here purely for follow-up tracking (who's waiting on a response, who's mid-interview),
        not because anything needs to happen to get them "ready."
      </div>
      <div class="card-body" id="manual-handled" style="padding-left:0; padding-right:0;"></div>
    </div>
  </details>

  <div class="footer-note">${FOOTER_NOTE}</div>
</div>

<script>
const READY_TO_APPLY = ${READY_TO_APPLY_JSON};
const NEEDS_DECISION_FORCED = ${NEEDS_DECISION_FORCED_JSON};
const NEEDS_DECISION = ${NEEDS_DECISION_JSON};
const AWAITING_LLM_REVIEW = ${AWAITING_LLM_REVIEW_JSON};
const JD_UNRESOLVED = ${JD_UNRESOLVED_JSON};
const NOT_PRIORITIZED_COUNT = ${NOT_PRIORITIZED_COUNT_JSON};
const MANUAL_HANDLED = ${MANUAL_HANDLED_JSON};
const UNMATCHED_COMMUNICATIONS = ${UNMATCHED_COMMUNICATIONS_JSON};

// PENDING_REVIEW kept as the name of the main filterable table's backing
// array (section 3, "Needs your decision") purely so the rest of this
// script's table/sort/filter/copy-prompt logic below didn't need renaming
// throughout — it's NEEDS_DECISION under the hood.
const PENDING_REVIEW = NEEDS_DECISION;

function priorityOf(pct) {
  if (pct >= 50) return "high";
  if (pct >= 35) return "medium";
  return "low";
}
const PRIORITY_LABEL = { high: "High (\u226550%)", medium: "Medium (35\u201349%)", low: "Low (<35%)" };

let query = "";
let priorityFilter = "all";
// Default sort: oldest first — see _age_days' docstring in
// render_pending_actions.py for why age, not just match %, drives the
// default ordering. Click any column header to re-sort by that instead.
let sortKey = "ageDays";
let sortDir = "desc";
const STALE_DAYS = ${STALE_DAYS_THRESHOLD};
// How long to wait after firing refreshpending://run before reloading this
// same tab in place (see regen-btn's click handler below). render() scales
// with total_leads (mostly the --no-rescore-skippable rule-based rescore of
// every status='new' lead), so this is recomputed from the CURRENT lead
// count every time the page is regenerated rather than a value that would
// quietly go stale as the DB grows.
const REGEN_DELAY_MS = ${REGEN_DELAY_MS_JSON};

function ageCellHtml(days) {
  const cls = days >= STALE_DAYS ? "age stale" : "age";
  return `<td class="num ${cls}">${days}</td>`;
}

function compareBy(key, dir) {
  const sign = dir === "asc" ? 1 : -1;
  return (a, b) => {
    const av = a[key], bv = b[key];
    if (typeof av === "string" || typeof bv === "string") {
      return sign * String(av).localeCompare(String(bv));
    }
    return sign * ((av ?? 0) - (bv ?? 0));
  };
}

function reviewPrompt(lead) {
  return `Show me the full stored JD-match review for "${lead.company}" / "${lead.title}" ` +
    `(python3 scripts/list_leads.py --company "${lead.company}" --title "${lead.title}" ` +
    `--show-review), then help me decide whether to pursue it. If I decide to pursue, ` +
    `generate the r\u00e9sum\u00e9 + cover letter with --force (the stored verdict is "${lead.verdict}", ` +
    `so apply_package.py needs --force unless it's already "pursue"). If I decide to ` +
    `pass, set its status to skipped instead.`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// folderPath / companyFolderPath are precomputed server-side per LEAD —
// see render_pending_actions._lead_folder_and_count, which mirrors
// job_tracker.pipeline.llm_apply._safe_filename()/_job_folder()'s naming
// rules: a single-lead company's files sit flat in <Company>/, a
// multi-lead company's land in <Company>/<Company>_<Title>/ instead.
// The company-name link opens the shared <Company>/ root; the title link
// opens THIS lead's own package folder. File count is scoped to the
// package folder alone.
const FOLDER_ROOT = "${FOLDER_ROOT}";
// Opens a package folder in Finder via the local RevealFolder helper
// (tools/reveal-folder/) — browsers cannot open Finder from a static
// page with file:// alone. Install once: tools/reveal-folder/install.sh
function folderUrl(folderPath) {
  const abs = `${FOLDER_ROOT}/${folderPath}`.replace(/\/+/g, "/");
  return `revealfolder://reveal?path=${encodeURIComponent(abs)}`;
}
function companyCellHtml(company, companyFolderPath) {
  return `<a class="company-link" href="${folderUrl(companyFolderPath)}" title="Open company folder in Finder">${escapeHtml(company)}</a>`;
}

// Tri-state `directRecruiter` selector (2026-07-21, moved to its own column
// same day, made tri-state 2026-07-21, made inline-editable 2026-07-21) —
// see models.JobLead.direct_recruiter_outreach's docstring for exactly how
// the value is decided. "yes" -> filled gold star (confirmed a real
// recruiter personally reached out); "undecided" -> empty outline star (not
// yet reviewed); "no" -> a muted dash (reviewed, confirmed NOT direct
// outreach). A plain <select> rather than a click-to-cycle badge, so the
// three states are always visible/discoverable and there's no ambiguity
// about what a bare click would do. Picking a new option immediately fires
// setDirectRecruiterOutreach() below — no separate "save" step. Lives in
// its own unlabeled column just after "Age (days)" rather than inline with
// the company name, so it reads as a distinct signal instead of decorating
// the link.
function directRecruiterCellHtml(directRecruiter, normalizedKey) {
  const value = directRecruiter === true ? "yes" : directRecruiter === false ? "no" : "undecided";
  const opt = (v, glyph, label) =>
    `<option value="${v}"${v === value ? " selected" : ""}>${glyph} ${label}</option>`;
  return `<td class="direct-cell">
    <select class="direct-select direct-select-${value}" title="direct_recruiter_outreach"
      onchange="setDirectRecruiterOutreach('${normalizedKey}', this.value, this)">
      ${opt("undecided", "\u2606", "Undecided")}
      ${opt("yes", "\u2B50", "Yes")}
      ${opt("no", "\u2014", "No")}
    </select>
  </td>`;
}

// Every lead with a live <select> lives in exactly one of these 5 arrays —
// used both by setDirectRecruiterOutreach() (to update the in-memory
// record a click just changed) and recomputeDirectRecruiterCounts() (to
// re-tally from them). Declared once here rather than re-listed in both.
const DIRECT_RECRUITER_BUCKETS = [JD_UNRESOLVED, AWAITING_LLM_REVIEW, NEEDS_DECISION, NEEDS_DECISION_FORCED, READY_TO_APPLY];

// Live re-tally of the two footer sub-counts that CAN be known purely from
// what's already loaded into the browser (compare direct_recruiter_
// undecided_count in the footer text, which is whole-DB and therefore
// server-only — see render()'s comment). Called after every inline edit so
// the footer reflects your choices instantly instead of going stale until
// the next full regenerate.
function recomputeDirectRecruiterCounts() {
  let yesCount = 0;
  let undecidedCount = 0;
  for (const bucket of DIRECT_RECRUITER_BUCKETS) {
    for (const lead of bucket) {
      if (lead.directRecruiter === true) yesCount++;
      else if (lead.directRecruiter === null || lead.directRecruiter === undefined) undecidedCount++;
    }
  }
  const yesEl = document.getElementById("direct-recruiter-count");
  const undecidedEl = document.getElementById("direct-recruiter-undecided-visible-count");
  if (yesEl) yesEl.textContent = String(yesCount);
  if (undecidedEl) undecidedEl.textContent = String(undecidedCount);
}

// Fires the setdro:// custom URL scheme (tools/set-direct-recruiter-
// outreach/main.swift), which shells out to
// `set-direct-recruiter-outreach --key ... --value ...` to persist the
// change to leads.db. Fire-and-forget, like refreshpending://'s regen
// button — a static file:// page can't get a return value back from the
// helper app, so this just applies the new selected-state CSS class and
// the live footer re-tally immediately (optimistic UI), trusting the
// helper's own NSAlert to surface a failure (e.g. a locked DB). The
// whole-DB undecided figure in the footer text still only refreshes on a
// full regenerate — see recomputeDirectRecruiterCounts()'s comment.
function setDirectRecruiterOutreach(normalizedKey, value, selectEl) {
  selectEl.className = "direct-select direct-select-" + value;
  const newValue = value === "yes" ? true : value === "no" ? false : null;
  for (const bucket of DIRECT_RECRUITER_BUCKETS) {
    const lead = bucket.find(l => l.normalizedKey === normalizedKey);
    if (lead) {
      lead.directRecruiter = newValue;
      break;
    }
  }
  recomputeDirectRecruiterCounts();
  window.location.href =
    "setdro://set?key=" + encodeURIComponent(normalizedKey) + "&value=" + encodeURIComponent(value);
}
function titleCellHtml(title, folderPath, fileCount) {
  const countSuffix = fileCount > 0 ? `<span class="file-count">(${fileCount} file${fileCount === 1 ? "" : "s"})</span>` : "";
  return `<a class="title-link" href="${folderUrl(folderPath)}" title="Open this role's folder in Finder">${escapeHtml(title)}</a>${countSuffix}`;
}

// "Apply for this job-lead" (2026-07-19) — applyUrl comes straight from the
// lead's stored `apply_url` column (see _LEAD_COLUMNS/render()'s entry
// dict). Plain <a target="_blank"> rather than a JS window.open() click
// handler: it degrades gracefully with JS disabled, isn't subject to
// popup-blocker heuristics, and still supports native middle-click/
// right-click "open in new window" — the standard way to say "don't
// navigate away from this dashboard tab" from a static page. Renders a
// disabled-looking pill instead when no apply_url was ever captured for
// this lead, rather than silently omitting the column.
function applyButtonHtml(applyUrl) {
  if (!applyUrl) {
    return `<span class="apply-btn-disabled" title="No apply URL captured for this lead">No link</span>`;
  }
  return `<a class="apply-btn" href="${escapeHtml(applyUrl)}" target="_blank" rel="noopener noreferrer" ` +
    `title="Opens the application page in a new browser tab/window">Apply \u2197</a>`;
}

// Left-to-right = target-to-farthest-blocker, matching the funnel-caption
// copy above and the numbered section headings below (1-5 are all now
// <details>/<summary> cards — #3 joined the other four on 2026-07-21 so
// clicking its title also collapses the table, matching 1/2/4/5). #6
// (unmatched communications) is deliberately tacked on at the far end
// rather than woven into that ordering: it's not a *lead* funnel stage
// (see render()'s docstring) — a parked message might resolve onto a lead
// already sitting in any of the other 5 boxes, or onto a brand-new one —
// it's just surfaced here too since it's real, actionable signal.
const FUNNEL_STEPS = [
  { count: () => READY_TO_APPLY.length, label: "Ready to apply", cls: "target", sectionId: "section-ready-to-apply" },
  { count: () => NEEDS_DECISION_FORCED.length, label: "Needs decision (forced package)", cls: "blocker", sectionId: "section-needs-decision-forced" },
  { count: () => NEEDS_DECISION.length, label: "Needs your decision", cls: "blocker", sectionId: "section-needs-decision" },
  { count: () => AWAITING_LLM_REVIEW.length, label: "Awaiting full-LLM-review", cls: "blocker", sectionId: "section-awaiting-llm-review" },
  { count: () => JD_UNRESOLVED.length, label: "JD unresolved", cls: "blocker-far", sectionId: "section-jd-unresolved" },
  { count: () => UNMATCHED_COMMUNICATIONS.length, label: "Unmatched communications", cls: "blocker-far", sectionId: "section-unmatched-communications" },
];

function renderFunnel() {
  const el = document.getElementById("funnel");
  el.innerHTML = FUNNEL_STEPS.map((step, idx) => {
    const box = `<div class="funnel-box ${step.cls}" data-idx="${idx}">
      <div class="value">${step.count()}</div>
      <div class="label">${step.label}</div>
    </div>`;
    return idx === 0 ? box : `<div class="funnel-arrow">&larr;</div>${box}`;
  }).join("");
  el.querySelectorAll(".funnel-box").forEach(box => {
    box.addEventListener("click", () => {
      const step = FUNNEL_STEPS[Number(box.dataset.idx)];
      const targetId = step.sectionId || "table-heading";
      const target = document.getElementById(targetId);
      if (!target) return;
      if (target.tagName === "DETAILS") target.open = true;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  document.getElementById("funnel-note").innerHTML =
    `Not shown above: <strong>${NOT_PRIORITIZED_COUNT}</strong> low-score/already-"pass" leads that ` +
    `were never going to clear the LLM-review gate &mdash; not blocking anything, just not worth ` +
    `individual attention. <a id="not-prioritized-link">Why these aren't shown &rarr;</a>`;
  document.getElementById("not-prioritized-link").addEventListener("click", () => {
    document.getElementById("not-prioritized-note").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  document.getElementById("not-prioritized-note").innerHTML =
    `${NOT_PRIORITIZED_COUNT} leads omitted here: either the full-LLM-review already said "pass," or ` +
    `the free rule-based score never cleared the ${LLM_REVIEW_GATE_PCT}% gate that decides whether ` +
    `a real LLM call is even worth spending on it. Use <code>list_leads.py --verdict pass</code> if you ` +
    `ever want the full list.`;
}

function renderPills() {
  const counts = {
    high: PENDING_REVIEW.filter(l => priorityOf(l.matchPct) === "high").length,
    medium: PENDING_REVIEW.filter(l => priorityOf(l.matchPct) === "medium").length,
    low: PENDING_REVIEW.filter(l => priorityOf(l.matchPct) === "low").length,
  };
  const defs = [
    { key: "all", label: `All (${PENDING_REVIEW.length})` },
    { key: "high", label: `High (${counts.high})` },
    { key: "medium", label: `Medium (${counts.medium})` },
    { key: "low", label: `Low (${counts.low})` },
  ];
  const el = document.getElementById("priority-pills");
  el.innerHTML = defs.map(d => `<button class="pill ${priorityFilter === d.key ? "active" : ""}" data-key="${d.key}">${d.label}</button>`).join("");
  el.querySelectorAll(".pill").forEach(btn => {
    btn.addEventListener("click", () => { priorityFilter = btn.dataset.key; renderPills(); renderTable(); });
  });
}

function renderTableHeaderSortState() {
  document.querySelectorAll("#table-header-row th[data-sort]").forEach(th => {
    const key = th.dataset.sort;
    const arrow = th.querySelector(".arrow");
    if (arrow) arrow.remove();
    th.classList.toggle("sorted", key === sortKey);
    if (key === sortKey) {
      th.insertAdjacentHTML("beforeend", `<span class="arrow">${sortDir === "asc" ? "\u25b2" : "\u25bc"}</span>`);
    }
  });
}

function renderTable() {
  const q = query.trim().toLowerCase();
  const filtered = PENDING_REVIEW
    .filter(l => (priorityFilter === "all" || priorityOf(l.matchPct) === priorityFilter))
    .filter(l => !q || l.company.toLowerCase().includes(q) || l.title.toLowerCase().includes(q))
    .sort(compareBy(sortKey, sortDir));

  document.getElementById("table-heading").textContent = `3. Needs your decision (${filtered.length} of ${PENDING_REVIEW.length})`;
  renderTableHeaderSortState();

  const body = document.getElementById("table-body");
  body.innerHTML = filtered.map((lead, idx) => `
    <tr class="${lead.verdict === "pursue" ? "pursue" : (priorityOf(lead.matchPct) === "high" ? "high" : "")}">
      <td class="company">${companyCellHtml(lead.company, lead.companyFolderPath)}</td>
      <td class="title">${titleCellHtml(lead.title, lead.folderPath, lead.fileCount)}</td>
      <td class="num">${lead.matchPct}%</td>
      <td><span class="verdict-badge ${lead.verdict}">${lead.verdict.toUpperCase()}</span></td>
      ${ageCellHtml(lead.ageDays)}
      ${directRecruiterCellHtml(lead.directRecruiter, lead.normalizedKey)}
      <td><span class="count-pill">${PRIORITY_LABEL[priorityOf(lead.matchPct)]}</span></td>
      <td><button class="copy-btn" data-idx="${idx}">Copy prompt</button></td>
    </tr>`).join("");

  body.querySelectorAll(".copy-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const lead = filtered[Number(btn.dataset.idx)];
      const text = reviewPrompt(lead);
      navigator.clipboard.writeText(text).then(() => {
        btn.textContent = "Copied";
        btn.classList.add("copied");
        setTimeout(() => { btn.textContent = "Copy prompt"; btn.classList.remove("copied"); }, 1500);
      });
    });
  });
}

document.querySelectorAll("#table-header-row th[data-sort]").forEach(th => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (sortKey === key) {
      sortDir = sortDir === "asc" ? "desc" : "asc";
    } else {
      sortKey = key;
      sortDir = key === "company" || key === "title" || key === "verdict" ? "asc" : "desc";
    }
    renderTable();
  });
});

function renderJdUnresolved() {
  document.getElementById("jd-unresolved-count").textContent = JD_UNRESOLVED.length;
  document.getElementById("jd-unresolved-body").innerHTML = JD_UNRESOLVED.map(l => `
    <tr>
      <td class="company">${companyCellHtml(l.company, l.companyFolderPath)}</td>
      <td class="title">${titleCellHtml(l.title, l.folderPath, l.fileCount)}</td>
      ${ageCellHtml(l.ageDays)}
      ${directRecruiterCellHtml(l.directRecruiter, l.normalizedKey)}
    </tr>`).join("");
}

function renderAwaitingLlmReview() {
  document.getElementById("awaiting-llm-review-count").textContent = AWAITING_LLM_REVIEW.length;
  document.getElementById("awaiting-llm-review-body").innerHTML = AWAITING_LLM_REVIEW.map(l => `
    <tr>
      <td class="company">${companyCellHtml(l.company, l.companyFolderPath)}</td>
      <td class="title">${titleCellHtml(l.title, l.folderPath, l.fileCount)}</td>
      <td class="num">${l.matchPct}%</td>
      ${ageCellHtml(l.ageDays)}
      ${directRecruiterCellHtml(l.directRecruiter, l.normalizedKey)}
    </tr>`).join("");
}

function renderNeedsDecisionForced() {
  document.getElementById("needs-decision-forced-count").textContent = NEEDS_DECISION_FORCED.length;
  document.getElementById("needs-decision-forced-body").innerHTML = NEEDS_DECISION_FORCED.map(l => `
    <tr>
      <td class="company">${companyCellHtml(l.company, l.companyFolderPath)}</td>
      <td class="title">${titleCellHtml(l.title, l.folderPath, l.fileCount)}</td>
      <td><span class="verdict-badge ${l.verdict}">${l.verdict.toUpperCase()}</span></td>
      <td class="num">${l.matchPct}%</td>
      ${ageCellHtml(l.ageDays)}
      ${directRecruiterCellHtml(l.directRecruiter, l.normalizedKey)}
    </tr>`).join("");
}

function renderReadyToApply() {
  document.getElementById("ready-to-apply-count").textContent = READY_TO_APPLY.length;
  document.getElementById("ready-to-apply-body").innerHTML = READY_TO_APPLY.map(l => `
    <tr>
      <td class="company">${companyCellHtml(l.company, l.companyFolderPath)}</td>
      <td class="title">${titleCellHtml(l.title, l.folderPath, l.fileCount)}</td>
      <td class="num">${l.matchPct}%</td>
      ${ageCellHtml(l.ageDays)}
      ${directRecruiterCellHtml(l.directRecruiter, l.normalizedKey)}
      <td>${applyButtonHtml(l.applyUrl)}</td>
    </tr>`).join("");
}

function renderUnmatchedCommunications() {
  document.getElementById("unmatched-communications-count").textContent = UNMATCHED_COMMUNICATIONS.length;
  document.getElementById("unmatched-communications-body").innerHTML = UNMATCHED_COMMUNICATIONS.map(m => {
    // The 180-char preview is all that fits in a table cell; click it to
    // expand the full stored body inline (no live DB access from this
    // static page, so the full text has to already be embedded — see
    // render_pending_actions.render()'s "body" field). The expanded block
    // repeats From/To/Subject above the body so it's self-contained —
    // readable on its own without having to look back at the row's other
    // columns (which can also be truncated/off-screen on a narrow window).
    const hasMore = (m.body || "").length > m.preview.length;
    const headerLine = (label, value) => value ? `<div><strong>${label}:</strong> ${escapeHtml(value)}</div>` : "";
    const fullBlock = hasMore
      ? `<div class="preview-full">
          ${headerLine("Message-Id", m.messageId)}
          ${headerLine("Subject", m.subject)}
          ${headerLine("From", m.fromAddress)}
          ${headerLine("To", m.toAddress)}
          <div class="preview-body">${escapeHtml(m.body)}</div>
        </div>`
      : "";
    return `
    <tr>
      <td>${escapeHtml(m.direction)}</td>
      <td class="title">${escapeHtml(m.subject)}</td>
      <td class="title">${escapeHtml(m.fromAddress || m.toAddress)}</td>
      <td class="title preview-cell">
        ${hasMore
          ? `<details><summary>${escapeHtml(m.preview)}&hellip;</summary>${fullBlock}</details>`
          : escapeHtml(m.preview || "(empty)")}
      </td>
      ${ageCellHtml(m.ageDays)}
    </tr>`;
  }).join("");
}

function renderManualHandled() {
  document.getElementById("manual-handled").innerHTML = MANUAL_HANDLED.map(group => `
    <div class="manual-row">
      <span class="manual-status">${group.status} (${group.count})</span>
      <span style="color:var(--text-secondary); font-size:13px;">${group.companies.join(", ")}</span>
    </div>`).join("");
}

document.getElementById("search").addEventListener("input", (e) => {
  query = e.target.value;
  renderTable();
});

// Shared by "Regenerate page" and auto-refresh below: reload THIS tab in
// place via a cache-busted self-navigation (not a plain location.reload(),
// which some browsers may serve from cache for file:// URLs). Stashes the
// current scroll position first — a full reload otherwise always jumps
// back to the top of the page, which is a needless annoyance for a
// dashboard that's expected to auto-refresh under you every few minutes.
const SCROLL_STORAGE_KEY = "pendingActionsScrollY";
function reloadSelf() {
  try {
    window.sessionStorage.setItem(SCROLL_STORAGE_KEY, String(window.scrollY));
  } catch (e) { /* sessionStorage unavailable (e.g. locked-down file:// origin) — scroll just resets, not fatal */ }
  window.location.href = window.location.pathname + "?_r=" + Date.now();
}

// "Regenerate page" (2026-07-19 rewrite): fires the refreshpending://run
// URL scheme with no_open=1 (see tools/refresh-pending/main.swift), which
// re-runs render_pending_actions.py but deliberately does NOT open a new
// browser window/tab itself. Instead, THIS tab waits ~REGEN_DELAY_MS (sized
// to the current lead count above) and then reloads itself via reloadSelf().
// Fixed delay rather than polling for completion — a static file:// page
// can't reliably fetch/poll its own file cross-browser (Chrome's fetch()
// rejects the file: scheme outright) — so this trades a little slack time
// for something that works everywhere.
document.getElementById("regen-btn").addEventListener("click", () => {
  const btn = document.getElementById("regen-btn");
  const spinner = document.getElementById("regen-spinner");
  btn.disabled = true;
  btn.textContent = "Regenerating\u2026";
  spinner.hidden = false;
  window.location.href = "refreshpending://run?no_open=1";
  // No explicit "hide" call needed on success: reloadSelf() navigates this
  // tab to a fresh copy of the page, whose spinner starts `hidden` again.
  // Only guard against a stuck spinner if the refreshpending:// scheme
  // itself was never registered/accepted, in which case reloadSelf() below
  // still fires on schedule and re-renders this same stale page in place.
  setTimeout(reloadSelf, REGEN_DELAY_MS);
});

// Auto-refresh (2026-07-19): the hourly recruiting-automation cycle
// (run_cycle.sh) already regenerates this file on its own every hour —
// this just makes an already-open tab notice and pick that up, without
// needing you to remember to reload it or click Regenerate (which also
// re-runs the rescore). Purely a disk re-read via reloadSelf() — this
// NEVER fires refreshpending://run, so it costs nothing and never
// re-triggers a rescore itself. Defaults on; the choice is remembered
// per-browser via localStorage (best-effort — silently falls back to
// "always on, not remembered" if storage is unavailable).
const AUTO_REFRESH_MS = 5 * 60 * 1000;
const AUTO_REFRESH_STORAGE_KEY = "pendingActionsAutoRefreshEnabled";

function loadAutoRefreshPref() {
  try {
    const v = window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY);
    return v === null ? true : v === "1";
  } catch (e) {
    return true;
  }
}
function saveAutoRefreshPref(enabled) {
  try {
    window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, enabled ? "1" : "0");
  } catch (e) { /* best-effort only */ }
}

let autoRefreshEnabled = loadAutoRefreshPref();
let autoRefreshRemainingMs = AUTO_REFRESH_MS;

function formatMmSs(ms) {
  const totalSec = Math.max(0, Math.round(ms / 1000));
  return `${Math.floor(totalSec / 60)}:${String(totalSec % 60).padStart(2, "0")}`;
}

function renderAutoRefreshStatus() {
  document.getElementById("auto-refresh-status").textContent = autoRefreshEnabled
    ? `(next check in ${formatMmSs(autoRefreshRemainingMs)})`
    : "(paused)";
}

const autoRefreshToggle = document.getElementById("auto-refresh-toggle");
autoRefreshToggle.checked = autoRefreshEnabled;
autoRefreshToggle.addEventListener("change", (e) => {
  autoRefreshEnabled = e.target.checked;
  saveAutoRefreshPref(autoRefreshEnabled);
  autoRefreshRemainingMs = AUTO_REFRESH_MS;
  renderAutoRefreshStatus();
});

setInterval(() => {
  if (!autoRefreshEnabled) {
    renderAutoRefreshStatus();
    return;
  }
  autoRefreshRemainingMs -= 1000;
  if (autoRefreshRemainingMs > 0) {
    renderAutoRefreshStatus();
    return;
  }
  const searchEl = document.getElementById("search");
  const searchBusy = document.activeElement === searchEl || (searchEl && searchEl.value.trim() !== "");
  if (searchBusy) {
    // Don't discard an in-progress filter — retry every second instead of
    // waiting a whole other AUTO_REFRESH_MS once the field is cleared/blurred.
    autoRefreshRemainingMs = 1000;
    renderAutoRefreshStatus();
    return;
  }
  reloadSelf();
}, 1000);
renderAutoRefreshStatus();

renderFunnel();
renderPills();
renderTable();
renderReadyToApply();
renderNeedsDecisionForced();
renderAwaitingLlmReview();
renderJdUnresolved();
renderUnmatchedCommunications();
renderManualHandled();

// Restore scroll position stashed by reloadSelf() above, if any — must run
// after the render*() calls above so the page has its full height first.
(function restoreScrollPosition() {
  try {
    const saved = window.sessionStorage.getItem(SCROLL_STORAGE_KEY);
    if (saved !== null) {
      window.sessionStorage.removeItem(SCROLL_STORAGE_KEY);
      window.scrollTo(0, parseInt(saved, 10) || 0);
    }
  } catch (e) { /* best-effort only */ }
})();
</script>
</body>
</html>
"""


def _render_html(data: dict, *, output_root: Path) -> str:
    footer = (
        f"Generated as a static bookmarkable snapshot of leads.db. {data['total_leads']} total leads. "
        f"Funnel: {len(data['jd_unresolved'])} JD unresolved, {len(data['awaiting_llm_review'])} awaiting "
        f"full-LLM-review, {len(data['needs_decision'])} needs your decision, "
        f"{len(data['needs_decision_forced'])} needs decision (forced package), "
        f"{len(data['ready_to_apply'])} ready to apply. Plus {data['not_prioritized_count']} not "
        "prioritized (low score or already-\"pass\"). Tracking (past package_generated): "
        + (
            ", ".join(f"{g['count']} {g['status']}" for g in data["manual_handled"])
            if data["manual_handled"]
            else "none"
        )
        + f". {len(data['unmatched_communications'])} unmatched communication(s) awaiting manual resolution."
        + f' <span id="direct-recruiter-count">{data["direct_recruiter_count"]}</span> of the leads above '
        "(\u2B50) are confirmed direct recruiter outreach — this updates live as you use the \u2606/\u2B50/"
        f'\u2014 dropdown in the tables above. {data["direct_recruiter_undecided_count"]} lead(s) total '
        "still await that review as of this regenerate (run `review-direct-recruiter-outreach` to go "
        'through all of them); <span id="direct-recruiter-undecided-visible-count">'
        f'{data["direct_recruiter_undecided_visible_count"]}</span> of those are visible in the tables '
        "above right now (this sub-count also updates live)."
    )
    html = _TEMPLATE
    html = html.replace("${GENERATED_AT}", data["generated_at"].strftime("%Y-%m-%d %H:%M %Z") or data["generated_at"].strftime("%Y-%m-%d %H:%M"))
    html = html.replace("${FOOTER_NOTE}", footer)
    html = html.replace("${READY_TO_APPLY_JSON}", json.dumps(data["ready_to_apply"]))
    html = html.replace("${NEEDS_DECISION_FORCED_JSON}", json.dumps(data["needs_decision_forced"]))
    html = html.replace("${NEEDS_DECISION_JSON}", json.dumps(data["needs_decision"]))
    html = html.replace("${AWAITING_LLM_REVIEW_JSON}", json.dumps(data["awaiting_llm_review"]))
    html = html.replace("${JD_UNRESOLVED_JSON}", json.dumps(data["jd_unresolved"]))
    html = html.replace("${NOT_PRIORITIZED_COUNT_JSON}", json.dumps(data["not_prioritized_count"]))
    html = html.replace("${MANUAL_HANDLED_JSON}", json.dumps(data["manual_handled"]))
    html = html.replace("${UNMATCHED_COMMUNICATIONS_JSON}", json.dumps(data["unmatched_communications"]))
    html = html.replace("${FOLDER_ROOT}", str(output_root))
    html = html.replace("${STALE_DAYS_THRESHOLD}", str(STALE_DAYS_THRESHOLD))
    html = html.replace("${LLM_REVIEW_GATE_PCT}", str(LLM_REVIEW_GATE_PCT))
    # ~15ms/lead (the rescore loop's dominant cost) plus a flat 3s floor for
    # process startup/venv activation, capped at 20s so a runaway lead count
    # can't leave the regen button looking hung forever.
    regen_delay_ms = max(3000, min(20000, data["total_leads"] * 15))
    html = html.replace("${REGEN_DELAY_MS_JSON}", json.dumps(regen_delay_ms))
    return html


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_HTML)
    ap.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Résumé/JD folder root (for file counts + folder links)")
    ap.add_argument("--no-rescore", action="store_true", help="Skip refreshing status='new' leads' rule-based scores before rendering")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        if not args.no_rescore:
            n = _rescore_new_leads(conn)
            print(f"Rescored {n} status='new' lead(s) with the current rule-based scorer.")
        data = render(conn, output_root=args.output_root, now=datetime.now().astimezone())
    finally:
        conn.close()

    html = _render_html(data, output_root=args.output_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(
        f"Wrote {args.output} ({data['total_leads']} leads, {len(data['ready_to_apply'])} ready to apply, "
        f"{len(data['needs_decision'])} needing a decision)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
