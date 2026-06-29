# job-tracker

Job-search **automation pipeline** for Shawn Becker's recruiting funnel.

**Routing truth** (where mail goes) lives in [comms-migration](https://github.com/sbecker11/comms-migration) — see [`routing-inventory.md`](https://github.com/sbecker11/comms-migration/blob/main/routing-inventory.md) for the four-into-one forward into `shawnbecker.recruiting@gmail.com`. **Processing** (what happens to that mail) lives here.

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

1. [Google Cloud Console](https://console.cloud.google.com/) → create project → enable **Gmail API**.
2. OAuth consent screen → add test user (`shawnbecker.recruiting@gmail.com`).
3. Credentials → **OAuth client ID** → Desktop app → download JSON.
4. Save as `credentials.json` in the repo root (gitignored), or set `JOB_TRACKER_GMAIL_CREDENTIALS`.
5. First fetch opens a browser; token caches to `token.json` (gitignored) or `JOB_TRACKER_GMAIL_TOKEN`.

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
