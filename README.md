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

A third sibling, [`recruiting-automation`](https://github.com/sbecker11/recruiting-automation),
owns neither routing nor processing — it's the `launchd`-scheduled wrapper
that runs `comms-migration`'s classifier and this repo's pipeline together
hourly, unattended, with its own halt/resume safety net.

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
| `src/job_tracker/cli/no_llm_review.py` | Print (and optionally rewrite) the deterministic rule-based review for one lead — verdict, match %, passed/failed rules; no LLM (`no-llm-review`) |
| `src/job_tracker/cli/apply_package.py` | Evaluate one stored lead + generate résumé/cover letter on a pursue verdict (`apply-package`) |
| `src/job_tracker/pipeline/triage.py` | Classify → extract → resolve → LLM-evaluate (+ auto-generate on pursue) for one recruiter-inbox message, deciding a PURSUE/SKIP/NEEDS_REVIEW outcome — never touches Gmail or the DB itself |
| `src/job_tracker/email/gmail_writer.py` | The only place in this repo that writes to Gmail — labels a message `JobTracker/PURSUE\|SKIP\|NEEDS_REVIEW` (`Category/recruiter_job`) or `JobTracker/Linked\|NeedsFollowup` (`Category/social` replies, since 2026-07-19) and archives it |
| `src/job_tracker/cli/triage_recruiter_inbox.py` | Runs `pipeline/triage.py` over `Category/recruiter_job` inbox mail, persists leads + the message outcome, and relabels/archives via `gmail_writer.py` (`triage-recruiter-inbox`) |
| `src/job_tracker/cli/add_job.py` | Interactively add a job that didn't come from a triaged email (`add-job`) |
| `src/job_tracker/cli/log_contact.py` | Log a manual conversation or meeting/interview against an existing job (`log-contact`) |
| `src/job_tracker/cli/attach_document.py` | Attach a local file or URL (signed RTR, NDA, JD PDF, etc.) to an existing job (`attach-document`) |
| `src/job_tracker/cli/list_contacts.py` | Report every tracked contact across all jobs — name, company, role, phone, email (`list-contacts`) |
| `src/job_tracker/cli/generate_message.py` | Draft a thank-you or status-check-in follow-up email via the same LLM pipeline used for résumés/cover letters (`generate-message`) |
| `src/job_tracker/pipeline/comms_match.py` | Tiered matching (thread id → contact email → opt-in LLM extraction) that attaches a communication to the right job |
| `src/job_tracker/cli/scan_communications.py` | Archives LinkedIn message replies (and, with `--include-sent`, your own Sent-folder replies) that `triage_recruiter_inbox.py` never sees, and labels/archives the inbound ones `JobTracker/Linked` or `JobTracker/NeedsFollowup` (`scan-communications`) |
| `src/job_tracker/cli/resolve_communication.py` | Manually resolve a parked `unmatched_messages` row onto a real (or brand-new) job (`resolve-communication`) |
| `src/job_tracker/cli/export_communications.py` | Render one job's full communications history to an on-demand PDF (`export-communications`) |
| `src/job_tracker/cli/process_awaiting_llm_review.py` | Sweeps every lead whose free rule-based score cleared the LLM-review gate but has no `llm_verdict` yet (most often a `scan_communications.py` stub lead) and runs the same two-tier review `apply_package.py` runs by hand (`process-awaiting-llm-review`) |
| `src/job_tracker/cli/resync_labels.py` | Re-syncs a message's `JobTracker/PURSUE\|SKIP\|NEEDS_REVIEW` label to its linked lead(s)' CURRENT verdict, catching drift from a later LLM review or manual status change (`resync-labels`) |
| `config/framework.yaml` | Dealbreakers, `not_dealbreakers` (e.g. W2-only, US citizen / no sponsorship), + skills vocabulary — transcribed from `~/CLAUDE.md` |
| `docs/JOB_CRM_VISION.md` | Design doc: Job as a first-class object — contacts, conversations, documents, meetings, offers, and the 5 use cases (dedupe alerts, follow-ups, offer comparison, market-withdrawal notice) this is building toward |
| `docs/CATEGORY_HANDLER_EXTENSIBILITY.md` | Design doc: how the classify → decide → label/archive pattern generalizes beyond `recruiter_job` to future comms-migration categories |

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

### Historical backlog: job-lead mail from before the recruiting funnel existed

The recruiting funnel's three source-address forwards (see
[`routing-inventory.md`](https://github.com/sbecker11/comms-migration/blob/main/routing-inventory.md))
were only confirmed working as of 2026-07-04 — anything that arrived before
then never reached `shawnbecker.recruiting@gmail.com`. Two different gaps,
two different fixes:

1. **Mail that landed on `scbboston@gmail.com` (personal hub) directly** —
   already sitting in Gmail, just never classified/triaged. Fully
   recoverable with the tools above, run against a wider window:

   ```bash
   # comms-migration: label historical mail (any age, any inbox/archive
   # state — dropping the default in:inbox + 365-day cutoff via --query/
   # --newer-than 0) as Category/recruiter_job, etc.
   python scripts/run_classifier.py --account personal_hub --dry-run \
     --query "after:2026/1/1" --newer-than 0   # preview first
   python scripts/run_classifier.py --account personal_hub \
     --query "after:2026/1/1" --newer-than 0   # then for real

   # job-tracker: triage what just got labeled. --account needs its own
   # one-time gmail.modify consent for personal_hub the first time you
   # drop --dry-run (separate from comms-migration's own personal_hub
   # token — see "Write access" below). DEFAULT_QUERY itself dropped
   # `in:inbox` on 2026-07-18 (see triage_recruiter_inbox.py's module
   # docstring — 374 already-archived recruiter_job messages were a silent
   # dead end under the old in:inbox-scoped default), so this --query
   # override below no longer needs to say so explicitly either; the
   # after:2026/1/1 + explicit label exclusions are still worth keeping for
   # a historical-backlog run like this one.
   python scripts/triage_recruiter_inbox.py --account personal_hub --dry-run \
     --query "label:Category/recruiter_job after:2026/1/1 -label:JobTracker/PURSUE -label:JobTracker/SKIP -label:JobTracker/NEEDS_REVIEW"
   python scripts/triage_recruiter_inbox.py --account personal_hub \
     --query "label:Category/recruiter_job after:2026/1/1 -label:JobTracker/PURSUE -label:JobTracker/SKIP -label:JobTracker/NEEDS_REVIEW"
   ```

2. **Mail that landed at one of the three forwarding *source* addresses**
   (`shawn.becker@spexture.com` / Hostinger, `scb_boston@yahoo.com` /
   `shawn.becker@yahoo.com` / Yahoo, `sbecker@alum.mit.edu` / MIT alumni
   Outlook) **before its forward was set up** — this is the harder gap.
   That mail never reached *any* Gmail account job-tracker or
   comms-migration can read via the Gmail API; it's sitting (if it still
   exists) in Hostinger webmail, Yahoo Mail, or the MIT-hosted Outlook
   mailbox, none of which this pipeline has API access to.
   `routing-inventory.md` already flags this as an open "historical
   backlog risk." There's no automated fix here — check each mailbox by
   hand for the Jan–Jul 2026 window and, for anything found, either
   forward it into `shawnbecker.recruiting@gmail.com` (it'll flow through
   the normal pipeline from there) or record it directly with
   `scripts/add_job.py` (see "Manual (non-email) job management CLIs"
   below) if the original message itself isn't worth chasing down.

### Re-authenticating when a login expires (should be rare now — see below)

**Status as of 2026-07-13: this OAuth app (`job-tracker-desktop`, Google
Cloud project `job-tracker-500901`) is published "In production."** It was
in Google's "Testing" publishing status until then, which hard-expires
refresh tokens after 7 days regardless of use — that was the cause of the
recurring weekly re-auth prompts. Moving to "In production" removes that
7-day cap entirely; **this does not require Google's full verification
process** (that's a separate, optional step — see the "Submit for
verification" banner note below) even though `gmail.readonly`/`gmail.modify`
are "restricted" scopes. All 5 token grants across this repo and the
sibling `comms-migration` repo (which shares this same OAuth client) were
force-refreshed under the new production policy the same day — see that
repo's README for the full account/scope breakdown. Old pre-production
tokens were backed up to `~/tmp/oauth-tokens-backup-20260713-182409/` before
being replaced, in case anything needs to be cross-checked later.

If Google ever shows an "unverified app" click-through on a *future* new
account/scope grant, that's expected and harmless — publishing to
production and submitting for Google's optional verification are
independent; you can always ignore the "submit for verification" nudge for
a personal tool like this one.

**If you ever *do* see the old weekly-expiry symptom again** (a cached token
suddenly failing to refresh well before you'd expect), that most likely
means the app's publishing status regressed back to "Testing" somehow (or a
*new* OAuth client was created that isn't the production one) — check
Google Cloud Console → APIs & Services → OAuth consent screen ("Google Auth
Platform") → **Audience** tab → **Publishing status** first, rather than
assuming this is just normal weekly maintenance again. When a cached token
genuinely does need a fresh login (expired for any reason), the next
`run_pipeline.py` (or any Gmail-backed) command handles it automatically —
no need to delete `token.json` by hand:

```
Cached Gmail token for 'personal_hub' is no longer valid (...). Re-opening
browser for a fresh login ...
```

and a browser window opens automatically for a fresh login — same as the
very first time you authenticated that account. Sign in as the **same**
Gmail account named in the message, approve access, and the command
continues normally.

### Write access for the triage flow (`triage_recruiter_inbox.py` only)

Every command above is read-only. `triage_recruiter_inbox.py` is the one
exception — it relabels (`JobTracker/PURSUE|SKIP|NEEDS_REVIEW`) and archives
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

# For real: also relabels JobTracker/PURSUE|SKIP|NEEDS_REVIEW and archives
# (first run without --dry-run opens the gmail.modify consent screen above)
python scripts/triage_recruiter_inbox.py --limit 10

# Score only, never spend on résumé/cover-letter generation even on a pursue verdict
python scripts/triage_recruiter_inbox.py --no-generate

# Widen (or disable, with 0) the rejection-cooldown auto-disqualification window
python scripts/triage_recruiter_inbox.py --rejection-cooldown-days 30
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
- **PURSUE** (at least one extracted role scored "pursue"): generates a
  tailored résumé + cover letter automatically (unless `--no-generate`),
  advances the lead to `package_generated` (or `pursued` with
  `--no-generate`), and archives the message under `JobTracker/PURSUE`.
- **SKIP** (classified noise/rejection, every extracted role scored "pass",
  or a role auto-disqualified by the rejection cooldown below): advances the
  lead to `skipped` and archives under `JobTracker/SKIP` — no Anthropic spend
  beyond the one evaluate call per (non-disqualified) role.
- **Rejection cooldown:** before scoring a role, checks whether that
  `(company, title)` was already marked `rejected` within the last
  `--rejection-cooldown-days` (default 90) — if so, skips JD resolution and
  the two-tier review pipeline entirely and records a `pass` verdict with a
  `"disqualified: ..."` rationale instead. See the Lifecycle stages section
  above for how a lead gets marked `rejected` in the first place.
- **NEEDS_REVIEW** (recruiter outreach with no JD to score, an
  unparseable/incomplete extraction, or a "review" verdict): archived under
  `JobTracker/NEEDS_REVIEW` for a human look; nothing in `job_leads`
  auto-advances past `new`.
- From here, real-world progress (`applied`, `interviewing`, `offered`,
  `accepted`, `started`) is reported by hand — see `list_leads.py
  --set-status` above.

## Tests & coverage

```bash
# Full suite with line + branch coverage (uses .venv when present; installs
# pytest-cov into that env if missing). Exits non-zero on test failure.
# Also prints a soft per-file ≥90% report (see COVERAGE.md).
./scripts/coverage.sh

# Or a focused subset without coverage:
pytest tests/test_classifier.py tests/test_gmail_reader.py -v
```

Coverage policy, omit allowlist, and per-file helper: [`COVERAGE.md`](COVERAGE.md).

For a rollup across `job-tracker`, `comms-migration`, and `recruiting-automation`:

```bash
../report-coverage.sh
# or: ../recruiting-automation/scripts/report-coverage-all.sh
```

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
`not_dealbreakers`, skills vocabulary/weights, and the `pursue_min_pct` /
`review_min_pct` thresholds are all data, not code. Keep it in sync with
`~/CLAUDE.md` §3 (dealbreakers / not-dealbreakers) and §8–9 (skills) when
those change.

**Evaluation vs deliverables (citizenship / sponsorship):** JD lines like
"US citizens only" or "authorized to work without sponsorship" are a clear
**fit** at evaluation time (`not_dealbreakers.us_citizen_or_no_sponsorship`
+ `llm_apply.py`'s `_EVAL_SYSTEM_PROMPT`) — never a reason to force
`REVIEW` or invent a "confirm citizenship" next step. Résumés and cover
letters still **never** state citizenship, sponsorship, or other
work-authorization status (CLAUDE.md §4 rule 11; mechanical `_WORK_AUTH_RE`
check). Active security-clearance *possession* is a separate question from
citizenship / no-sponsorship.

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

# Rule-based (no-LLM) review for one lead — re-scores jd_text, prints VERDICT + match %
# plus every passed/failed framework rule (dealbreakers, not_dealbreakers, skills, thresholds)
python scripts/no_llm_review.py --company "Magnet Forensics" --title "Senior Software Engineer"
python scripts/no_llm_review.py --company "Magnet Forensics" --title "Senior Software Engineer" --json
python scripts/no_llm_review.py --company "Magnet Forensics" --title "Senior Software Engineer" --write

# Export everything to CSV for a spreadsheet pass
python scripts/list_leads.py --csv ~/Desktop/job_leads.csv

# Contacts tracked for a lead, and leads currently awaiting a response
python scripts/list_leads.py --company "Acme" --title "Software Engineer" --show-contacts
python scripts/list_leads.py --waiting

# Advance leads through their lifecycle (models.LEAD_STAGES) as real-world progress happens
python scripts/list_leads.py --verdict pursue --set-status pursued
python scripts/list_leads.py --company "Acme" --title "Software Engineer" --set-status applied --on 2026-07-10

# Soft-delete a lead (junk/duplicate — hides from default list; keeps CRM history)
python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --reason "duplicate"
# Req closed / filled / withdrawn
python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --unavailable
# Already hired (you took another offer, or this req hired someone else)
python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --already-hired
# List hidden leads
python scripts/list_leads.py --status deleted
python scripts/list_leads.py --status unavailable
python scripts/list_leads.py --status hired
# Hard-purge (irreversible — removes lead + contacts/conversations/docs/meetings/offers)
python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --purge --yes
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
a flat blob. Once a lead's `status` moves off `new` (e.g. to `pursued`),
`jd_text` — like the rest of the scoring fields — stops being overwritten by
later re-sends of the same digest, so it won't quietly change out from under
you after you've started using it.

**Lifecycle stages (`models.LEAD_STAGES`):** `new` (unprocessed) → `pursued`
(triage said pursue) → `package_generated` (résumé + cover letter rendered)
→ `applied` → `following_up` → `interviewing` → `offered` → `accepted` →
`started`, with off-ramps that can happen at any point: `skipped` (**we**
decided not to pursue it), `rejected` (**they** declined us), `deleted`
(junk/duplicate you removed), `unavailable` (req closed/filled/withdrawn),
and `hired` (you took another offer, or this req already hired someone —
distinct from `accepted`/`started` on *this* lead). The triage flow (below)
stamps `pursued`/`package_generated`/`skipped` automatically; everything from
`applied` onward — including `rejected` — is real-world progress you report
by hand with `list_leads.py --set-status <stage> [--on <date>]` (or
`delete_lead.py` / `--unavailable` / `--already-hired` for the hide
off-ramps). Each stage past `new` has its own `<stage>_at` timestamp column
(e.g. `applied_at`), stamped once and never overwritten by a later re-run,
so the DB keeps a timeline — not just a current state.

```bash
# Record a rejection you heard about (email, phone call, etc.)
python scripts/list_leads.py --company "Acme" --title "Software Engineer" --set-status rejected
```

**Rejection cooldown / auto-disqualification:** a `rejected` lead also feeds
back into the triage pipeline itself. `pipeline/store.find_recent_rejection()`
checks, for every extracted role, whether that exact `(company, title)` (or a
close fuzzy variant — same 0.92 similarity bar as the "multiple recruiters,
same job" dedup below) was already marked `rejected` within the last N days
(`--rejection-cooldown-days`, default 90, `0` disables it). If so,
`triage_recruiter_inbox.py` short-circuits straight to a `pass` verdict —
skipping the JD resolution and the two-tier review pipeline entirely, so a
digest re-sending a still-open posting (or a different recruiter surfacing
the same already-declined role) never re-spends an LLM call or a human's
attention on it. `pipeline/store.record_rejection()` is the write side —
`list_leads.py --set-status rejected` calls the same `advance_status()`
machinery under the hood, so both paths stay in sync. `job_leads` also has
`rejection_source`/`rejection_email_text`/`rejection_message_id` columns for
recording the rejection's own details when they're known (currently populated
by `record_rejection()` directly — auto-extracting these from a detected
rejection email and staging them for confirmation is a planned follow-up, not
yet wired into the live pipeline).

Leads persist in `var/leads.db` (gitignored — personal data) across runs, so
you can classify a batch, step away, and come back to review with
`list_leads.py` without touching the network again.

## Job CRM foundation (contacts, conversations, documents, meetings, offers)

Full design/use cases: `docs/JOB_CRM_VISION.md`. `var/leads.db` has five join
tables hanging off a job's `normalized_key` (`pipeline/store.py`), each with
its own dataclass in `pipeline/models.py`:

| Table | Dataclass | Answers |
|---|---|---|
| `job_contacts` | `JobContact` | Who's involved (recruiter/hiring manager/referral) — name, email, **phone**. Dedupes on email within a job; a repeat call backfills name/phone onto the existing row if it was missing one. |
| `job_conversations` | `JobConversation` | What was said, when, by which contact, which direction. `latest_conversation_at()` backs the follow-up nudge check. Also drives `job_leads.awaiting_response_since` — see below. |
| `job_documents` | `JobDocument` | JD snapshot, résumé, cover letter, RTR, availability sent, generated follow-up messages — versioned per `doc_type`, auto-incrementing. |
| `job_meetings` | `JobMeeting` | Scheduled/completed interviews, linked to a contact. |
| `job_offers` | `JobOffer` | Comp/benefits/deadline per job, for side-by-side comparison across jobs in `offered` status. |

`triage_recruiter_inbox.py` records a `JobContact` + `JobConversation` for
every message it triages, and calls `find_matching_job()` before doing so:
if a new (company, title) fuzzy-matches an *existing* job well enough
(`find_matching_job` / `find_similar_jobs` in `pipeline/store.py`, same
`SequenceMatcher`-based approach `contacts/store.py` uses for organization
dedup — ratio ≥ 0.92 auto-merges, 0.75–0.92 is logged as a candidate but not
merged), the new contact/conversation attaches to the existing job instead
of only living under a separate lead — this is the "multiple recruiters
pitching the same role" detection from `JOB_CRM_VISION.md` UC-2. `job_leads`
rows themselves are unaffected by this fuzzy match; only the contact/
conversation linkage uses it, so JD text/scoring never gets silently merged
between two postings that just have similar titles.

### "Whose turn is it" — `awaiting_response_since`

`job_leads.awaiting_response_since` is a nullable timestamp, orthogonal to
`status`/`LEAD_STAGES` (a lead can be `applied` *and* waiting-on-them, or
`interviewing` *and* waiting-on-them — it's never a lifecycle stage of its
own). `store.add_job_conversation()` keeps it in sync as a side effect: an
`outbound` conversation (you spoke) sets it to that conversation's
`occurred_at`; an `inbound` one (they spoke) clears it; `direction="other"`
leaves it untouched. For cases direction alone doesn't capture well (e.g. a
completed interview logged as a `JobMeeting`, which has no `direction` of
its own), pass `awaiting_response=True/False` to `add_job_conversation()`
directly, or call `store.set_awaiting_response()` standalone.
`list_leads.py`'s default table has a `WAITING` column, and `--waiting`
filters to just the leads currently awaiting a response.

### Manual (non-email) job management CLIs

Four CLIs cover the use cases that don't come through the triaged-email
path — manual lead creation, ad-hoc contact logging, and document
attachment (`JOB_CRM_VISION.md` UC-1/UC-3/UC-4):

```bash
# Add a job that came from a careers page, a referral, or a conversation —
# not a triaged email. Purely interactive (prompts for company/title/status/
# optional contact); no flags to memorize.
python scripts/add_job.py --db var/leads.db

# Log a manual email/call exchange, or a scheduled/completed interview,
# against an existing job.
python scripts/log_contact.py --company Acme --title "Software Engineer" \
    --conversation --channel email --direction outbound --summary "Sent a follow-up asking about status"

python scripts/log_contact.py --company Acme --title "Software Engineer" \
    --meeting --kind technical --status completed --notes "Went well" --waiting

# Attach a signed RTR, NDA, or any other local file/URL to a job.
python scripts/attach_document.py --company Acme --title "Software Engineer" \
    --doc-type rtr --file ~/Desktop/signed_rtr.pdf

# List every tracked contact across all jobs (name, company, role, phone, email).
python scripts/list_contacts.py
python scripts/list_contacts.py --company Acme --csv ~/Desktop/contacts.csv
```

`log_contact.py`/`attach_document.py` resolve the job by exact
(company, title) match; if nothing matches, they print `find_similar_jobs()`
candidates as a "did you mean" hint rather than guessing and silently
attaching to the wrong job — use `add_job.py` first if it's genuinely new.

### Generated follow-up messages

`scripts/generate_message.py` drafts a short thank-you (post-interview) or
status-check-in (stale application) email via the same Anthropic pipeline
that generates résumés/cover letters (`pipeline/llm_apply.py`'s
`generate_followup_message()`) — same candidate-profile input (`~/CLAUDE.md`)
and the same mechanical house-rule safety net (no banned terms, no
work-authorization statements, no compensation figures). It's saved as a
plain-text file under `--output-root` (default same
`~/Desktop/Resumes/2026/<Company>/` tree as résumés) and recorded as a
`JobDocument`, but **never sent automatically** — always printed for review
first, with any house-rule warnings on stderr:

```bash
python scripts/generate_message.py --company Acme --title "Software Engineer" --kind thank_you \
    --context "We discussed the migration to event-driven architecture"

python scripts/generate_message.py --company Acme --title "Software Engineer" --kind status_check_in
```

The contact name is auto-picked from the job's most-recently-logged
`JobContact` unless overridden with `--contact-name`; `status_check_in`
automatically computes "days since contact" from `awaiting_response_since`.

Not yet built (see `JOB_CRM_VISION.md` §5 open questions): offer-comparison
reports, market-withdrawal drafting, and transition-validation on
`advance_status`.

## Communications archival (catches what triage never sees)

**Why this exists (2026-07-17):** `triage_recruiter_inbox.py` only reads
mail comms-migration has labeled `Category/recruiter_job`. LinkedIn
"Message replied: ..." notifications — the actual back-and-forth on an
*existing* recruiter conversation — are deliberately routed to
`Category/social` instead (a standing comms-migration rule, kept that way on
purpose so ordinary InMail traffic doesn't clutter the job funnel). The side
effect: that traffic was invisible to job-tracker entirely, even when it
carried real signal (a recruiter confirming W2 vs. C2C, naming the actual
end client, or quoting a rate). `scripts/scan_communications.py` is the fix.

```bash
# Writes to var/leads.db AND to Gmail (gmail.modify, since 2026-07-19) — a
# resolved inbound message gets JobTracker/Linked and is archived; a parked
# (unmatched) inbound message gets JobTracker/NeedsFollowup and stays in the
# inbox. --dry-run skips both. Scans hit-reply@/inmail-hit-reply@linkedin.com
# (the two senders that carry real message text) plus, with --include-sent,
# your own Sent-folder replies (Tier-1 thread/contact match only, never
# labeled — see below).
python scripts/scan_communications.py --llm-fallback --include-sent

# See what's still parked, unmatched (a ~160-char preview per message):
python scripts/resolve_communication.py --list

# Read one in full before deciding how to resolve it (var/pending-actions.html's
# "Unmatched communications" table also has a click-to-expand full preview,
# with From/To/Subject/Message-Id repeated above the body, for the same reason):
python scripts/resolve_communication.py --message-id <id> --show

# Attach one to the job it's actually about (--create makes a new stub lead
# if this is a genuinely new opportunity with no JD yet):
python scripts/resolve_communication.py --message-id <id> \
    --company "<company>" --title "<title>" --contact-name "<name>"

# On-demand full communications history for one job, rendered fresh from
# whatever's in job_conversations right now (not a series of dated snapshots):
python scripts/export_communications.py --company "<company>" --title "<title>"
```

Matching (`pipeline/comms_match.py`) tries progressively more expensive
tiers, cheapest first:

1. **Thread id** already linked to a job (`job_conversations.thread_id`) — free.
2. **Contact email** already on file (`job_contacts.email`, across *all*
   jobs, not just one) — free.
3. **LLM extraction** (opt-in via `--llm-fallback`, one cached-by-message_id
   Haiku call) — reuses `pipeline/llm_extract.py`'s existing "pull a
   company/title out of this email" extractor, then fuzzy-matches the result
   against `job_leads` the same way `find_matching_job()` does. A
   company-only extraction (no title — e.g. "confirming W2, end client is GE
   Healthcare") only auto-attaches if it fuzzy-matches **exactly one**
   existing job for that company; more than one is genuinely ambiguous and
   left for a human.
4. **A full (company, title) pair extracted, matching nothing on file**
   (`llm_new_lead` tier / `MatchOutcome.is_new_lead_candidate`) — distinct
   from genuinely "couldn't tell." Deliberately **not** the full triage
   happy path *at this step*: `scan_communications.py` itself only creates
   a brand-new stub lead (scored with the free rule-based pass only — no
   ATS lookup, no LLM review, no résumé/cover letter) so it's visible on
   the dashboard right away. `scripts/process_awaiting_llm_review.py` (see
   below) is what automatically finishes the job on the next hourly cycle
   once that stub's score clears the review gate — no separate manual
   `apply_package.py` run needed unless you want to jump the queue.
5. **Unmatched** — parked in the `unmatched_messages` table for
   `resolve_communication.py`.

**Whenever both a company and a title are extracted** (tiers 3's
`llm_company_title` match and tier 4's brand-new lead — never tiers 1/2,
which don't run an extraction at all), two more things happen automatically:
the raw message is saved as a `.txt` `JobDocument` (`doc_type="email_txt"`)
in that job's folder (`communications/Email_<message_id>.txt`, same
folder convention as `export_communications.py`'s PDF), and the extracted
text is folded into that lead's `jd_text` — appended for an existing lead,
set outright for a new one. Both only touch a lead while it's still
`status="new"`; once a human has triaged it, `store.upsert_lead`'s standing
guard leaves its `jd_text` alone.

Sent-folder scanning (`--include-sent`) deliberately only ever uses Tier 1
— an outbound message with no thread/contact match is silently skipped, not
parked, since a Sent folder carries plenty of ordinary non-recruiting mail
and parking every unrecognized outgoing email would flood the review queue.
This is also why replying in-thread and naming the company/title in cold
outreach (rather than composing a brand-new email) matters in practice: it's
what keeps future replies on Tier 1 instead of falling through to Tier 3/4.
Sent messages are never labeled, matched or not — Sent isn't something
reviewed for "still needs my attention."

Wired into `recruiting-automation/run_cycle.sh`'s hourly cycle already;
`var/pending-actions.html`'s "Unmatched communications" section (rendered by
`scripts/render_pending_actions.py`) surfaces whatever's still waiting on a
human. Every linked/resolved message is archived verbatim in
`job_conversations.body_text` — cheap and searchable by default; the PDF
export above is only for when you actually need a document to hand someone.

### Closing the "Awaiting full-LLM-review" loop (2026-07-19)

**Why this exists:** `var/pending-actions.html`'s "Awaiting full-LLM-review"
bucket — leads whose free rule-based score already cleared
`config/framework.yaml`'s `llm_review_min_pct` gate but have no
`llm_verdict` yet — carried a code comment calling it "purely a 'wait for
the pipeline' state." Nothing in `run_cycle.sh` actually did that waiting-for.
Verified live: 21 leads sitting there, several 12+ days old (one at a 100%
rule-based match), most landed there via this section's own stub-lead
creation (tier 4 above), a few via a digest whose score cleared the gate
before `triage_recruiter_inbox.py`'s real LLM call reached it.

```bash
python scripts/process_awaiting_llm_review.py             # sweeps + processes, live
python scripts/process_awaiting_llm_review.py --dry-run   # lists candidates, touches nothing
python scripts/process_awaiting_llm_review.py --limit 5   # spend circuit-breaker for a backlog catch-up run
```

For every qualifying lead, this runs the exact same
`pipeline/llm_apply.generate_two_tier_package` call `apply_package.py` runs
by hand for one lead — full LLM review always (already cleared the score
gate that decides whether that's worth spending on), résumé + cover letter
only on an actual "pursue" — and advances `status` to
`package_generated`/`skipped` the same way `triage_recruiter_inbox.py` does
after its own call. A lead only ever leaves the candidate set once it has
an `llm_verdict`, so nothing gets billed twice across hourly runs. No Gmail
access at all (local DB + Anthropic API only) — wired into
`recruiting-automation/run_cycle.sh`'s hourly cycle, right after
`scan_communications.py`.

### Keeping Gmail labels trustworthy (2026-07-19)

Two Gmail-label gaps stood between "the pipeline handles this" and actually
being able to stop reviewing recruiting mail in the Gmail client directly:

1. **This section's `Category/social` traffic carried no Gmail signal at
   all**, ever — a perfectly-archived reply and an untouched one looked
   identical in the inbox. Fixed above: `scan_communications.py` now labels
   a resolved inbound message `JobTracker/Linked` (+ archives it) and a
   parked one `JobTracker/NeedsFollowup` (left visible in the inbox).
2. **`triage_recruiter_inbox.py`'s own `JobTracker/PURSUE\|SKIP\|
   NEEDS_REVIEW` label goes stale** — it's set once, at initial triage, and
   never revisited even when a later full LLM review or a manual
   `list_leads.py --set-status` changes the lead's effective verdict.
   `scripts/resync_labels.py` fixes this: for every already-triaged message,
   it re-derives today's outcome from the linked lead(s)'
   CURRENT `llm_verdict`/`verdict` (same PURSUE > NEEDS_REVIEW > SKIP
   priority rule as initial triage) and swaps the label if it's drifted.
   No LLM spend, no INBOX/archive changes — a pure label sync.

```bash
python scripts/resync_labels.py --dry-run   # preview what's stale
python scripts/resync_labels.py             # apply
```

Both are wired into `recruiting-automation/run_cycle.sh`'s hourly cycle.
Together, every category of recruiting mail this pipeline touches now
carries a label reflecting its CURRENT state — what's left unlabeled, or
still carrying `NEEDS_REVIEW`/`NeedsFollowup`, is exactly (and only) what
still needs a human look.

### Recruiter contact extraction (2026-07-17)

A LinkedIn InMail's `From:` header is always a generic relay address
(`inmail-hit-reply@linkedin.com`) — no use as a contact record — but a real
name, and often a real email/phone, are usually sitting in the body itself:
LinkedIn's own template renders a short sender block (`<Name> / Reply /
<thread URL>`), and a meaningful minority of recruiters also sign off with
their own `Name | Company` / `Title` / `Email: ... | Cell: ...` block.
`src/job_tracker/pipeline/signature.py`'s `parse_signature()` — plain
regex, no LLM call, so it runs on every message for free — pulls both
signals out (preferring the recruiter's own fuller sign-off name over
LinkedIn's sometimes-truncated display name), and both
`scan_communications.py` and `resolve_communication.py` populate
`job_contacts` from it automatically now. Deliberately does **not** attempt
to recover a LinkedIn profile URL — the only link in these messages is a
private "reply to this thread" URL, not the sender's public profile; there's
nothing there to parse. See what's been captured for a job without opening
Gmail at all:

```bash
list-leads --company "<company>" --title "<title>" --show-contacts
```

## Limits

- **Workday** has no clean public board API — expect misses; fallback TBD.
- **LinkedIn** is intentionally out of scope (no public retrieval API).
- Direct-email employers (e.g. careers@…) won't resolve via ATS boards.
