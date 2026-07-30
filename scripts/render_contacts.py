#!/usr/bin/env python3
"""Regenerate `var/contacts.html` from the current state of `leads.db`.

The "a recruiter I've worked with before just called — who is this and what
have we got going with them" lookup (2026-07-24). Deliberately mirrors
render_pending_actions.py's static-snapshot approach rather than a live
helper app or a local server: a file:// page can't shell out to the
`list-contacts` CLI and stream results back into itself, so instead every
contact is baked into the page at generation time and a plain client-side
search box (same instant-filter pattern as pending-actions.html's own
`#search` input) does the actual lookup with zero latency — faster than a
live CLI call would be anyway, since there's no process to spawn.

    python scripts/render_contacts.py

Re-run any time contacts change (wired into recruiting-automation's hourly
run_cycle.sh, right after render_pending_actions.py, so this never goes
stale by more than an hour).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from job_tracker.pipeline.llm_apply import DEFAULT_OUTPUT_ROOT, _safe_filename  # noqa: E402
from job_tracker.pipeline.store import DEFAULT_DB_PATH, connect, get_sibling_titles, list_all_contacts  # noqa: E402

DEFAULT_OUTPUT_HTML = _REPO_ROOT / "var" / "contacts.html"


def _folder_path(conn, *, company: str, title: str) -> str:
    """This lead's package folder, relative to DEFAULT_OUTPUT_ROOT — same
    naming rule as render_pending_actions.py's `_lead_folder_and_count`
    (itself a read-only mirror of `llm_apply._job_folder`, deliberately
    reimplemented rather than calling `_job_folder` directly, since that
    function mkdir's and can migrate files — not something a page-render
    script should ever trigger as a side effect). No file count needed
    here (contacts.html only needs the path to open in Finder)."""
    multi_lead = len(get_sibling_titles(conn, company, exclude_title=title)) > 0
    company_safe = _safe_filename(company)
    if not multi_lead:
        return company_safe
    return f"{company_safe}/{_safe_filename(f'{company}_{title}')}"


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<title>Contacts lookup</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --border: #2a2e37;
    --text: #e6e8ec;
    --text-secondary: #9aa0ac;
    --text-tertiary: #6b7280;
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
  .subtitle { color: var(--text-secondary); font-size: 13px; margin-bottom: 20px; }
  .subtitle code { color: var(--text); background: var(--panel); padding: 1px 5px; border-radius: 4px; }
  input[type="text"] {
    width: 100%;
    max-width: 420px;
    padding: 10px 12px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--panel);
    color: var(--text);
    font-size: 15px;
    margin-bottom: 14px;
  }
  input[type="text"]::placeholder { color: var(--text-tertiary); }
  #count { font-size: 12px; color: var(--text-tertiary); margin-bottom: 10px; }
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
  tbody tr { border-bottom: 1px solid var(--border); }
  tbody tr:nth-child(odd) { background: rgba(255,255,255,0.02); }
  tbody tr:hover { background: rgba(108,126,225,0.08); }
  td { padding: 8px 10px; vertical-align: top; }
  td.name { font-weight: 600; }
  td.email a, td.phone a { color: var(--accent); text-decoration: none; }
  td.email a:hover, td.phone a:hover { text-decoration: underline; }
  td.company-title .company { font-weight: 500; }
  td.company-title .title { color: var(--text-secondary); font-size: 12px; }
  .lead-link { color: inherit; text-decoration: none; }
  .lead-link:hover { text-decoration: underline; color: var(--info); }
  .role-badge {
    display: inline-block;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 999px;
    border: 1px solid var(--border);
    color: var(--text-secondary);
  }
  .empty-state { color: var(--text-tertiary); font-size: 13px; padding: 20px 0; }
  .footer-note { font-size: 12px; color: var(--text-tertiary); margin-top: 20px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Contacts lookup</h1>
  <div class="subtitle">
    Every recruiter / hiring manager / referral on file, across every lead. Generated
    <code>${GENERATED_AT}</code> &mdash; search matches name, company, title, role, phone, or email.
  </div>
  <input type="text" id="search" placeholder="Who's calling? Search name, company, phone, email&hellip;" autofocus />
  <div id="count"></div>
  <table>
    <thead>
      <tr>
        <th>Name</th>
        <th>Role</th>
        <th>Lead</th>
        <th>Phone</th>
        <th>Email</th>
        <th>Last contact</th>
      </tr>
    </thead>
    <tbody id="table-body"></tbody>
  </table>
  <div class="footer-note">${FOOTER_NOTE}</div>
</div>
<script>
const CONTACTS = ${CONTACTS_JSON};
const FOLDER_ROOT = "${FOLDER_ROOT}";

// Opens this lead's package folder in Finder via the same local
// RevealFolder helper pending-actions.html uses (tools/reveal-folder/) —
// browsers cannot open Finder from a static file:// page on their own.
// Install once: tools/reveal-folder/install.sh
function folderUrl(folderPath) {
  const abs = `${FOLDER_ROOT}/${folderPath}`.replace(/\/+/g, "/");
  return `revealfolder://reveal?path=${encodeURIComponent(abs)}`;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function rowHtml(c) {
  const name = escapeHtml(c.name) || "(no name on file)";
  const role = c.role ? `<span class="role-badge">${escapeHtml(c.role)}</span>` : "";
  const phone = c.phone ? `<a href="tel:${escapeHtml(c.phone)}">${escapeHtml(c.phone)}</a>` : "&mdash;";
  const email = c.email ? `<a href="mailto:${escapeHtml(c.email)}">${escapeHtml(c.email)}</a>` : "&mdash;";
  const lastContact = c.lastContactedAt ? c.lastContactedAt.slice(0, 10) : "&mdash;";
  return `<tr>
    <td class="name">${name}</td>
    <td>${role}</td>
    <td class="company-title">
      <a class="lead-link" href="${folderUrl(c.folderPath)}" title="Open this role's folder in Finder">
        <div class="company">${escapeHtml(c.company)}</div>
        <div class="title">${escapeHtml(c.title)}</div>
      </a>
    </td>
    <td class="phone">${phone}</td>
    <td class="email">${email}</td>
    <td>${lastContact}</td>
  </tr>`;
}

function matches(c, needle) {
  if (!needle) return true;
  const haystack = [c.name, c.company, c.title, c.role, c.phone, c.email].join(" ").toLowerCase();
  return haystack.includes(needle);
}

function render() {
  const needle = document.getElementById("search").value.trim().toLowerCase();
  const rows = CONTACTS.filter((c) => matches(c, needle));
  document.getElementById("table-body").innerHTML = rows.length
    ? rows.map(rowHtml).join("")
    : "";
  document.getElementById("count").textContent = needle
    ? `${rows.length} of ${CONTACTS.length} contact(s) match "${needle}"`
    : `${CONTACTS.length} contact(s) total`;
  if (!rows.length) {
    document.getElementById("table-body").innerHTML =
      `<tr><td colspan="6" class="empty-state">No contacts match that search.</td></tr>`;
  }
}

document.getElementById("search").addEventListener("input", render);
render();
</script>
</body>
</html>
"""


