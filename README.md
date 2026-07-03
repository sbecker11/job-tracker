# job-tracker

Job-search **automation pipeline** for Shawn Becker's recruiting funnel.

**Routing truth** (where mail goes) lives in [comms-migration](https://github.com/sbecker11/comms-migration) — see [`routing-inventory.md`](https://github.com/sbecker11/comms-migration/blob/main/routing-inventory.md) for the four-into-one forward into `shawnbecker.recruiting@gmail.com`. **Processing** (what happens to that mail) lives here.

## Relationship to `comms-migration`

The split is deliberate and symmetric:

| Repo | Owns | Does NOT own |
|---|---|---|
| **comms-migration** | Routing truth: which hub/inbox a sender lands in, the four-into-one forward into `shawnbecker.recruiting@gmail.com`, contacts data, `rules/senders.yaml` | Reading, classifying, or acting on mail once it arrives |
| **job-tracker** (this repo) | Reading `shawnbecker.recruiting@gmail.com` via the Gmail API, email classification, ATS JD resolution, match scoring, the job tracker DB | Where mail is forwarded from, hub/contact routing decisions |

**Handoff point:** the recruiting funnel inbox (`shawnbecker.recruiting@gmail.com`).
`comms-migration` is the source of truth for *how mail gets there*; this repo is
the source of truth for *what happens once it's there* (Gmail API setup below,
classifier, pipeline).

If the recruiting funnel's source addresses ever change, that update happens in
`comms-migration`'s `routing-inventory.md` first — this repo just needs the
Gmail reader still pointed at the right inbox.

## Pipeline (target)

```
recruiting Gmail inbox
  → email classifier (single-JD / multi-JD / digest / outreach / rejection / noise)
  → fan-out: (company, title) pairs
  → ATS JD resolver (this repo)
  → framework scoring (dealbreakers, match %, pursue/pass)
  → job tracker DB (deduped roles)
```

## Components

| Path | Status |
|---|---|
| `src/job_tracker/ats/jd_resolver.py` | Public ATS board lookup (Greenhouse, Lever, Ashby, SmartRecruiters) |
| `src/job_tracker/email/` | Gmail reader + heuristic classifier |
| `src/job_tracker/pipeline/extract.py` | Fan-out: (company, title) roles from SINGLE_JD / MULTI_JD_IN_BODY messages |
| `src/job_tracker/scoring/scorer.py` | Dealbreaker sweep + skills alignment match % (CLAUDE.md §10, keyword heuristic v1) |
| `src/job_tracker/pipeline/store.py` | SQLite dedup store (`var/leads.db`, gitignored) |
| `src/job_tracker/pipeline/run.py` | Orchestrator: classify → extract → resolve → score → store |
| `src/job_tracker/cli/list_leads.py` | Review/export/update stored leads without re-running the pipeline |
| `config/framework.yaml` | Dealbreakers + skills vocabulary, transcribed from `~/Wisdom/CLAUDE.md` |

## Setup

```bash
cd job-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"
```

## Gmail setup (one-time)

Credentials live **outside the repo** at `~/.config/job-tracker/` (never pushed to GitHub).

1. Create the config directory:
   ```bash
   mkdir -p ~/.config/job-tracker
   ```
