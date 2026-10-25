"""Global skills analysis over `lead_skills`: which skills the market asks
for, and which of them Shawn has shown.

Two cached LLM steps, both incremental (only names not yet in the cache are
sent):
1. Merge: fold spelling variants onto one standard name ("API Design" /
   "API Development"), with a category.
2. Match: mark each standard skill against ~/CLAUDE.md §6–§9 as
   met_client / met_portfolio / partial / gap, with the evidence it relied on.

The page and the caches live under var/ (gitignored): the match step quotes
the candidate profile, so none of it belongs in the repo.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from job_tracker.pipeline.required_skills import CATEGORIES

_REPO_ROOT = Path(__file__).resolve().parents[3]
CACHE_DIR = _REPO_ROOT / "var" / "skills"
MAP_CACHE = CACHE_DIR / "skill_map.json"
MATCH_CACHE = CACHE_DIR / "skill_match.json"
DEFAULT_HTML = _REPO_ROOT / "var" / "skills-analysis.html"
MIN_SKILLS_FOR_REAL_JD = 3
MERGE_CHUNK = 100
MATCH_CHUNK = 50
# Long list-mapping replies: thinking spent the whole output budget before any text.
_NO_THINKING = {"type": "disabled"}
STATUSES = ("met_client", "met_portfolio", "partial", "gap")
_OPEN = ("new", "pursued", "package_generated", "applied", "following_up", "interviewing", "offered")


@dataclass
class SkillRow:
    job_key: str
    company: str
    status: str
    role_family: str
    canonical: str
    category: str
    importance: str


def load_rows(conn: sqlite3.Connection) -> list[SkillRow]:
    """Skills from leads whose extraction looks like a real JD."""
    rows = conn.execute(
        """
        SELECT s.job_key, coalesce(l.company, ''), coalesce(l.status, ''), coalesce(e.role_family, 'other'),
               s.canonical, coalesce(s.category, ''), s.importance
        FROM lead_skills s
        JOIN lead_skill_extractions e ON e.job_key = s.job_key
        LEFT JOIN job_leads l ON l.normalized_key = s.job_key
        WHERE s.job_key IN (SELECT job_key FROM lead_skills GROUP BY job_key HAVING count(*) >= ?)
        """,
        (MIN_SKILLS_FOR_REAL_JD,),
    ).fetchall()
    return [SkillRow(*r) for r in rows]


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")


_MERGE_PROMPT = f"""You normalize skill names pulled from job descriptions so they can be counted together.

You get ALL NAMES in use (with how many jobs use each, for reference), the STANDARD names already \
chosen, and a short list of names TO MAP. For each name to map, give the standard name it belongs to. \
Reuse an existing standard name when it means the same skill. Otherwise use the most-used variant \
among its synonyms in ALL NAMES, so the same choice is made no matter which list a synonym arrives in. \
Merge only true synonyms and spelling variants ("API Development" and "API Design"; "ML" and \
"Machine Learning"; "Postgres" and "PostgreSQL"). Keep distinct products distinct ("AWS Lambda" is \
not "AWS"; "React" is not "React Native"; "Spark" is not "PySpark").

Output ONLY JSON: {{"<name exactly as given>": {{"standard": "<name>", "category": "<one of: \
{', '.join(CATEGORIES)}>"}}, ...}} with one key per name to map.
"""

_MATCH_PROMPT = """You compare skills that employers ask for against one candidate's documented background. \
The candidate profile follows. Use ONLY its sections on career timeline, education and credentials, \
portfolio projects, and technical anchors by engagement. Never invent experience.

For each skill, choose one status:
- "met_client": hands-on use in a named paid engagement or employer (Spexture clients, SeniorLink, \
ClipFile, Sierra Vista Group clients, HomePortfolio).
- "met_portfolio": shown only in a named portfolio project.
- "partial": adjacent or related experience, a certification without documented hands-on work, or a \
close substitute (e.g. GCP when the skill is Azure).
- "gap": nothing in the profile supports it.
Degrees, domains and soft skills count too: judge them against the profile the same way.

Output ONLY JSON: {"<skill exactly as given>": {"status": "...", "evidence": "<max 18 words naming the \
engagement, project, or credential; empty for gap>", "close": "<for partial or gap: max 18 words on \
the most direct way to build real evidence; empty otherwise>"}, ...}

