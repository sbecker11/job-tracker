# job-tracker — Cursor project instructions

**Humans:** [`README.md`](README.md) · [`PRIMER.md`](PRIMER.md) · umbrella [`docs/WORKSPACE.md`](docs/WORKSPACE.md).

Job-search **processing** pipeline: Gmail → classify → ATS JD resolve → score →
store → (on pursue) résumé/cover-letter packages.

Routing (where mail goes) is **not** owned here — that lives in sibling
`comms-migration/`. Scheduling lives in sibling `recruiting-automation/`.

## Candidate profile (required)

When evaluating a JD, scoring a lead, generating or revising a résumé/cover
letter, or changing dealbreakers/skills vocabulary:

1. **Load `~/CLAUDE.md` first** — it is the only candidate-profile source of truth.
2. Treat its house rules, dealbreakers, timeline, and §8–§9 anchors as non-negotiable.
3. Do **not** invent employers, metrics, stakeholders, or domain claims absent from that file.
4. Ignore conflicting notes in `~/.claude/` session memory if they disagree with `~/CLAUDE.md`.

Automation reads the same file via `JOB_TRACKER_CANDIDATE_PROFILE_PATH`
(defaults to `~/CLAUDE.md`). Keep `config/framework.yaml` in sync when the
CLAUDE.md dealbreaker / skills framework changes.

## This repo owns

- Reading `shawnbecker.recruiting@gmail.com` (Gmail API)
- Classification, extraction, ATS resolution, keyword + LLM scoring
- Lead DB (`var/leads.db`), package generation under `~/Desktop/Resumes/2026/`
- Mechanical post-generation checks (banned terms, work-auth language, compensation figures)

## This repo does not own

- Hub/contact routing or `rules/senders.yaml` → `comms-migration`
- launchd schedule / halt window → `recruiting-automation`

## Local helpers (optional)

- Finder folder open: `tools/reveal-folder/install.sh` → `revealfolder://reveal?path=...`
- View communications ODT: `tools/view-communications/install.sh` → `viewcomms://open?company=...&title=...` (Pending Actions React UI: **View communications**)
- Regenerate pending-actions page: `tools/refresh-pending/install.sh` → `refreshpending://run`
- After BCC self on a LinkedIn reply: `tools/reply-sent/install.sh` → `replysent://run` (per-row **Reply sent** next to “Recruiter waiting on you” — runs `comms_fast_cycle.py`)
- Process new mail at shawn.becker@spexture.com immediately (full JD resolve + LLM score + package, vs. the 3-minute tick's offline/no-generate stub): `tools/triage-imap-now/install.sh` → `triageimap://run` (header **Check inbox now** button — runs `scripts/triage_imap_now.py`, then jumps to the newly-processed lead wherever it landed)
- Inline tri-state edit (direct_recruiter_outreach): `tools/set-direct-recruiter-outreach/install.sh` → `setdro://set?key=...&value=yes|no|undecided`
- Dismiss LinkedIn reply card: `tools/dismiss-linkedin-reply/install.sh` → `dlr://dismiss?kind=lead|unmatched&key=...&message_id=...`
- Mark résumé package sent: `tools/mark-package-sent/install.sh` → `mps://mark?key=...`
- React pending-actions UI: `pending-actions-ui/` — `tools/pending-actions-ui-server/install.sh` starts Vite at login/reboot (`http://127.0.0.1:3174/`); or manually `python scripts/render_pending_actions.py --no-rescore` then `cd pending-actions-ui && npm run dev` (stage strip: Clarify → Send résumé → Wait → Decide/apply)

Prefer `python-docx` via this repo’s `.venv` (+ `letter_style.apply_template_styles`)
when generating packages from scratch.

## leads.db concurrency (2026-08-18)

Several independent processes write to `var/leads.db`: the hourly
`recruiting-automation/run_cycle.sh`, the 3-minute `comms_fast_cycle.py`
LaunchAgent tick, and one-off runs like `scripts/triage_imap_now.py`. A
real HALT on 2026-08-18 (`sqlite3.OperationalError: database is locked`)
came from the first two colliding with no coordination between them.

Fix, in two parts:
- `store.connect()` opens with `timeout=30` (was sqlite3's 5s default) and
  `PRAGMA journal_mode=WAL`, so brief overlap waits instead of erroring.
- All three writers above also cooperate through one shared advisory lock
  — `job_tracker.pipeline.db_lock` (`acquire`/`release`, backed by
  `fcntl.flock` on `var/comms_fast.lock`) — with `scripts/with_db_lock.py`
  as the bash-callable wrapper `run_cycle.sh` uses (zsh has no flock and
  macOS ships no `flock(1)`). **Do not** add lock acquisition *inside* the
  individual triage/render CLI scripts themselves — `comms_fast_cycle.py`
  already holds this lock while calling those scripts as subprocesses, so
  a script that also tried to acquire it internally would deadlock against
  its own parent.

A dedicated always-running "DB agent" (every writer talks over IPC instead
of opening sqlite3 directly) was considered and set aside as
disproportionate for this project's scale — see `job_tracker/pipeline/db_lock.py`'s
docstring. Revisit only if the write volume/process count grows a lot.