def _to_rows(contacts: list[dict], *, conn=None) -> list[dict]:
    rows = []
    for c in contacts:
        company = c["job_company"] or ""
        title = c["job_title"] or ""
        rows.append(
            {
                "name": c["name"] or "",
                "company": company,
                "title": title,
                "role": (c["role"] or "").replace("_", " "),
                "phone": c["phone"] or "",
                "email": c["email"] or "",
                "lastContactedAt": c["last_contacted_at"] or "",
                "folderPath": _folder_path(conn, company=company, title=title) if conn is not None else "",
            }
        )
    # Most-recently-contacted first — the person most likely to be the one
    # calling you back is the one you last heard from; search still finds
    # anyone regardless of this default order.
    rows.sort(key=lambda r: r["lastContactedAt"], reverse=True)
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    ap.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_HTML)
    ap.add_argument(
        "--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="Résumé/JD folder root (for folder links)"
    )
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"No leads DB found at {args.db}", file=sys.stderr)
        return 1

    conn = connect(args.db)
    try:
        contacts = [dict(r) for r in list_all_contacts(conn)]
        rows = _to_rows(contacts, conn=conn)
    finally:
        conn.close()

    generated_at = datetime.now().astimezone()

    html = _TEMPLATE
    html = html.replace(
        "${GENERATED_AT}", generated_at.strftime("%Y-%m-%d %H:%M %Z") or generated_at.strftime("%Y-%m-%d %H:%M")
    )
    html = html.replace(
        "${FOOTER_NOTE}",
        "Static bookmarkable snapshot — re-run `python scripts/render_contacts.py` (or wait for the next "
        "hourly automation cycle) to pick up newly logged contacts.",
    )
    html = html.replace("${CONTACTS_JSON}", json.dumps(rows))
    html = html.replace("${FOLDER_ROOT}", str(args.output_root))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(html, encoding="utf-8")
    print(f"Wrote {args.output} ({len(rows)} contact(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
