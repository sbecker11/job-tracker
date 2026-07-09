# Primer: JDs → Scores → Targeted Résumé/Cover-Letter Packages

**Goal:** starting from nothing but a Gmail inbox, end up with a list of real
job leads, each scored against your JD Match Framework, and — for the ones
you decide to pursue — a tailored `.docx` résumé + cover letter pair.

This is the existing four-stage pipeline in this repo, run in the right
order, plus a fifth, higher-stakes command (`triage_recruiter_inbox.py`) that
collapses stages 1-4 into one same-session decision per email and also
relabels/archives the source message. Stages 1-4 are separate commands so a
human stays in the loop before any money gets spent or any document gets
generated for a lead nobody's actually decided to pursue; Stage 5 is the
opposite trade-off — full automation for the curated recruiter-inbox
account, in exchange for real spend on every message it touches.

```
Stage 1 (free)      Stage 2 (~$0.02-0.04/lead)   Stage 3 (free)   Stage 4 (~$0.10-0.30/lead)
Gmail inbox    →    keyword score   →   LLM score   →   review   →   docx package
run_pipeline.py      (same command)     evaluate_backlog.py  list_leads.py   apply_package.py
                                                                              (one lead at a time,
                                                                               by design — see below)

Stage 5 (~$0.10-0.30/msg, fully automatic — see "Triage the recruiter inbox" in README.md)
Category/recruiter_job inbox mail  →  LLM score + auto-generate on pursue  →  relabel + archive
                              triage_recruiter_inbox.py (requires gmail.modify OAuth, one-time consent)
```

---

## Prerequisites (one-time, already done in this environment)