2. [Google Cloud Console](https://console.cloud.google.com/) → create project → enable **Gmail API**.
3. OAuth consent screen → add test user (`shawnbecker.recruiting@gmail.com`).
4. Credentials → **OAuth client ID** → Desktop app → download JSON.
5. Save the download as `~/.config/job-tracker/credentials.json`:
   ```bash
   cp ~/Downloads/client_secret_*.json ~/.config/job-tracker/credentials.json
   ```
6. Optional env helper (paths only, safe to copy):
   ```bash
   cp config/gmail.env.example ~/.config/job-tracker/env
   source ~/.config/job-tracker/env
   ```
7. First fetch opens a browser; token caches to `~/.config/job-tracker/token.json`.

## Classify recruiting inbox

```bash
pytest tests/test_classifier.py tests/test_gmail_reader.py -v
python scripts/classify_inbox.py --all-fixtures
python scripts/classify_inbox.py --dry-run --limit 5
python scripts/classify_inbox.py --message-id <GMAIL_MESSAGE_ID>
```

Use `--newer-than 7` during development to limit how far back unread search goes.

## Resolve a job description

```bash
python scripts/resolve_jd.py --selftest
python scripts/resolve_jd.py --company "Stripe" --title "Software Engineer" --verbose
python scripts/resolve_jd.py --company "Ancestry" --title "Senior Software Engineer" --json
```

Pin board tokens that guessing misses in `KNOWN_BOARDS` at the top of `jd_resolver.py`.

## Run the full pipeline (classify → extract → resolve → score → store)

This is the one-command version of "read the recruiting inbox and tell me
what's worth pursuing." It classifies each message, fans multi-role digests
out into individual (company, title) leads, tries to resolve the full JD from
the public ATS APIs, scores it against the JD Match Framework in
`config/framework.yaml` (dealbreaker sweep + skills alignment — see CLAUDE.md
§10), and dedups into `var/leads.db` (gitignored) so re-processing the same
inbox never creates duplicate rows.

```bash
# Offline dry run against the bundled fixtures — no network, no Gmail auth needed
python scripts/run_pipeline.py --all-fixtures --offline

# Against real Gmail (requires the one-time OAuth login below)
python scripts/run_pipeline.py --dry-run --newer-than 30 --limit 50

# Skip live ATS lookups (faster, scores against email body text only)
python scripts/run_pipeline.py --dry-run --newer-than 30 --offline

# Full JSON output (every lead, full rationale) for scripting/inspection
python scripts/run_pipeline.py --dry-run --newer-than 30 --json > /tmp/run.json
```

**Output buckets:** `PURSUE` (match % ≥ `pursue_min_pct`), `REVIEW` (borderline —
needs a human look), `PASS` (low match or a load-bearing dealbreaker hit),
`RECRUITER OUTREACH` (no JD to score — needs a reply, not a lead), and
`EXTRACTION NEEDS REVIEW` (couldn't confidently parse a company/title — check
manually rather than silently dropping it).

**Tuning the framework:** edit `config/framework.yaml` directly — dealbreakers,
skills vocabulary/weights, and the `pursue_min_pct` / `review_min_pct`
thresholds are all data, not code. Keep it in sync with `~/Wisdom/CLAUDE.md`
§3 (dealbreakers) and §8–9 (skills) when those change.

**Limitation (by design, v1):** matching is keyword-based, not an LLM read of
the JD — it's the "rule engine" layer from the runbook's architecture
(Appendix D). A JD using unlisted synonyms for a known skill won't match, and
"load-bearing" is approximated by mention count, not real emphasis. Nothing in
this pipeline applies, replies, or sends anything on your behalf — it only
surfaces and ranks leads for you to act on.

**Scale note:** when a batch pulls several roles from the same employer (a
3-role digest, or the same company showing up across multiple emails), the
pipeline fetches that company's ATS board once per run and reuses it for
every title — it does not refetch per role. ATS requests also get one retry
with backoff on a transient error or a 429. Still, be considerate running
against hundreds of backlog messages at once; `--limit` lets you work through
it in batches.

## Review stored leads (without re-running the pipeline)

```bash
# Table view
python scripts/list_leads.py --verdict pursue
python scripts/list_leads.py --verdict review

# Full detail (rationale, matched skills) as JSON
python scripts/list_leads.py --verdict pursue --json

# Export everything to CSV for a spreadsheet pass
python scripts/list_leads.py --csv ~/Desktop/job_leads.csv

# Mark leads you've decided to pursue so future runs don't re-suggest them the same way
python scripts/list_leads.py --verdict pursue --set-status pursuing
```

Leads persist in `var/leads.db` (gitignored — personal data) across runs, so
you can classify a batch, step away, and come back to review with
`list_leads.py` without touching the network again.

## Limits

- **Workday** has no clean public board API — expect misses; fallback TBD.
- **LinkedIn** is intentionally out of scope (no public retrieval API).
- Direct-email employers (e.g. careers@…) won't resolve via ATS boards.
