# job-tracker

Job-search **automation pipeline** for Shawn Becker's recruiting funnel.

> **Just want to run it?** See [`PRIMER.md`](PRIMER.md) for the end-to-end
> command sequence: inbox → scored leads → targeted résumé/cover-letter
> packages. Everything below is the fuller reference.

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
| `src/job_tracker/pipeline/extract.py` | Fan-out: (company, title) roles from SINGLE_JD / MULTI_JD_IN_BODY messages (regex-based, first pass) |
| `src/job_tracker/pipeline/llm_extract.py` | Opt-in LLM extraction fallback (Anthropic API) for digests the regex pass can't confidently parse |
| `src/job_tracker/scoring/scorer.py` | Dealbreaker sweep + skills alignment match % (CLAUDE.md §10, keyword heuristic v1) |
| `src/job_tracker/pipeline/llm_apply.py` | LLM JD evaluation (CLAUDE.md §10 framework) + résumé/cover-letter generation (Anthropic API), with token/time/cost tracking per call |
| `src/job_tracker/pipeline/store.py` | SQLite dedup store (`var/leads.db`, gitignored) |
| `src/job_tracker/pipeline/run.py` | Orchestrator: classify → extract → resolve → score → store |
| `src/job_tracker/cli/list_leads.py` | Review/export/update stored leads without re-running the pipeline |
| `src/job_tracker/cli/apply_package.py` | Evaluate one stored lead + generate résumé/cover letter on a pursue verdict (`apply-package`) |
| `src/job_tracker/pipeline/triage.py` | Classify → extract → resolve → LLM-evaluate (+ auto-generate on pursue) for one recruiter-inbox message, deciding an ACCEPT/DENY/NEEDS_REVIEW outcome — never touches Gmail or the DB itself |
| `src/job_tracker/email/gmail_writer.py` | The only place in this repo that writes to Gmail — labels a message `JobTracker/ACCEPT\|DENY\|NEEDS_REVIEW` and archives it |
| `src/job_tracker/cli/triage_recruiter_inbox.py` | Runs `pipeline/triage.py` over `Category/recruiter_job` inbox mail, persists leads + the message outcome, and relabels/archives via `gmail_writer.py` (`triage-recruiter-inbox`) |
| `config/framework.yaml` | Dealbreakers + skills vocabulary, transcribed from `~/CLAUDE.md` |

## Setup

```bash
cd job-tracker
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"
```

Optional: copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` if you
plan to use `--llm-fallback` (see below). `.env` is loaded automatically at
startup (via `python-dotenv`, wired in `job_tracker/__init__.py`) and is
git-ignored.

```bash
cp .env.example .env
# then edit .env and paste in your key
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

### Optional: also read `scbboston@gmail.com` (personal hub)

