# job-tracker — Cursor project instructions

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
- Regenerate pending-actions page: `tools/refresh-pending/install.sh` → `refreshpending://run`

Prefer `python-docx` via this repo’s `.venv` (+ `letter_style.apply_template_styles`)
when generating packages from scratch.
