# job-tracker

Job-search **processing** pipeline: Gmail → score → (on pursue) résumé/cover-letter
packages under `~/Desktop/Resumes/2026/`.

| Sibling | Owns |
|---------|------|
| [`comms-migration`](../comms-migration/) | **Routing** — how mail reaches the funnel |
| **This repo** | **Processing** — what happens after it arrives |
| [`recruiting-automation`](../recruiting-automation/) | **Orchestration** — hourly schedule |

> **Just want to run it?** → [`PRIMER.md`](PRIMER.md)  
> Umbrella install / ops → [`../README.md`](../README.md) (or [`docs/WORKSPACE.md`](docs/WORKSPACE.md))  
> Secrets / git-crypt → [`../SECRETS.md`](../SECRETS.md) (or [`docs/SECRETS.md`](docs/SECRETS.md))  
> Full historical detail → [`docs/REFERENCE.md`](docs/REFERENCE.md)

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e ".[dev]"

# Unlock .env — see ../SECRETS.md (key: ~/.git-crypt-keys/job-tracker.key)
# Prefer shared ANTHROPIC_API_KEY in ../.env when possible
```

### Gmail (one-time)

Credentials stay **outside** the repo:

```bash
mkdir -p ~/.config/job-tracker
# Google Cloud → Gmail API → OAuth Desktop client → save as:
cp ~/Downloads/client_secret_*.json ~/.config/job-tracker/credentials.json
```

First live fetch opens a browser; token caches to `~/.config/job-tracker/token.json`.  
Optional personal-hub + write-scope (`token_modify.json`) details:
[`docs/REFERENCE.md`](docs/REFERENCE.md).

### Smoke tests

```bash
pytest tests/test_classifier.py tests/test_gmail_reader.py -v
python scripts/run_pipeline.py --all-fixtures --offline
python scripts/triage_recruiter_inbox.py --dry-run --inbox-batch-message-cap 5
```

## Common commands

| Goal | Command |
|------|---------|
| End-to-end primer | See [`PRIMER.md`](PRIMER.md) |
| Triage recruiter inbox (dry-run) | `python scripts/triage_recruiter_inbox.py --dry-run --inbox-batch-message-cap 10` |
| List pursue leads | `python scripts/list_leads.py --verdict pursue` |
| Apply package for one lead | `python scripts/apply_package.py --company "…" --title "…"` |
| Pending-actions HTML | `python scripts/render_pending_actions.py --no-rescore` |
| React UI (port 3174) | `./tools/pending-actions-ui-server/install.sh` |
| Coverage | `./scripts/coverage.sh` |
| Workspace coverage | `../report-coverage.sh` |

## Pipeline (target)

```
recruiting Gmail inbox
  → classifier / extract / ATS JD resolve
  → framework scoring (dealbreakers, match %, pursue/pass)
  → leads DB (var/leads.db) + packages on pursue
```

Candidate profile: **`~/CLAUDE.md`** (required for JD eval / packages).  
Keep `config/framework.yaml` in sync when CLAUDE.md dealbreakers/skills change.

## Components (map)

| Area | Path |
|------|------|
| ATS JD resolve | `src/job_tracker/ats/` |
| Gmail + classifier | `src/job_tracker/email/` |
| Extract / LLM / triage | `src/job_tracker/pipeline/` |
| CLI entrypoints | `src/job_tracker/cli/` + `scripts/` |
| Dealbreakers / skills | `config/framework.yaml` |
| Pending-actions UI | `pending-actions-ui/` |
| Design docs | `docs/JOB_CRM_VISION.md`, `docs/CATEGORY_HANDLER_EXTENSIBILITY.md` |

## `.env` / git-crypt

Tracked + encrypted. Unlock steps: [`../SECRETS.md`](../SECRETS.md).

## More detail

Deep topics (CRM, communications archival, IMAP, label resync, unemployment
report, OAuth production notes, etc.) live in
[`docs/REFERENCE.md`](docs/REFERENCE.md) — not duplicated here.
