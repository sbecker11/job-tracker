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
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect  # noqa: E402
from job_tracker.scoring.scorer import score_jd  # noqa: E402

DEFAULT_OUTPUT_HTML = _REPO_ROOT / "var" / "pending-actions.html"

_LEAD_COLUMNS = (
    "normalized_key, company, title, status, jd_text, jd_resolved, "
    "match_pct, matched_skills, verdict, rationale, "
    "llm_verdict, llm_match_pct"
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


def _lead_folder_and_count(output_root: Path, *, company: str, title: str, multi_lead: bool) -> tuple[str, int]:
    """This one lead's own folder (relative to `output_root`) plus a file
    count scoped to just that folder — mirrors `llm_apply._job_folder`'s
    naming rules (flat `<Company>/` for a single-lead company, nested
    `<Company>/<Company>_<Title>/` once a second lead exists) without its
    mkdir/migration side effects, since this only reads state to render a
    static page. 0 files if the folder doesn't exist yet (e.g. a
    multi-lead company whose sibling hasn't triggered the on-disk
    migration out of the old flat layout yet — self-heals next time the
    real pipeline runs for that lead)."""
    company_safe = _safe_filename(company)
    rel = f"{company_safe}/{_safe_filename(f'{company}_{title}')}" if multi_lead else company_safe
    lead_dir = output_root / rel
    count = sum(1 for p in lead_dir.rglob("*") if p.is_file()) if lead_dir.is_dir() else 0
    return rel, count


def _fmt_pct(pct: float | None) -> float:
    return round(pct or 0.0, 1)


def _company_label(company: str, count: int) -> str:
    return company if count <= 1 else f"{company} (x{count})"


def render(conn, *, output_root: Path, now: datetime) -> dict:
    rows = [dict(r) for r in conn.execute(f"SELECT {_LEAD_COLUMNS} FROM job_leads")]

    # Per-company distinct titles across ALL rows/statuses (mirrors
    # store.get_sibling_titles' scope) — computed once here instead of one
    # DB round-trip per row, purely to decide flat-vs-subfolder layout below.
    company_titles: defaultdict[str, set[str]] = defaultdict(set)
    for r in rows:
        company_titles[r["company"]].add(r["title"])

    pending_review: list[dict] = []
    auto_skipped: list[dict] = []
    unresolved: list[dict] = []
    needs_manual_jd: list[dict] = []
    manual_status: defaultdict[str, Counter] = defaultdict(Counter)

    for r in rows:
        if r["status"] != "new":
            manual_status[r["status"]][r["company"]] += 1
            continue

        multi_lead = len(company_titles[r["company"]]) > 1
        folder_path, fc = _lead_folder_and_count(
            output_root, company=r["company"], title=r["title"], multi_lead=multi_lead
        )
        verdict = r["llm_verdict"] or r["verdict"] or "review"
        pct = r["llm_match_pct"] if r["llm_match_pct"] is not None else r["match_pct"]
        pct = _fmt_pct(pct)

        entry = {"company": r["company"], "title": r["title"], "fileCount": fc, "folderPath": folder_path}
        if verdict == "REVIEW NEEDED":
            needs_manual_jd.append(entry)
        elif pct > 0 and verdict in ("review", "pursue"):
            pending_review.append({**entry, "matchPct": pct, "verdict": verdict})
        elif pct > 0 and verdict == "pass":
            auto_skipped.append({**entry, "matchPct": pct})
        else:
            unresolved.append(entry)

    manual_handled = [
        {
            "status": status,
            "count": sum(counts.values()),
            "companies": [_company_label(c, n) for c, n in sorted(counts.items())],
        }
        for status, counts in sorted(manual_status.items())
    ]

    pending_review.sort(key=lambda l: l["matchPct"], reverse=True)
    auto_skipped.sort(key=lambda l: l["matchPct"], reverse=True)
    unresolved.sort(key=lambda l: l["company"].lower())
    needs_manual_jd.sort(key=lambda l: l["company"].lower())

    status_counts = Counter(r["status"] for r in rows)
    new_verdict_counts = Counter(r["llm_verdict"] or r["verdict"] or "review" for r in rows if r["status"] == "new")

    return {
        "pending_review": pending_review,
        "auto_skipped": auto_skipped,
        "unresolved": unresolved,
        "needs_manual_jd": needs_manual_jd,
        "manual_handled": manual_handled,
        "status_counts": status_counts,
        "new_verdict_counts": new_verdict_counts,
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
  .stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }
  .stat { border: 1px solid var(--border); border-radius: 8px; padding: 14px; background: var(--panel); }
  .stat .value { font-size: 24px; font-weight: 600; }
  .stat .label { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
  .stat.warning .value { color: var(--warning); }
  .stat.danger .value { color: var(--danger); }
  .stat.success .value { color: var(--success); }
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
  tbody tr { border-bottom: 1px solid var(--border); }
  tbody tr:nth-child(odd) { background: rgba(255,255,255,0.02); }
  tbody tr.high { background: rgba(74,144,217,0.08); }
  tbody tr.pursue { background: rgba(217,83,79,0.10); }
  td { padding: 8px 10px; vertical-align: middle; }
  td.company { font-weight: 600; }
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
  .hint { font-size: 12px; color: var(--text-tertiary); margin-top: 10px; }
  .divider { border: none; border-top: 1px solid var(--border); margin: 28px 0; }
  details { border: 1px solid var(--border); border-radius: 8px; background: var(--panel); margin-bottom: 12px; }
  summary { padding: 12px 14px; cursor: pointer; font-size: 14px; display: flex; align-items: center; justify-content: space-between; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  .count-pill { border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; font-size: 12px; color: var(--text-secondary); }
  .verdict-badge { border-radius: 999px; padding: 1px 8px; font-size: 11px; }
  .verdict-badge.pursue { color: var(--danger); border: 1px solid var(--danger); }
  .verdict-badge.review { color: var(--text-secondary); border: 1px solid var(--border); }
  .card-body { padding: 0 14px 14px; }
  .manual-row { display: flex; gap: 8px; align-items: baseline; padding: 6px 0; }
  .manual-status { border: 1px solid var(--border); border-radius: 999px; padding: 1px 8px; font-size: 11px; color: var(--text-secondary); white-space: nowrap; }
  .footer-note { font-size: 11px; color: var(--text-tertiary); margin-top: 32px; }
  .company-link { color: var(--text); text-decoration: none; }
  .company-link:hover { text-decoration: underline; color: var(--info); }
  .file-count { color: var(--text-tertiary); font-weight: 400; font-size: 11px; margin-left: 4px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Pending job-tracker actions</h1>
  <div class="subtitle">
    Live snapshot of <code>leads.db</code>, regenerated ${GENERATED_AT} via
    <code>scripts/render_pending_actions.py</code>.<br/>
    Static snapshot &mdash; not live-synced. Re-run that script (or ask the agent to) after further changes.
  </div>

  <div class="stats" id="stats"></div>

  <div class="callout">
    <div class="title">Why so many are still "review"</div>
    Most of these came off LinkedIn job-alert digests with only a thin snippet (title/location), not a
    full JD, so the rule-based/LLM scorers had too little text to confidently recommend pursuing. The
    rows in "Needs your review" below all have a nonzero, meaningful match score. Leads with a 0% score
    and no LLM verdict are collapsed further down into "Unresolved" &mdash; probably not worth working
    through individually unless you want to try resolving their JDs (see PRIMER.md's "Resolving JDs for
    link-only digest leads" section).
  </div>

  <h2>
    <span id="table-heading">Needs your review</span>
    <span class="pills" id="priority-pills"></span>
  </h2>
  <input type="text" id="search" placeholder="Filter by company or title&hellip;" />
  <div class="table-scroll">
    <table>
      <thead>
        <tr>
          <th>Company</th>
          <th>Title</th>
          <th class="num">Match %</th>
          <th>Verdict</th>
          <th>Priority</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody id="table-body"></tbody>
    </table>
  </div>
  <div class="hint">
    Rows shaded red are already scored <strong>PURSUE</strong> but haven't had a package generated yet.
    "Copy prompt" copies a ready-to-paste request for a new Cursor chat, pre-loaded with that
    company/title, so the agent can pull its full stored review and &mdash; if you decide to pursue
    &mdash; generate the r&eacute;sum&eacute; + cover letter for you (with <code>--force</code> for
    "review" verdicts, since <code>apply_package.py</code> only auto-generates on "pursue").
  </div>

  <hr class="divider" />

  <details open>
    <summary>JD could not be resolved &mdash; needs manual intervention <span class="count-pill" id="needs-manual-jd-count"></span></summary>
    <div class="card-body">
      <div class="table-scroll short">
        <table>
          <thead><tr><th>Company</th><th>Title</th></tr></thead>
          <tbody id="needs-manual-jd-body"></tbody>
        </table>
      </div>
      <div class="hint">
        Link-following and a company-careers-page search both failed to turn up a full JD (2026-07-11
        policy &mdash; see <code>~/CLAUDE.md</code> &sect;11 / PRIMER.md). Scored 0% and marked
        <code>REVIEW NEEDED</code> instead of leaning on a thin-snippet score.
      </div>
    </div>
  </details>

  <details>
    <summary>Already scored "pass", not yet marked skipped <span class="count-pill" id="auto-skip-count"></span></summary>
    <div class="card-body">
      <div class="table-scroll short">
        <table>
          <thead><tr><th>Company</th><th>Title</th><th class="num">Match %</th></tr></thead>
          <tbody id="auto-skip-body"></tbody>
        </table>
      </div>
    </div>
  </details>

  <details>
    <summary>Unresolved &mdash; thin/no JD, 0% score, not worth reviewing individually <span class="count-pill" id="unresolved-count"></span></summary>
    <div class="card-body">
      <div class="table-scroll short">
        <table>
          <thead><tr><th>Company</th><th>Title</th></tr></thead>
          <tbody id="unresolved-body"></tbody>
        </table>
      </div>
    </div>
  </details>

  <details>
    <summary>Manually tracked leads &mdash; already past "new"</summary>
    <div class="card-body" id="manual-handled"></div>
  </details>

  <div class="footer-note">${FOOTER_NOTE}</div>
</div>

<script>
const PENDING_REVIEW = ${PENDING_REVIEW_JSON};
const AUTO_SKIPPED = ${AUTO_SKIPPED_JSON};
const UNRESOLVED = ${UNRESOLVED_JSON};
const NEEDS_MANUAL_JD = ${NEEDS_MANUAL_JD_JSON};
const MANUAL_HANDLED = ${MANUAL_HANDLED_JSON};

function priorityOf(pct) {
  if (pct >= 50) return "high";
  if (pct >= 35) return "medium";
  return "low";
}
const PRIORITY_LABEL = { high: "High (\u226550%)", medium: "Medium (35\u201349%)", low: "Low (<35%)" };

let query = "";
let priorityFilter = "all";

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

// folderPath is precomputed server-side per LEAD (not per company) — see
// render_pending_actions._lead_folder_and_count, which mirrors
// job_tracker.pipeline.llm_apply._safe_filename()/_job_folder()'s naming
// rules: a single-lead company's files sit flat in <Company>/, a
// multi-lead company's land in <Company>/<Company>_<Title>/ instead — so
// each lead's row links straight to ITS OWN folder, not just the shared
// company root, and its file count is scoped to that folder alone.
const FOLDER_ROOT = "${FOLDER_ROOT}";
function folderUrl(folderPath) {
  return `file://${FOLDER_ROOT}/${encodeURIComponent(folderPath).replace(/%2F/g, "/")}/`;
}
function companyCellHtml(company, folderPath, fileCount) {
  const countSuffix = fileCount > 0 ? `<span class="file-count">(${fileCount} file${fileCount === 1 ? "" : "s"})</span>` : "";
  return `<a class="company-link" href="${folderUrl(folderPath)}" target="_blank" rel="noopener">${escapeHtml(company)}</a>${countSuffix}`;
}

function renderStats() {
  const el = document.getElementById("stats");
  const pursueNotActioned = PENDING_REVIEW.filter(l => l.verdict === "pursue").length;
  const packageGenerated = (MANUAL_HANDLED.find(g => g.status === "package_generated") || { count: 0 }).count;
  const interviewing = (MANUAL_HANDLED.find(g => g.status === "interviewing") || { count: 0 }).count;
  const items = [
    { value: PENDING_REVIEW.length, label: "Needs review (meaningful score)", cls: "warning" },
    { value: pursueNotActioned, label: "PURSUE verdict, not yet actioned", cls: "danger" },
    { value: packageGenerated, label: "Package generated (ready to send)", cls: "success" },
    { value: interviewing, label: "Interviewing", cls: "success" },
    { value: NEEDS_MANUAL_JD.length, label: "JD unresolved, needs manual look", cls: "warning" },
  ];
  el.innerHTML = items.map(i => `
    <div class="stat ${i.cls}">
      <div class="value">${i.value}</div>
      <div class="label">${i.label}</div>
    </div>`).join("");
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

function renderTable() {
  const q = query.trim().toLowerCase();
  const filtered = PENDING_REVIEW
    .filter(l => (priorityFilter === "all" || priorityOf(l.matchPct) === priorityFilter))
    .filter(l => !q || l.company.toLowerCase().includes(q) || l.title.toLowerCase().includes(q))
    .sort((a, b) => b.matchPct - a.matchPct);

  document.getElementById("table-heading").textContent = `Needs your review (${filtered.length} of ${PENDING_REVIEW.length})`;

  const body = document.getElementById("table-body");
  body.innerHTML = filtered.map((lead, idx) => `
    <tr class="${lead.verdict === "pursue" ? "pursue" : (priorityOf(lead.matchPct) === "high" ? "high" : "")}">
      <td class="company">${companyCellHtml(lead.company, lead.folderPath, lead.fileCount)}</td>
      <td class="title">${escapeHtml(lead.title)}</td>
      <td class="num">${lead.matchPct}%</td>
      <td><span class="verdict-badge ${lead.verdict}">${lead.verdict.toUpperCase()}</span></td>
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

function renderNeedsManualJd() {
  document.getElementById("needs-manual-jd-count").textContent = NEEDS_MANUAL_JD.length;
  document.getElementById("needs-manual-jd-body").innerHTML = NEEDS_MANUAL_JD.map(l => `
    <tr>
      <td class="company">${companyCellHtml(l.company, l.folderPath, l.fileCount)}</td>
      <td class="title">${escapeHtml(l.title)}</td>
    </tr>`).join("");
}

function renderAutoSkipped() {
  document.getElementById("auto-skip-count").textContent = AUTO_SKIPPED.length;
  document.getElementById("auto-skip-body").innerHTML = AUTO_SKIPPED.map(l => `
    <tr>
      <td class="company">${companyCellHtml(l.company, l.folderPath, l.fileCount)}</td>
      <td class="title">${escapeHtml(l.title)}</td>
      <td class="num">${l.matchPct}%</td>
    </tr>`).join("");
}

function renderUnresolved() {
  document.getElementById("unresolved-count").textContent = UNRESOLVED.length;
  document.getElementById("unresolved-body").innerHTML = UNRESOLVED.map(l => `
    <tr>
      <td class="company">${companyCellHtml(l.company, l.folderPath, l.fileCount)}</td>
      <td class="title">${escapeHtml(l.title)}</td>
    </tr>`).join("");
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

renderStats();
renderPills();
renderTable();
renderNeedsManualJd();
renderAutoSkipped();
renderUnresolved();
renderManualHandled();
</script>
</body>
</html>
"""


def _render_html(data: dict, *, output_root: Path) -> str:
    footer = (
        f"Generated as a static bookmarkable snapshot of leads.db. Counts: {data['total_leads']} total leads, "
        f"{data['status_counts'].get('new', 0)} status=new "
        f"({data['new_verdict_counts'].get('review', 0)} review / "
        f"{data['new_verdict_counts'].get('pass', 0)} pass / "
        f"{data['new_verdict_counts'].get('pursue', 0)} pursue"
        + (f" / {data['new_verdict_counts']['REVIEW NEEDED']} REVIEW NEEDED" if data['new_verdict_counts'].get('REVIEW NEEDED') else "")
        + "), "
        + ", ".join(
            f"{count} {status}"
            for status, count in sorted(data["status_counts"].items())
            if status != "new"
        )
        + "."
    )
    html = _TEMPLATE
    html = html.replace("${GENERATED_AT}", data["generated_at"].strftime("%Y-%m-%d %H:%M %Z") or data["generated_at"].strftime("%Y-%m-%d %H:%M"))
    html = html.replace("${FOOTER_NOTE}", footer)
    html = html.replace("${PENDING_REVIEW_JSON}", json.dumps(data["pending_review"]))
    html = html.replace("${AUTO_SKIPPED_JSON}", json.dumps(data["auto_skipped"]))
    html = html.replace("${UNRESOLVED_JSON}", json.dumps(data["unresolved"]))
    html = html.replace("${NEEDS_MANUAL_JD_JSON}", json.dumps(data["needs_manual_jd"]))
    html = html.replace("${MANUAL_HANDLED_JSON}", json.dumps(data["manual_handled"]))
    html = html.replace("${FOLDER_ROOT}", str(output_root))
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
    print(f"Wrote {args.output} ({data['total_leads']} leads, {len(data['pending_review'])} pending review).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