CANDIDATE PROFILE:
"""


def _chunks(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def pending_merge(names: set[str]) -> list[str]:
    cache = _read(MAP_CACHE)
    return sorted(n for n in names if n.lower() not in cache)


def _parallel(fn, chunks: list[list[str]], workers: int) -> list[tuple[list[str], dict, float]]:
    """Run fn(chunk) -> (data, cost) over chunks; a failed chunk is skipped (retried next run)."""
    out = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fn, c): c for c in chunks}
        for fut in as_completed(futures):
            try:
                data, cost = fut.result()
            except Exception as exc:  # noqa: BLE001
                print(f"    ! chunk failed, will retry next run: {exc}", flush=True)
                continue
            out.append((futures[fut], data, cost))
    return out


def run_merge(names: set[str], *, model: str, client=None, counts: dict[str, int] | None = None, workers: int = 4) -> float:
    from job_tracker.pipeline.llm_apply import _call_and_parse_json

    cache = _read(MAP_CACHE)
    counts = counts or {}
    every = "\n".join(f"{n} ({counts.get(n, 1)})" for n in sorted(names, key=lambda n: (-counts.get(n, 1), n.lower())))
    standards = "\n".join(sorted({v["standard"] for v in cache.values()}))

    def one(chunk: list[str]):
        user = f"ALL NAMES:\n{every}\n\nSTANDARD NAMES:\n{standards}\n\nNAMES TO MAP:\n" + "\n".join(chunk)
        data, calls = _call_and_parse_json(_MERGE_PROMPT, user, model=model, client=client, max_tokens=8000, step="skill_merge", thinking=_NO_THINKING)
        return data, sum(c.cost_usd or 0 for c in calls)

    spent = 0.0
    for chunk, data, cost in _parallel(one, _chunks(pending_merge(names), MERGE_CHUNK), workers):
        spent += cost
        for name in chunk:
            got = data.get(name) if isinstance(data.get(name), dict) else {}
            category = str(got.get("category") or "").lower()
            cache[name.lower()] = {
                "standard": str(got.get("standard") or name).strip() or name,
                "category": category if category in CATEGORIES else "",
            }
        _write(MAP_CACHE, cache)
    return spent


def standard_of(name: str, cache: dict[str, Any]) -> str:
    return (cache.get(name.lower()) or {}).get("standard") or name


def pending_match(standards: set[str]) -> list[str]:
    cache = _read(MATCH_CACHE)
    return sorted(s for s in standards if s not in cache)


def profile_for_matching(profile: str) -> str:
    """§1–§13 only: the billing records and the review log (§14–§16) aren't
    background, and the log keeps superseded facts (e.g. removed employers)."""
    cut = re.search(r"^## 14\.", profile, re.M)
    return profile[: cut.start()] if cut else profile


def run_match(standards: set[str], *, profile: str, model: str, client=None, workers: int = 4) -> float:
    from job_tracker.pipeline.llm_apply import _call_and_parse_json

    cache = _read(MATCH_CACHE)
    system = _MATCH_PROMPT + profile_for_matching(profile)

    def one(chunk: list[str]):
        data, calls = _call_and_parse_json(
            system, "SKILLS:\n" + "\n".join(chunk), model=model, client=client, max_tokens=8000, step="skill_match", thinking=_NO_THINKING
        )
        return data, sum(c.cost_usd or 0 for c in calls)

    spent = 0.0
    for chunk, data, cost in _parallel(one, _chunks(pending_match(standards), MATCH_CHUNK), workers):
        spent += cost
        for name in chunk:
            got = data.get(name) if isinstance(data.get(name), dict) else {}
            status = str(got.get("status") or "").lower()
            if status not in STATUSES:
                continue
            cache[name] = {"status": status, "evidence": str(got.get("evidence") or ""), "close": str(got.get("close") or "")}
        _write(MATCH_CACHE, cache)
    return spent


def aggregate(rows: list[SkillRow]) -> dict[str, Any]:
    """Per-standard-skill demand, split by role family, plus lead totals."""
    names = _read(MAP_CACHE)
    match = _read(MATCH_CACHE)
    leads: dict[str, dict[str, Any]] = {}
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        leads.setdefault(r.job_key, {"family": r.role_family, "open": r.status in _OPEN})
        std = standard_of(r.canonical, names)
        entry = per.setdefault(
            std,
            {
                "skill": std,
                "category": (names.get(r.canonical.lower()) or {}).get("category") or r.category or "practice",
                "by": defaultdict(lambda: [set(), set()]),
                "companies": set(),
            },
        )
        slot = 0 if r.importance == "required" else 1
        entry["by"][r.role_family][slot].add(r.job_key)
        if r.company:
            entry["companies"].add(r.company)
    families: dict[str, int] = defaultdict(int)
    for lead in leads.values():
        families[lead["family"]] += 1
    skills = []
    for std, e in per.items():
        by = {}
        for fam, (req, pref) in e["by"].items():
            by[fam] = [len(req), len(pref - req)]
        m = match.get(std) or {}
        skills.append(
            {
                "skill": std,
                "category": e["category"],
                "by": by,
                "status": m.get("status") or "unmapped",
                "evidence": m.get("evidence") or "",
                "close": m.get("close") or "",
                "companies": sorted(e["companies"])[:6],
            }
        )
    return {"leads": len(leads), "families": dict(families), "skills": skills}


_CSS = """
:root, [data-theme=dark] {
  color-scheme: dark;
  --bg:#16181d; --surface:#1e2128; --raised:#252932; --text:#c4c8ce; --muted:#8a919b;
  --border:#343a44; --link:#82aee0; --badge-text:#16181d;
  --met:#6fae7f; --port:#6f9fc9; --partial:#c9a35a; --gap:#d47a76; --unmapped:#6b727c;
}
[data-theme=light] {
  color-scheme: light;
  --bg:#e6e1d8; --surface:#efeae1; --raised:#e9e4da; --text:#2e2c29; --muted:#625d55;
  --border:#c8c0b3; --link:#2f5a92; --badge-text:#f3efe7;
  --met:#3d7a4c; --port:#2f5f8f; --partial:#8a6420; --gap:#a8433f; --unmapped:#8c867c;
}
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; color: var(--text); background: var(--bg); }
header { position: sticky; top: 0; background: var(--surface); border-bottom: 1px solid var(--border); padding: 12px 24px; z-index: 2; }
.title-row { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
h1 { font-size: 18px; margin: 0; } h2 { font-size: 15px; margin: 28px 0 6px; }
.sub { color: var(--muted); font-size: 12px; margin: 2px 0 0; }
.controls { display: flex; flex-wrap: wrap; gap: 6px 14px; align-items: center; margin-top: 10px; }
.group { display: flex; flex-wrap: wrap; gap: 4px; align-items: center; }
.group > span { color: var(--muted); font-size: 12px; margin-right: 2px; }
.chip, #theme { border: 1px solid var(--border); border-radius: 999px; padding: 2px 10px; background: var(--raised); color: var(--text); cursor: pointer; font: inherit; font-size: 12px; }
.chip[aria-pressed=true] { background: var(--text); color: var(--bg); border-color: var(--text); }
main { padding: 8px 24px 48px; max-width: 1200px; }
.cards { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 12px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 10px 14px; min-width: 150px; }
.card b { display: block; font-size: 22px; }
.card span { color: var(--muted); font-size: 12px; }
.legend { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--muted); margin: 6px 0; }
.sw { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
.bars { display: grid; grid-template-columns: minmax(150px, 220px) 1fr 54px; gap: 3px 10px; align-items: center; }
.bars .name { text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; cursor: default; }
.track { background: var(--raised); border-radius: 3px; height: 14px; position: relative; }
.fill { height: 100%; border-radius: 3px; }
.fill.pref { opacity: 0.45; position: absolute; top: 0; }
.pct { color: var(--muted); font-size: 12px; }
.met_client { background: var(--met); } .met_portfolio { background: var(--port); }
.partial { background: var(--partial); } .gap { background: var(--gap); } .unmapped { background: var(--unmapped); }
table { width: 100%; border-collapse: collapse; background: var(--surface); border: 1px solid var(--border); font-size: 13px; }
th, td { text-align: left; vertical-align: top; padding: 5px 9px; border-top: 1px solid var(--border); }
th { background: var(--raised); cursor: pointer; white-space: nowrap; }
td.num { text-align: right; white-space: nowrap; }
.badge { display: inline-block; font-size: 11px; font-weight: 700; color: var(--badge-text); border-radius: 4px; padding: 0 6px; white-space: nowrap; }
.muted { color: var(--muted); }
input[type=search] { padding: 4px 9px; border: 1px solid var(--border); border-radius: 6px; width: 240px; font: inherit; color: var(--text); background: var(--raised); }
"""

_THEME_HEAD_JS = """
try { document.documentElement.dataset.theme = localStorage.getItem('skills-theme') || 'dark'; }
catch (e) { document.documentElement.dataset.theme = 'dark'; }
"""

_JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const LABEL = {met_client: 'Met (client work)', met_portfolio: 'Met (portfolio only)', partial: 'Partly met', gap: 'Gap', unmapped: 'Not mapped yet'};
const state = {fam: 'all', cat: 'all', soft: false, pref: true, q: '', sort: 'demand'};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
function leadsIn() { return state.fam === 'all' ? D.leads : (D.families[state.fam] || 0); }
function counts(s) {
  let req = 0, pref = 0;
  for (const [fam, [r, p]] of Object.entries(s.by)) if (state.fam === 'all' || fam === state.fam) { req += r; pref += p; }
  return {req, pref, demand: req + (state.pref ? 0.5 * pref : 0)};
}
function visible() {
  const n = leadsIn() || 1;
  return D.skills.map((s) => ({...s, ...counts(s)}))
    .filter((s) => s.req + (state.pref ? s.pref : 0) > 0)
    .filter((s) => state.cat === 'all' || s.category === state.cat)
    .filter((s) => state.soft || s.category !== 'soft')
    .map((s) => ({...s, pctReq: 100 * s.req / n, pctPref: 100 * s.pref / n}))
    .sort((a, b) => b.demand - a.demand || a.skill.localeCompare(b.skill));
}
function bars(list, el) {
  const max = Math.max(1, ...list.map((s) => s.pctReq + (state.pref ? s.pctPref : 0)));
  el.innerHTML = list.map((s) => {
    const w = 100 * s.pctReq / max, wp = state.pref ? 100 * s.pctPref / max : 0;
    const tip = `${s.skill}: required by ${s.req}, preferred by ${s.pref} leads. ${LABEL[s.status]}${s.evidence ? ' — ' + s.evidence : ''}`;
    return `<div class="name" title="${esc(tip)}">${esc(s.skill)}</div>
      <div class="track" title="${esc(tip)}"><div class="fill ${s.status}" style="width:${w}%"></div>
      ${wp ? `<div class="fill pref ${s.status}" style="left:${w}%;width:${wp}%"></div>` : ''}</div>
      <div class="pct">${(s.pctReq + (state.pref ? s.pctPref : 0)).toFixed(0)}%</div>`;
  }).join('');
}
function render() {
  const list = visible();
  const n = leadsIn();
  const total = list.reduce((a, s) => a + s.demand, 0) || 1;
  const share = (st) => 100 * list.filter((s) => st.includes(s.status)).reduce((a, s) => a + s.demand, 0) / total;
  document.getElementById('cards').innerHTML = [
    [n, 'leads analyzed'], [list.length, 'distinct skills'],
    [share(['met_client']).toFixed(0) + '%', 'of demand met by client work'],
    [share(['met_portfolio']).toFixed(0) + '%', 'met by portfolio only'],
    [share(['partial']).toFixed(0) + '%', 'partly met'], [share(['gap']).toFixed(0) + '%', 'gaps'],
  ].map(([b, t]) => `<div class="card"><b>${b}</b><span>${t}</span></div>`).join('');
  bars(list.slice(0, 40), document.getElementById('top'));
  bars(list.filter((s) => s.status === 'gap').slice(0, 25), document.getElementById('gaps'));
  const wins = list.filter((s) => s.status === 'partial' || s.status === 'met_portfolio').slice(0, 25);
  document.getElementById('wins').innerHTML = wins.map((s) => `<tr><td>${esc(s.skill)}</td>
    <td><span class="badge ${s.status}">${LABEL[s.status]}</span></td><td class="num">${s.req}</td>
    <td class="muted">${esc(s.evidence)}</td><td>${esc(s.close)}</td></tr>`).join('');
  const q = state.q.toLowerCase();
  const rows = list.filter((s) => !q || (s.skill + ' ' + s.evidence + ' ' + s.companies.join(' ')).toLowerCase().includes(q));
  const key = {skill: (a, b) => a.skill.localeCompare(b.skill), status: (a, b) => a.status.localeCompare(b.status) || b.demand - a.demand,
    category: (a, b) => a.category.localeCompare(b.category) || b.demand - a.demand, demand: (a, b) => b.demand - a.demand}[state.sort];
  document.getElementById('all').innerHTML = rows.sort(key).map((s) => `<tr><td>${esc(s.skill)}</td><td class="muted">${esc(s.category)}</td>
    <td class="num">${s.req}</td><td class="num">${s.pref}</td><td class="num">${s.pctReq.toFixed(1)}%</td>
    <td><span class="badge ${s.status}">${LABEL[s.status]}</span></td><td class="muted">${esc(s.evidence || s.close)}</td>
    <td class="muted">${esc(s.companies.join(', '))}</td></tr>`).join('');
}
function chips(id, key, values) {
  const el = document.getElementById(id);
  el.innerHTML += values.map(([v, t]) => `<button class="chip" data-v="${v}" aria-pressed="${state[key] === v}">${t}</button>`).join('');
  el.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    state[key] = b.dataset.v;
    el.querySelectorAll('button').forEach((x) => x.setAttribute('aria-pressed', x === b));
    render();
  });
}
const famNames = {software: 'Software', full_stack: 'Full stack', backend: 'Backend', frontend: 'Frontend', data: 'Data', ml_ai: 'ML/AI', platform: 'Platform', other: 'Other'};
chips('fam', 'fam', [['all', 'All'], ...Object.keys(D.families).sort((a, b) => D.families[b] - D.families[a]).map((f) => [f, `${famNames[f] || f} (${D.families[f]})`])]);
chips('cat', 'cat', [['all', 'All'], ...D.categories.filter((c) => c !== 'soft').map((c) => [c, c.replace('_', '/')])]);
document.getElementById('soft').addEventListener('change', (e) => { state.soft = e.target.checked; render(); });
document.getElementById('pref').addEventListener('change', (e) => { state.pref = e.target.checked; render(); });
document.getElementById('q').addEventListener('input', (e) => { state.q = e.target.value; render(); });
document.querySelectorAll('th[data-sort]').forEach((th) => th.addEventListener('click', () => { state.sort = th.dataset.sort; render(); }));
document.getElementById('theme').addEventListener('click', () => {
  const t = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem('skills-theme', t); } catch (e) {}
});
render();
"""


def render_html(agg: dict[str, Any], *, generated: str) -> str:
    data = dict(agg, categories=list(CATEGORIES))
    legend = "".join(
        f'<span><i class="sw {k}"></i>{v}</span>'
        for k, v in (
            ("met_client", "Met (client work)"),
            ("met_portfolio", "Met (portfolio only)"),
            ("partial", "Partly met"),
            ("gap", "Gap"),
        )
    )
    payload = json.dumps(data).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Skills analysis</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>{_THEME_HEAD_JS}</script><style>{_CSS}</style></head>
<body>
<header>
  <div class="title-row"><div><h1>Skills analysis</h1>
  <p class="sub">What wheelhouse leads ask for, and what you've shown. Generated {html.escape(generated)}.</p></div>
  <button id="theme" type="button">Light / dark</button></div>
  <div class="controls">
    <div class="group" id="fam"><span>Role:</span></div>
    <div class="group" id="cat"><span>Category:</span></div>
    <label class="muted"><input type="checkbox" id="pref" checked> Count preferred (half weight)</label>
    <label class="muted"><input type="checkbox" id="soft"> Include soft skills</label>
  </div>
</header>
<main>
  <div class="cards" id="cards"></div>
  <h2>Most requested skills</h2>
  <div class="legend">{legend}<span>Solid bar: required · faded: preferred · % of leads</span></div>
  <div class="bars" id="top"></div>
  <h2>Most requested skills you lack</h2>
  <div class="bars" id="gaps"></div>
  <h2>Closest wins: in demand, partly shown</h2>
  <table><thead><tr><th>Skill</th><th>Status</th><th>Leads requiring</th><th>What you have</th><th>How to close it</th></tr></thead>
  <tbody id="wins"></tbody></table>
  <h2>All skills <input type="search" id="q" placeholder="Filter skills, evidence, companies"></h2>
  <table><thead><tr><th data-sort="skill">Skill</th><th data-sort="category">Category</th><th data-sort="demand">Required</th>
  <th>Preferred</th><th data-sort="demand">% of leads</th><th data-sort="status">Status</th><th>Evidence / how to close</th><th>Example companies</th></tr></thead>
  <tbody id="all"></tbody></table>
</main>
<script type="application/json" id="data">{payload}</script>
<script>{_JS}</script>
</body></html>
"""
