# workspace-recruiting-automation

Umbrella folder for Shawn’s recruiting pipeline. **Open this folder as the Cursor
workspace root** so all siblings stay in one window.

| Sibling | Owns | One-liner |
|---------|------|-----------|
| [`comms-migration/`](comms-migration/) | **Routing** | Hubs, contacts, `rules/senders.yaml`, Gmail classify/label |
| [`job-tracker/`](job-tracker/) | **Processing** | Gmail → score → résumé/cover-letter packages |
| [`recruiting-automation/`](recruiting-automation/) | **Orchestration** | Hourly `launchd` schedule, HALT, status |

Related (not part of the hourly pipeline): `workspace-resume-flyer/`.

```
mail arrives
  → comms-migration (classify / label / archive)
  → job-tracker (triage / packages / CRM)
  → recruiting-automation (runs the above on a schedule)
```

## Doc map

| Doc | Audience |
|-----|----------|
| **This file** | Humans — install, ops, orientation |
| [`AGENTS.md`](AGENTS.md) | Cursor agents — ownership map (no candidate facts) |
| [`SECRETS.md`](SECRETS.md) | git-crypt keys, `.env`, Time Machine exclusions |
| `~/CLAUDE.md` | Candidate profile (only source of truth for packages) |
| Each sibling `README.md` | That repo’s quick start |
| Each sibling `docs/REFERENCE.md` | Full historical / deep reference |

## New-machine install

Paths below use `$WORKSPACE` — on mini2 that’s usually  
`~/workspace-recruiting-automation`; on mini1 via SMB it may be  
`/Volumes/sbecker11/workspace-recruiting-automation`.  
You can also export:

```bash
export RECRUITING_AUTOMATION_WORKSPACE_ROOT=/path/to/workspace-recruiting-automation
```

### 0. Prerequisites

```bash
brew install git-crypt
# Python 3.11+ recommended; each sibling uses its own .venv
```

### 1. Clone the three siblings side-by-side

```bash
mkdir -p "$WORKSPACE" && cd "$WORKSPACE"
git clone git@github.com:sbecker11/comms-migration.git
git clone git@github.com:sbecker11/job-tracker.git
git clone git@github.com:sbecker11/recruiting-automation.git
```

### 2. Unlock `.env` (git-crypt)

Keys live at `~/.git-crypt-keys/<repo>.key` (never in git). Full rationale:
[`SECRETS.md`](SECRETS.md).

```bash
mkdir -p ~/.git-crypt-keys && chmod 700 ~/.git-crypt-keys
# copy the three .key files here via scp / encrypted USB / password manager

cd "$WORKSPACE/comms-migration" && git-crypt unlock ~/.git-crypt-keys/comms-migration.key
cd "$WORKSPACE/job-tracker" && git-crypt unlock ~/.git-crypt-keys/job-tracker.key
cd "$WORKSPACE/recruiting-automation" && git-crypt unlock ~/.git-crypt-keys/recruiting-automation.key

# Shared Anthropic key (optional but recommended):
cp "$WORKSPACE/.env.example" "$WORKSPACE/.env"   # if present
# edit WORKSPACE/.env with ANTHROPIC_API_KEY
./tm-exclude-env-files.sh                         # exclude plaintext .env from Time Machine
```

### 3. Per-repo Python env

```bash
for r in comms-migration job-tracker; do
  cd "$WORKSPACE/$r"
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  # job-tracker also: pip install -e ".[dev]"
  deactivate
done
```

`.venv` is **not relocatable** — after moving this folder, recreate each venv.

### 4. OAuth (one-time per machine)

Follow each sibling README:

- `comms-migration` — `~/.config/comms-classifier/{personal_hub,recruiting_funnel}/`
- `job-tracker` — `~/.config/job-tracker/` (+ optional `personal_hub/`)

Always dry-run before the first live Gmail write.

### 5. Start the schedule

```bash
cd "$WORKSPACE/recruiting-automation"
./install.sh          # or ./install.sh 0 for no expiry window
./status.sh
```

### 6. Smoke checks

```bash
cd "$WORKSPACE/comms-migration" && source .venv/bin/activate
python scripts/run_classifier.py --account personal_hub --dry-run --limit 5

cd "$WORKSPACE/job-tracker" && source .venv/bin/activate
# see job-tracker/PRIMER.md for the end-to-end sequence
```

## Daily ops

| Goal | Command |
|------|---------|
| **Monday briefing (decide now)** | `cd recruiting-automation && ./monday.sh` |
| Health check | `cd recruiting-automation && ./status.sh` |
| Restart after HALT | `./install.sh` (clears halt; safe to re-run) |
| Stop on purpose | `./stop.sh` |
| Coverage rollup | `./report-coverage.sh` (workspace root) |
| Re-exclude `.env` from Time Machine | `./tm-exclude-env-files.sh` |
| Candidate / package rules | `~/CLAUDE.md` (never fork into AGENTS.md) |

`./monday.sh` refreshes pending-actions data and prints decision queues ranked
for **interview likelihood** (direct-recruiter, reply-due, match %, packages
ready) — aimed at zero dead-time between lead arrival and action.

## KPIs (Phase 0 baseline)

Track weekly (or whenever tuning the pipeline). `./status.sh` prints a live
snapshot; `./status.sh --json` emits machine-readable KPIs for scripts.

| KPI | Source | Healthy target |
|-----|--------|----------------|
| Minutes/day in Gmail for recruiting | Manual (weekly) | Trending toward 0 |
| Unmatched communications | `status.sh` / `./monday.sh` | 0 — clarify immediately |
| Awaiting full LLM review | `status.sh` / `./monday.sh` | Low; none > 3 days old |
| Packages ready to send | `status.sh` / pending-actions UI | Act same day |
| Waiting on them (stale) | `status.sh` | Follow up when threshold hit |
| Label↔DB drift | `resync_labels.py --dry-run` | 0 would-relabel rows |
| Framework sync | `verify_framework_sync.py` | OK (no drift vs `~/CLAUDE.md`) |
| HALT incidents / week | `logs/run-*.log` | 0 after reliability work |
| $ / pursue package / week | LLM logs (Phase 4) | TBD |

Guardrails wired in Phase 0:

```bash
cd job-tracker && python scripts/verify_framework_sync.py   # CLAUDE.md ↔ framework.yaml
cd recruiting-automation && ./status.sh                     # schedule + KPI queues
```

## Layout

```
workspace-recruiting-automation/     ← open this in Cursor (not a git repo)
  README.md                          ← this file
  AGENTS.md                          ← agent ownership map
  SECRETS.md                         ← keys / .env / backups
  .env                               ← shared ANTHROPIC_API_KEY (local)
  comms-migration/                   ← git repo
  job-tracker/                       ← git repo
  recruiting-automation/             ← git repo
  workspace-resume-flyer/            ← related tooling, not hourly pipeline
```