Recruiter/job mail sometimes lands in the personal hub instead of the
recruiting funnel. [`comms-migration`](https://github.com/sbecker11/comms-migration)'s
`classifier/` labels that mail `Category/recruiter_job` there and archives
it out of the inbox (as of 2026-07-04) — this repo polls by label, not
inbox location (`is:unread` with no `in:inbox`), so archiving on the
comms-migration side never hides anything from this pipeline. It picks up
the exact same classify → extract → resolve → score → store pipeline
without ever touching the rest of your personal inbox, and with no manual
forwarding step.

This needs its own one-time OAuth consent for `scbboston@gmail.com`
(job-tracker only ever requests read-only access, so it can't reuse
comms-migration's broader `gmail.modify` token even for the same account):

```bash
mkdir -p ~/.config/job-tracker/personal_hub
cp ~/Downloads/client_secret_*.json ~/.config/job-tracker/personal_hub/credentials.json
# First run opens a browser — sign in as scbboston@gmail.com.
python scripts/run_pipeline.py --dry-run --account personal_hub \
  --query "label:Category/recruiter_job is:unread" --limit 20
```

Run comms-migration's classifier first (or on a schedule) so the label
exists before job-tracker polls it — this repo never applies that label
itself.

### Re-authenticating when a login expires (expect this ~weekly)

This OAuth app is in Google's **"Testing" publishing status** (unverified —
that's the "Google hasn't verified this app" screen you see on every login).
Google hard-expires refresh tokens issued to Testing-status apps **after 7
days, regardless of use**. This is not a bug and not specific to one
account — every account authenticated against this app (recruiting funnel,
`personal_hub`, or any future one) will need to be re-authenticated on this
cadence until/unless the app is moved to "In production" status.

**You don't need to do anything manually to prepare for this.** When a
cached token has expired, the next `run_pipeline.py` (or any Gmail-backed)
command will print something like:

```
Cached Gmail token for 'personal_hub' is no longer valid (...). Re-opening
browser for a fresh login — this is expected roughly weekly while this app
is in Google's 'Testing' publishing status ...
```

and a browser window opens automatically for a fresh login — same as the
very first time you authenticated that account. Sign in as the **same**
Gmail account named in the message, click through the "unverified app"
warning the same way you did originally, approve access, and the command
continues normally. There's no need to delete `token.json` by hand first;
the code detects the failed refresh and re-opens the login flow for you.

### Write access for the triage flow (`triage_recruiter_inbox.py` only)

Every command above is read-only. `triage_recruiter_inbox.py` is the one
exception — it relabels (`JobTracker/ACCEPT|DENY|NEEDS_REVIEW`) and archives
messages on the default recruiting-funnel account, which needs the broader
`gmail.modify` scope. This is a **separate scope and a separate cached
token** (`token_modify.json`, next to the existing `token.json`) — it reuses
the same `credentials.json` OAuth client, but needs its own one-time consent
screen the first time you run the triage CLI without `--dry-run`, since a
readonly token never gains write permission just because a wider scope is
requested later. `--dry-run` (scoring only, no Gmail mutation) keeps using
the existing readonly token and needs no new consent.

## Triage the recruiter inbox (score, auto-generate, relabel + archive)

```bash
# Preview: LLM-score every unlabeled Category/recruiter_job message, generate
# packages for pursue verdicts, but never touch Gmail or the DB
python scripts/triage_recruiter_inbox.py --dry-run --limit 10

# For real: also relabels JobTracker/ACCEPT|DENY|NEEDS_REVIEW and archives
# (first run without --dry-run opens the gmail.modify consent screen above)
python scripts/triage_recruiter_inbox.py --limit 10

# Score only, never spend on résumé/cover-letter generation even on a pursue verdict
python scripts/triage_recruiter_inbox.py --no-generate
```

- Only looks at mail comms-migration has already labeled
  `Category/recruiter_job` on the recruiting-funnel inbox, and skips
  anything already triaged in a prior run (tracked in `processed_messages`
  and by the `JobTracker/*` label itself — never double-billed, never
  double-labeled).
- Always uses the LLM Match Framework (`pipeline/llm_apply.py`, CLAUDE.md
  §10) to score, never the free keyword scorer — the whole point is a
  same-session decision confident enough to actually relabel and archive
  the source email.
- **ACCEPT** (at least one extracted role scored "pursue"): generates a
  tailored résumé + cover letter automatically (unless `--no-generate`),
  advances the lead to `package_generated` (or `approved` with
  `--no-generate`), and archives the message under `JobTracker/ACCEPT`.
- **DENY** (classified noise/rejection, or every extracted role scored
  "pass"): advances the lead to `passed` and archives under
  `JobTracker/DENY` — no Anthropic spend beyond the one evaluate call per
  role.
- **NEEDS_REVIEW** (recruiter outreach with no JD to score, an
  unparseable/incomplete extraction, or a "review" verdict): archived under
  `JobTracker/NEEDS_REVIEW` for a human look; nothing in `job_leads`
  auto-advances past `new`.
- From here, real-world progress (`applied`, `interviewing`, `offered`,
  `accepted`, `started`) is reported by hand — see `list_leads.py
  --set-status` above.

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

**Job-board / aggregator digests (LinkedIn, Lensa, Talent.com, Indeed,
Adzuna, Robert Half, corporate "search agent" ATS notifications, etc.):**
these senders are never trusted as the employer — the pipeline keeps a
denylist of job-board domains/names (`_JOB_BOARD_DOMAINS` /
`_JOB_BOARD_NAMES` in `pipeline/extract.py`) so e.g. "Adzuna" or "Ladders,
Inc." never gets stored as a fake hiring company. HTML mail is converted to
plain text with block-level structure preserved (paragraphs/list items keep
their own lines — see `htmltext.py`) rather than flattened to one run-on
line, which is what makes the following digest shapes parseable at all:
- **Search-agent digests** (e.g. jobs2web-style "Job Matches:" emails) —
  the real per-listing titles are parsed cleanly and paired with the sender
  company; the saved-search name itself (e.g. "Agent: Sr Software Engineer")
  is never mistaken for a posting.
- **Flattened "more details" digests** (Adzuna-style) — title, flags (TOP
  MATCH/NEW/REMOTE), and "Company - Location" each reliably land on their
  own line, so these are parsed into clean structured leads.
- **"Ref no.:" web-aggregation digests** (Energy Job Line / LinkedIn-style
  curation) — the title is reliably the first line of each listing and is
  extracted cleanly, but the following line is ambiguously either a company
  or a location depending on which site the aggregator pulled the posting
  from, so company is deliberately left blank rather than guessed. That
  routes it to `EXTRACTION NEEDS REVIEW`, grouped by source email with a
  few sample titles shown inline (`--json` has the full per-listing list)
  — enough to skim and manually pursue anything interesting.

**LLM extraction fallback (opt-in, `--llm-fallback`):** new digest shapes
keep showing up as the backlog gets processed, and hand-writing a new regex
parser for every sender's HTML layout doesn't scale. Rather than a
multi-agent system (overkill here — there's one job: turn one email into a
list of (company, title) pairs), `--llm-fallback` adds a single LLM call as
a second pass, used **only** for messages the regex pass in
`pipeline/extract.py` couldn't confidently finish (i.e. it found nothing, or
every candidate role is missing a company or title):

```bash
# Requires ANTHROPIC_API_KEY in .env (see Setup above)
python scripts/run_pipeline.py --dry-run --newer-than 30 --llm-fallback

# Override the model (defaults to claude-haiku-4-5 — fast + cheap)
python scripts/run_pipeline.py --dry-run --llm-fallback --llm-model claude-sonnet-5
```

How it stays cheap and safe:
- **One call per message, not per role** — a single digest can leave a dozen
  incomplete roles behind after the regex pass; the LLM re-reads the whole
  message once and returns every listing in one shot, rather than being
  called once per leftover role.
- **Cached per Gmail message id** (`llm_extraction_cache` table in
  `var/leads.db`) — re-running the pipeline over the same backlog, e.g. after
  a crash or to pick up newly-arrived mail, never re-bills a message it has
  already sent to the API, including messages where the honest answer was
  "no real postings here."
- **Strict no-fabrication prompt** — the model is told never to invent a
  company or title, and never to attribute a listing to the job board /
  ATS platform that sent the email (mirroring the `_JOB_BOARD_DOMAINS` /
  `_JOB_BOARD_NAMES` denylist the regex pass already uses). An empty or
  partial result is always preferred over a guess; a genuinely ambiguous
  listing still lands in `EXTRACTION NEEDS REVIEW` afterward, same as today.
- **Fails soft** — a network error, missing/invalid API key, or unparseable
  response is logged and treated as "no roles found" rather than crashing
  the run; the message falls back to the normal `EXTRACTION NEEDS REVIEW`
  bucket. A summary line reports how many messages triggered the fallback
  and how many of those it actually rescued.
- **Off by default** — plain `run-pipeline` (no flag) behaves exactly as
  before; this is purely additive.

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

# Narrow to one lead
python scripts/list_leads.py --company "Acme" --title "Software Engineer"

# Full detail (rationale, matched skills, full jd_text) as JSON
python scripts/list_leads.py --verdict pursue --json

# Print the full stored JD text for a specific lead (e.g. before writing a cover letter)
python scripts/list_leads.py --company "Acme" --title "Software Engineer" --show-jd-text

# Export everything to CSV for a spreadsheet pass
python scripts/list_leads.py --csv ~/Desktop/job_leads.csv

# Advance leads through their lifecycle (models.LEAD_STAGES) as real-world progress happens
python scripts/list_leads.py --verdict pursue --set-status approved
python scripts/list_leads.py --company "Acme" --title "Software Engineer" --set-status applied --on 2026-07-10
python scripts/list_leads.py --company "Acme" --title "Software Engineer" --set-status interviewing
```

**Stored JD text:** each lead's `jd_text` column keeps the full description
text the score was computed against — the real ATS posting when
`jd_resolved` is true, or the raw email body otherwise — so you can write a
tailored cover letter or application answer later without re-fetching
anything. It's kept as plain SQLite `TEXT` in the same row as everything
else (no separate file store); the text is stored with its original
paragraph/bullet-list structure intact rather than collapsed to one line,
since a JD is semi-structured (headers, responsibilities, requirements), not
a flat blob. Once a lead's `status` moves off `new` (e.g. to `approved`),
`jd_text` — like the rest of the scoring fields — stops being overwritten by
later re-sends of the same digest, so it won't quietly change out from under
you after you've started using it.

**Lifecycle stages (`models.LEAD_STAGES`):** `new` (unprocessed) → `approved`
(triage said pursue) → `package_generated` (résumé + cover letter rendered)
→ `applied` → `following_up` → `interviewing` → `offered` → `accepted` →
`started`, with `passed` as the off-ramp at any point. The triage flow
(below) stamps `approved`/`package_generated`/`passed` automatically;
everything from `applied` onward is real-world progress you report by hand
with `list_leads.py --set-status <stage> [--on <date>]`. Each stage past
`new` has its own `<stage>_at` timestamp column (e.g. `applied_at`), stamped
once and never overwritten by a later re-run, so the DB keeps a timeline —
not just a current state.

Leads persist in `var/leads.db` (gitignored — personal data) across runs, so
you can classify a batch, step away, and come back to review with
`list_leads.py` without touching the network again.

## Limits

- **Workday** has no clean public board API — expect misses; fallback TBD.
- **LinkedIn** is intentionally out of scope (no public retrieval API).
- Direct-email employers (e.g. careers@…) won't resolve via ATS boards.
