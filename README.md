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
| `src/job_tracker/pipeline/` | Classify → resolve → score → dedup — planned |
| `config/framework.yaml` | Scoring config stub |

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

## Limits

- **Workday** has no clean public board API — expect misses; fallback TBD.
- **LinkedIn** is intentionally out of scope (no public retrieval API).
- Direct-email employers (e.g. careers@…) won't resolve via ATS boards.
