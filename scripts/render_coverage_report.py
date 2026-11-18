#!/usr/bin/env python3
"""Write one dark HTML coverage report from the Python and UI coverage data.

Reads job-tracker's .coverage (via coverage.py) and the Vitest JSON under
pending-actions-ui/coverage/. Run the test suites first so those files exist.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_SUMMARY = ROOT / "pending-actions-ui" / "coverage" / "coverage-summary.json"
UI_FINAL = ROOT / "pending-actions-ui" / "coverage" / "coverage-final.json"
# Served by the Pending Actions dev server at /coverage-report.html.
OUT = ROOT / "pending-actions-ui" / "public" / "coverage-report.html"


def ranges(lines: list[int]) -> str:
    nums = sorted({int(n) for n in lines})
    if not nums:
        return ""
    spans: list[str] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append(str(start) if start == prev else f"{start}–{prev}")
        start = prev = n
    spans.append(str(start) if start == prev else f"{start}–{prev}")
    return ", ".join(spans)


def shorten(text: str, limit: int = 80) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def python_files() -> tuple[dict, list[dict]]:
    with tempfile.NamedTemporaryFile(suffix=".json") as tmp:
        subprocess.check_call(
            [sys.executable, "-m", "coverage", "json", "-o", tmp.name, "--quiet"],
            cwd=ROOT,
        )
        data = json.loads(Path(tmp.name).read_text())
    totals = data["totals"]
    files = []
    for path, info in data["files"].items():
        summary = info["summary"]
        missing = ranges(info.get("missing_lines") or [])
        files.append(
            {
                "file": path,
                "linesPct": round(100 * summary["covered_lines"] / summary["num_statements"], 1)
                if summary["num_statements"]
                else None,
                "lines": f"{summary['covered_lines']}/{summary['num_statements']}",
                "branchesPct": round(summary["percent_branches_covered"], 1)
                if summary["num_branches"]
                else None,
                "branches": f"{summary['covered_branches']}/{summary['num_branches']}"
                if summary["num_branches"]
                else "—",
                "missing": missing,
                "missingShort": shorten(missing),
            }
        )
    files.sort(key=lambda row: (row["linesPct"] is None, row["linesPct"] or 0, row["file"]))
    suite = {
        "name": "job-tracker Python",
        "note": "coverage.py score includes branches. File rows are sorted with the lowest line coverage first.",
        "score": round(totals["percent_covered"], 1),
        "linesCovered": totals["covered_lines"],
        "linesTotal": totals["num_statements"],
        "branchesCovered": totals["covered_branches"],
        "branchesTotal": totals["num_branches"],
    }
    return suite, files


def uncovered_ui_lines() -> dict[str, list[int]]:
    if not UI_FINAL.exists():
        return {}
    data = json.loads(UI_FINAL.read_text())
    found: dict[str, list[int]] = {}
    for path, info in data.items():
        lines = []
        statements = info.get("s") or {}
        statement_map = info.get("statementMap") or {}
        for sid, count in statements.items():
            if count:
                continue
            loc = statement_map.get(sid) or {}
            line = (loc.get("start") or {}).get("line")
            if line:
                lines.append(line)
        found[path] = lines
    return found


def ui_files() -> tuple[dict, list[dict]]:
    summary = json.loads(UI_SUMMARY.read_text())
    missing_by_file = uncovered_ui_lines()
    total = summary["total"]
    files = []
    for path, info in summary.items():
        if path == "total":
            continue
        rel = path
        marker = "/pending-actions-ui/"
        if marker in path:
            rel = "pending-actions-ui/" + path.split(marker, 1)[1]
        missing = ranges(missing_by_file.get(path, []))
        lines = info["lines"]
        branches = info["branches"]
        files.append(
            {
                "file": rel,
                "linesPct": lines["pct"],
                "lines": f"{lines['covered']}/{lines['total']}",
                "branchesPct": branches["pct"] if branches["total"] else None,
                "branches": f"{branches['covered']}/{branches['total']}" if branches["total"] else "—",
                "missing": missing,
                "missingShort": shorten(missing),
            }
        )
    files.sort(key=lambda row: (row["linesPct"] is None, row["linesPct"] or 0, row["file"]))
    suite = {
        "name": "Pending Actions UI",
        "note": "Vitest measures the print preview module only. The rest of the UI is outside this run.",
        "score": None,
        "linesCovered": total["lines"]["covered"],
        "linesTotal": total["lines"]["total"],
        "branchesCovered": total["branches"]["covered"],
        "branchesTotal": total["branches"]["total"],
    }
    return suite, files


def pct(covered: int, total: int) -> float | None:
    if not total:
        return None
    return round(100 * covered / total, 1)


def render(suites: list[tuple[dict, list[dict]]]) -> str:
    lines_covered = sum(s["linesCovered"] for s, _ in suites)
    lines_total = sum(s["linesTotal"] for s, _ in suites)
    branches_covered = sum(s["branchesCovered"] for s, _ in suites)
    branches_total = sum(s["branchesTotal"] for s, _ in suites)
    payload = {
        "generated": datetime.now().strftime("%-d %b %Y, %-I:%M %p"),
        "linesPct": pct(lines_covered, lines_total),
        "lines": f"{lines_covered:,}/{lines_total:,}",
        "branchesPct": pct(branches_covered, branches_total),
        "branches": f"{branches_covered:,}/{branches_total:,}",
        "suites": [
            {"suite": suite, "files": files}
            for suite, files in suites
        ],
    }
    data = json.dumps(payload).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Coverage report</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #0c0e12;
    --elevated: #1a1e27;
    --border: #2a2e37;
    --text: #e6e8ec;
    --muted: #9aa3b2;
    --ok: #9dccb0;
    --warn: #e3c48a;
    --low: #e7b09a;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font: 15px/1.45 "Segoe UI", sans-serif;
  }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 28px 20px 64px; }}
  h1 {{ font-size: 1.4rem; font-weight: 650; margin: 0 0 4px; }}
  h2 {{ font-size: 1.05rem; margin: 28px 0 6px; }}
  p {{ margin: 0; color: var(--muted); }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 18px; }}
  .card {{
    background: var(--elevated);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 12px 14px;
    min-width: 220px;
  }}
  .card strong {{ display: block; font-size: 1.35rem; font-variant-numeric: tabular-nums; }}
  .card span {{ color: var(--muted); font-size: 0.85rem; }}
  .ok {{ color: var(--ok); }}
  .warn {{ color: var(--warn); }}
  .low {{ color: var(--low); }}
  input {{
    margin-top: 18px;
    width: min(420px, 100%);
    background: var(--elevated);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 8px 10px;
    font: inherit;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 8px;
    font-size: 0.88rem;
    font-variant-numeric: tabular-nums;
  }}
  th, td {{
    text-align: left;
    padding: 7px 8px;
    border-bottom: 1px solid var(--border);
    vertical-align: top;
  }}
  th {{ color: var(--muted); font-weight: 600; }}
  td.num, th.num {{ text-align: right; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.84rem; }}
  .missing {{ color: var(--muted); }}
</style>
</head>
<body>
<main>
  <h1>Coverage report</h1>
  <p id="when"></p>
  <div class="cards" id="cards"></div>
  <input id="filter" type="search" placeholder="Filter files…" aria-label="Filter files">
  <div id="suites"></div>
</main>
<script>
const DATA = {data};

function tone(pct) {{
  if (pct == null) return "";
  if (pct >= 80) return "ok";
  if (pct >= 50) return "warn";
  return "low";
}}
function fmt(pct) {{
  return pct == null ? "—" : pct.toFixed(1) + "%";
}}
function card(label, pct, detail) {{
  return `<div class="card"><span>${{label}}</span><strong class="${{tone(pct)}}">${{fmt(pct)}}</strong><span>${{detail}}</span></div>`;
}}
document.getElementById("when").textContent = "Generated " + DATA.generated;
document.getElementById("cards").innerHTML = [
  card("Lines, both suites", DATA.linesPct, DATA.lines + " lines"),
  card("Branches, both suites", DATA.branchesPct, DATA.branches + " branches"),
].concat(DATA.suites.map((s) => {{
  const suite = s.suite;
  const detail = suite.score == null
    ? suite.linesCovered.toLocaleString() + "/" + suite.linesTotal.toLocaleString() + " lines"
    : "coverage.py " + suite.score.toFixed(1) + "% · " + suite.linesCovered.toLocaleString() + "/" + suite.linesTotal.toLocaleString() + " lines";
  const pct = suite.score == null
    ? (suite.linesTotal ? 100 * suite.linesCovered / suite.linesTotal : null)
    : suite.score;
  return card(suite.name, pct, detail);
}})).join("");

function table(files) {{
  const rows = files.map((f) => `<tr data-file="${{f.file.toLowerCase()}}">
    <td><code>${{f.file}}</code></td>
    <td class="num ${{tone(f.linesPct)}}">${{fmt(f.linesPct)}}</td>
    <td class="num">${{f.lines}}</td>
    <td class="num ${{tone(f.branchesPct)}}">${{fmt(f.branchesPct)}}</td>
    <td class="num">${{f.branches}}</td>
    <td class="missing" title="${{f.missing}}">${{f.missingShort || "—"}}</td>
  </tr>`).join("");
  return `<table>
    <thead><tr><th>File</th><th class="num">Lines</th><th class="num">Covered</th><th class="num">Branches</th><th class="num">Covered</th><th>Missing lines</th></tr></thead>
    <tbody>${{rows}}</tbody>
  </table>`;
}}
document.getElementById("suites").innerHTML = DATA.suites.map((s) => `
  <h2>${{s.suite.name}}</h2>
  <p>${{s.suite.note}} ${{s.files.length}} file${{s.files.length === 1 ? "" : "s"}}.</p>
  ${{table(s.files)}}
`).join("");
document.getElementById("filter").addEventListener("input", (e) => {{
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll("tbody tr").forEach((row) => {{
    row.hidden = q && !row.dataset.file.includes(q);
  }});
}});
</script>
</body>
</html>
"""


def main() -> None:
    if not UI_SUMMARY.exists():
        sys.exit(f"Missing {UI_SUMMARY}. Run npm test in pending-actions-ui first.")
    suite_py, files_py = python_files()
    suite_ui, files_ui = ui_files()
    OUT.write_text(render([(suite_py, files_py), (suite_ui, files_ui)]))
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