- Gmail OAuth for `shawnbecker.recruiting@gmail.com`: `~/.config/job-tracker/credentials.json` + `token.json` — present.
- Optional second account `personal_hub` (`scbboston@gmail.com`, reads `Category/recruiter_job` mail comms-migration's classifier applies there): `~/.config/job-tracker/personal_hub/` — present.
- `ANTHROPIC_API_KEY` in `.env` — required for Stage 2 and Stage 4 (both call the LLM). Without it, only Stage 1's keyword scoring works.
- Candidate profile / JD Match Framework: `~/CLAUDE.md` (dealbreakers, skills vocabulary, banned terms, contact info) and `config/framework.yaml` (the same framework, machine-readable, used by the keyword scorer). Keep these two in sync if the framework changes.
- `var/leads.db` — SQLite dedup store, created automatically on first run. Already has real data from prior runs.

Activate the venv first: `source .venv/bin/activate` (from the repo root).

---

## Stage 1 — Discover & keyword-score leads from the inbox

```bash
# Recruiting funnel (default account), last 30 days, up to 50 messages
python scripts/run_pipeline.py --dry-run --newer-than 30 --limit 50

# Also pick up recruiter_job mail comms-migration routed to the personal hub
python scripts/run_pipeline.py --dry-run --account personal_hub \
  --query "label:Category/recruiter_job is:unread" --limit 20
```

- `--dry-run` here means "read Gmail read-only" (doesn't mark messages read/archived) — it still fully classifies, extracts, resolves JDs against live ATS boards, keyword-scores, and **writes to `var/leads.db`**. This is the normal way to run it.
- Add `--offline` to skip live ATS lookups and score against the email body/snippet only (faster, no external calls, less accurate).
- Add `--llm-fallback` to also ask the LLM to extract (company, title) pairs from digest emails the regex parser can't confidently handle (cached per message — re-running never re-bills a message).
- Every new/updated lead lands in `job_leads` with a `match_pct` + `verdict` (`pursue`/`review`/`pass`) from the **keyword** scorer (`scorer.py`) — cheap, deterministic, no LLM involved yet.

## Stage 2 — Deepen scoring with the LLM (worth it — the keyword scorer produces false "pass"es the LLM catches as genuine "pursue"s)

```bash
# Preview what would be evaluated, no API calls
python scripts/evaluate_backlog.py --dry-run

# Actually run it (defaults to every lead with jd_text that hasn't been LLM-scored yet)
python scripts/evaluate_backlog.py

# Cap spend on a big backlog
python scripts/evaluate_backlog.py --limit 20
```

- Only touches leads with `jd_text` on file that don't yet have an `llm_verdict` — safe to re-run anytime, never re-bills an already-evaluated lead.
- Writes `llm_verdict`, `llm_match_pct`, `llm_dealbreaker_notes`, `llm_skills_alignment`, `llm_rationale` onto the same row, alongside (not overwriting) the keyword scorer's columns.
- Ends with a `=== PURSUE ===` / `=== REVIEW ===` printout and the exact next command for each.

## Stage 3 — Review scored leads

```bash
python scripts/list_leads.py --verdict pursue
python scripts/list_leads.py --verdict review
python scripts/list_leads.py --company "Acme" --title "Software Engineer"   # one lead, full detail
python scripts/list_leads.py --verdict pursue --csv ~/Desktop/job_leads.csv  # spreadsheet pass
```

This is where you decide, as a human, which leads are actually worth a
generated package — Stage 4 is deliberately **not** automatic for every
`pursue` verdict (see the docstring in
`src/job_tracker/cli/evaluate_backlog.py`): silently generating a résumé +
cover letter for everything the model calls "pursue" would spend real money
on leads you might reject at a glance (bad location, stale posting, etc.).

## Stage 4 — Generate the targeted docx package (one lead at a time, by design)

```bash
python scripts/apply_package.py --company "Acme Corp" --title "Senior Software Engineer"
```

- Re-evaluates that one lead's `jd_text` with the LLM (JD Match Framework) — always saving the JD text and the LLM review — and **only on a "pursue" verdict**, additionally renders a tailored résumé (`.docx`) and cover letter (`.docx`).
- Output location: everything for one lead lands together in `~/Desktop/Resumes/2026/<Company>_<Title>/` (override the root with `--output-root`) — `JobDescription.docx`, `LLM_Review.docx`, and on a pursue verdict `Shawn_Becker_Resume_<Company>_<Title>.docx` + `Shawn_Becker_Cover_Letter_<Company>_<Title>.docx`.
- Both documents are generated from the candidate profile in `~/CLAUDE.md` (contact info, banned terms, positioning) — never hand-typed per lead.
- `--json` gives the full machine-readable result (verdict, match %, rationale, both file paths, token/cost/time metrics for both the evaluate and generate calls) if you're scripting around this.
- Optional `--comparison-jsonl <path>`: if you're tracking leads in a manual comparison JSONL file, this updates the matching company/title line with the result instead of leaving it a separate, disconnected artifact.

### Doing Stage 4 for several leads in one sitting

There's no single batch command for this (intentionally — see above), but
looping over a reviewed list is one line:

```bash
python scripts/list_leads.py --verdict pursue --json | \
  python3 -c "
import json, subprocess, sys
leads = json.load(sys.stdin)
for lead in leads:
    subprocess.run([
        'python', 'scripts/apply_package.py',
        '--company', lead['company'], '--title', lead['title'],
    ])
"
```

Run this against a list you've already eyeballed via Stage 3 — not blindly
against every `pursue` verdict the moment it appears.

---

## End-to-end example (fresh backlog, recruiting funnel only)

```bash
source .venv/bin/activate
python scripts/run_pipeline.py --dry-run --newer-than 30 --limit 50
python scripts/evaluate_backlog.py
python scripts/list_leads.py --verdict pursue
# ... eyeball the list, pick the real ones ...
python scripts/apply_package.py --company "<company>" --title "<title>"
```

## Cost/safety summary

| Stage | Touches | Cost |
|---|---|---|
| 1 (`run_pipeline.py`) | Gmail (read-only), live ATS boards (unless `--offline`) | Free, unless `--llm-fallback` (cheap, cached per message) |
| 2 (`evaluate_backlog.py`) | Anthropic API, one call per un-evaluated lead | ~$0.02–0.04/lead (Haiku) |
| 3 (`list_leads.py`) | Local SQLite only | Free |
| 4 (`apply_package.py`) | Anthropic API, evaluate + generate calls | ~$0.10–0.30/lead (Sonnet), only on a pursue verdict |
| 5 (`triage_recruiter_inbox.py`) | Gmail (**read/write** — `gmail.modify`), Anthropic API | ~$0.02–0.04/message (evaluate), +~$0.10–0.30 more only on a pursue verdict; relabels + archives the message |

Nothing in this pipeline applies, replies to, or sends anything to an
employer on your behalf — it only surfaces, scores, and drafts documents for
you to review and use yourself.
