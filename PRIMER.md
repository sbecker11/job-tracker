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

Stage 6 (free–$0.01/msg, runs alongside Stage 5 in the hourly cycle — see "Archive communications" below)
LinkedIn replies (Category/social) + Sent folder  →  tiered match  →  job_conversations, or unmatched queue
                              scan_communications.py (read-only Gmail; see docs/JOB_CRM_VISION.md)
```

---

## Prerequisites (one-time, already done in this environment)

- Gmail OAuth for `shawnbecker.recruiting@gmail.com`: `~/.config/job-tracker/credentials.json` + `token.json` — present.
- Optional second account `personal_hub` (`scbboston@gmail.com`, reads `Category/recruiter_job` mail comms-migration's classifier applies there): `~/.config/job-tracker/personal_hub/` — present.
- `ANTHROPIC_API_KEY` in `.env` — required for Stage 2 and Stage 4 (both call the LLM). Without it, only Stage 1's keyword scoring works.
- Candidate profile / JD Match Framework: `~/CLAUDE.md` (dealbreakers, not-dealbreakers, skills vocabulary, banned terms, contact info) and `config/framework.yaml` (the same framework, machine-readable, used by the keyword scorer; LLM evaluate also honors `not_dealbreakers` via `_EVAL_SYSTEM_PROMPT`). Keep these two in sync if the framework changes. In particular: US citizen / no-sponsorship JD requirements are ✅ fit at evaluation; packages still omit work-auth language (§4 rule 11).
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
- Citizenship / "no sponsorship" JD requirements are evaluated as a clear fit (same as W2-only) — not a soft REVIEW gate. Do not confuse that with the package house rule that still forbids writing work-auth status into résumé/cover letter.
- Ends with a `=== PURSUE ===` / `=== REVIEW ===` printout and the exact next command for each.

## Stage 3 — Review scored leads

```bash
python scripts/list_leads.py --verdict pursue
python scripts/list_leads.py --verdict review
python scripts/list_leads.py --company "Acme" --title "Software Engineer"   # one lead, full detail
python scripts/list_leads.py --verdict pursue --csv ~/Desktop/job_leads.csv  # spreadsheet pass

# Deterministic no-LLM review for one lead (re-score + passed/failed rules + VERDICT)
python scripts/no_llm_review.py --company "Acme" --title "Software Engineer"
python scripts/no_llm_review.py --company "Acme" --title "Software Engineer" --json
python scripts/no_llm_review.py --company "Acme" --title "Software Engineer" --write

# Hide a lead from default list / pending-actions (keeps CRM history)
python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --reason "duplicate"
python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --unavailable
python scripts/delete_lead.py --company "Acme" --title "Software Engineer" --already-hired
```

For a single bookmarkable overview instead of one-off queries:

```bash
python scripts/render_pending_actions.py
```

Regenerates `var/pending-actions.html` — a static, `file://`-bookmarkable
snapshot laid out as a **sales funnel toward "ready to apply"** (redesigned
2026-07-15, replacing an earlier flatter needs-review/auto-skipped/unresolved
split that made "how close am I to actually submitting something" hard to
answer at a glance). Company-name links open the shared `<Company>/` folder in
**Finder**; title links open that lead's own package folder (`<Company>/` for
a single-lead company, `<Company>/<Company>_<Title>/` when the company has
multiple leads) via the local `revealfolder://` helper — install once with
`tools/reveal-folder/install.sh`. **Regenerate page** re-runs this same
script via `tools/refresh-pending/install.sh` (`refreshpending://run`).
Browsers will ask to allow each scheme the first time you click. A
horizontal strip of 5 boxes runs target-to-farthest, left to right, each
clickable to jump to its section below:

1. **Ready to apply** (the target) — `llm_verdict='pursue'`, still sitting at
   `status='package_generated'` (i.e. genuinely not yet acted on), with both
   a résumé and cover letter confirmed present **on disk**, not just claimed
   by the DB status.
2. **Needs your decision (forced package)** — a package already got
   generated (via `apply_package.py --force`, or the pursue-but-missing-files
   edge case) despite a non-pursue verdict; read the review, then either
   submit anyway or mark `skipped`.
3. **Needs your decision** — a real full-LLM-review ran and came back
   `review` (or, rarely, a `pursue` that's somehow still stuck at `status=new`
   instead of already having a package — shown here rather than hidden, since
   that'd indicate a pipeline bug). This is still the one sortable/filterable
   table with the "copy prompt" button.
4. **Awaiting full-LLM-review** — cleared the free rule-based score's
   `llm_review_min_pct` gate but the real LLM call hasn't run yet; nothing to
   decide, just wait for the next hourly cycle (or force it manually).
5. **JD unresolved** — no usable JD text at all yet (`verdict='REVIEW NEEDED'`);
   nothing downstream can happen until a human finds and pastes in the real
   posting.

Leads that were never going to clear the LLM-review gate, or that the LLM
already said `pass` on, are deliberately **not** part of the funnel — they're
low-priority chaff, not something blocking your target action — and collapse
into a single small "N leads not prioritized" footnote instead of their own
section. Separately, and below the funnel entirely, a **"Tracking submitted
applications"** section groups everything already past `package_generated`
(`applied`, `interviewing`, `skipped`, `rejected`, etc.) purely for follow-up
tracking, since nothing needs to happen to make those "ready" — they're
already resolved one way or another.

By default the script also refreshes every `status='new'` lead's rule-based
score with the current scorer before rendering (pass `--no-rescore` to skip
that and just render whatever's already stored). Re-run any time after the
backlog changes — it's free and local, no API calls.

Every table also shows an **Age (days)** column (days since `first_seen`)
and defaults to oldest-first sort — a lead's value decays the longer it
sits unreviewed (the posting may fill, the JD may go stale), so the
default view surfaces what's most at risk of going stale first rather
than just what scored highest. Click any column header in the main table
to re-sort by that column instead (click again to reverse); ages of 21+
days show in amber.

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

Two-tier review pipeline (2026-07-11) — every lead gets a free rule-based
pass; only a lead that looks like a strong stack match spends an LLM call;
only an actual "pursue" verdict spends a second LLM call on documents:

1. **Always**: the free, deterministic rule-based scorer (`scoring/scorer.py`,
   same `config/framework.yaml` vocabulary as Stage 1) scores `jd_text` and
   saves `no-LLM-review.docx` — a match % (JD-relative: of the recognizable
   tech stack *this JD* mentions, how much of it the candidate covers) plus a
   skills-match table. No API call, no cost.
2. **Only if** that score clears `thresholds.llm_review_min_pct` in
   `config/framework.yaml` (currently 70%) — or `--force` — a real LLM call
   runs the full JD Match Framework and saves `full-LLM-review.docx` (richer
   match %, dealbreaker sweep, skills matrix, framing/interview-prep notes).
3. **Only if** that LLM verdict is "pursue" — or `--force` — a second LLM
   call renders `Shawn_Becker_Resume_<Company>_<Title>.docx` +
   `Shawn_Becker_Cover_Letter_<Company>_<Title>.docx`.

- Output location: everything for one lead lands together under
  `~/Desktop/Resumes/2026/<Company>/` (override the root with
  `--output-root`) — flat directly in that folder if this is the company's
  only tracked lead, or nested one level deeper in
  `<Company>/<Company>_<Title>/` once a second lead exists for the same
  company (existing flat files auto-migrate the first time a sibling shows
  up).
- Both documents are generated from the candidate profile in `~/CLAUDE.md` (contact info, banned terms, positioning) — never hand-typed per lead.
- `--json` gives the full machine-readable result (both tiers' verdicts/scores, all doc paths, token/cost/time metrics for the evaluate and generate calls) if you're scripting around this.
- Optional `--comparison-jsonl <path>`: if you're tracking leads in a manual comparison JSONL file, this updates the matching company/title line with the result instead of leaving it a separate, disconnected artifact.
- `--force` bypasses both LLM gates above at once — use when a human's
  already decided this lead deserves the full treatment regardless of what
  the free pass or the LLM verdict says (e.g. after a call).

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

### Resolving JDs for link-only digest leads (manual, agent-assisted)

`resolve_jd_text()` (`pipeline/run.py`) only knows how to hit public ATS
board APIs (Greenhouse, Lever, etc.) directly by company/title. For leads
that only carry a link the ATS lookup can't resolve — e.g. a LinkedIn
digest's click-tracking URL (`linkedin.com/comm/jobs/view/<id>/?trackingId=...`,
gated behind login) — there's no automated crawler in this repo; getting the
full JD is a manual, agent-assisted step (WebFetch), following this order —
2026-07-11 policy:

1. **Follow the link chain.** Strip tracking wrappers down to the canonical
   public URL (e.g. LinkedIn's `/comm/jobs/view/<id>/?trackingId=...` →
   `/jobs/view/<id>/`, which stays publicly crawlable) and fetch that. For
   ATS-hosted links (Greenhouse, Lever, etc.) fetch directly.
2. **If that fails, search the company's own careers page** for the same
   role (by title + location) as a second attempt before giving up.
3. **If a full JD still can't be located by either method**, don't leave the
   lead on a stale thin-snippet score — explicitly mark it for manual
   intervention instead of letting a low-signal automatic score stand in for
   "actually looked and found nothing":
   ```bash
   sqlite3 var/leads.db "
   UPDATE job_leads SET match_pct = 0, verdict = 'REVIEW NEEDED'
   WHERE normalized_key = '<company>::<title>';"
   ```
   (`REVIEW NEEDED` is deliberately distinct from the scorer's normal
   `pursue`/`review`/`pass` verdicts — it signals "JD unresolved, needs a
   human to go find it," not "scored low.")

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
| 4 (`apply_package.py`) | Local rule-based score always; Anthropic API only past each gate | Free (`no-LLM-review.docx`) → ~$0.05–0.10/lead for the full LLM review only once the free score clears 70% → +~$0.10–0.20 more for résumé/cover letter only on a pursue verdict |
| 5 (`triage_recruiter_inbox.py`) | Gmail (**read/write** — `gmail.modify`), Anthropic API | ~$0.02–0.04/message (evaluate), +~$0.10–0.30 more only on a pursue verdict; relabels + archives the message |
| 6 (`scan_communications.py`) | Gmail (read-only), Anthropic API only with `--llm-fallback` | Free (thread-id/contact-email matching) + ~$0.001–0.01/message with `--llm-fallback` (Haiku, cached per message_id) |

Nothing in this pipeline applies, replies to, or sends anything to an
employer on your behalf — it only surfaces, scores, and drafts documents for
you to review and use yourself.

---

## Stage 6 — Archive communications the triage flow never sees

Background (2026-07-17): comms-migration deliberately routes LinkedIn
"Message replied: ..." notifications to `Category/social`, not
`Category/recruiter_job` — so `triage_recruiter_inbox.py` (Stage 5) never
even sees that traffic, even when it's a real recruiter confirming details
that matter (W2 vs. C2C, the actual end client, rate). Three such messages
were found sitting completely untracked before this stage existed.

```bash
# Read-only against Gmail — no labels/archiving touched, only var/leads.db written.
python scripts/scan_communications.py --llm-fallback --include-sent

# See what couldn't be auto-matched:
python scripts/resolve_communication.py --list

# Read one in full (var/pending-actions.html's own "Unmatched communications"
# table also has a click-to-expand full preview, headers included):
python scripts/resolve_communication.py --message-id <id> --show

# Attach one to the right job (add --create for a genuinely new lead):
python scripts/resolve_communication.py --message-id <id> --company "<company>" --title "<title>"

# On-demand PDF of one job's full communications history:
python scripts/export_communications.py --company "<company>" --title "<title>"
```

Matching is tiered, cheapest first: thread id already linked to a job → a
known `job_contacts.email` → (opt-in) one cached LLM call to extract a
company/title and fuzzy-match it → otherwise parked in the
`unmatched_messages` queue for a human. See
`src/job_tracker/pipeline/comms_match.py`'s module docstring for the full
tier breakdown, and `scan_communications.py`'s docstring for why Sent-folder
scanning only ever uses Tier 1 (never bills an LLM call, never parks an
unmatched outbound message — Sent folders carry plenty of non-recruiting
mail).

**Deliberately no "happy path" for what Tier 3 extracts (2026-07-17
refinement).** If the LLM extraction can't confidently pull out a company
*and* a title, the message stays parked for a human — same as always. But
if it *can* pull out both:
- **Matches an existing lead** (`llm_company_title` tier): the excerpt is
  appended to that lead's `jd_text` (only while it's still `status="new"` —
  same "don't silently overwrite a triaged lead" guard `store.upsert_lead`
  applies everywhere), and the raw message is archived as a `.txt`
  `JobDocument` (`doc_type="email_txt"`) in that job's folder.
- **Matches nothing on file** (`llm_new_lead` tier,
  `MatchOutcome.is_new_lead_candidate`): a brand-new stub lead is created
  (scored with the free rule-based pass only) — but that's *all* that
  happens. No ATS lookup, no `no-LLM-review`/`full-LLM-review` docx, no
  résumé/cover-letter generation. Getting from there to an actual reviewed,
  packaged lead is still `apply_package.py` (or the next
  `render_pending_actions.py` rescore for the free score) — a deliberate
  choice so a vague LinkedIn pitch never turns into a fully-packaged
  application with nobody having read the JD.

Wired into `recruiting-automation/run_cycle.sh`'s hourly cycle
already; `var/pending-actions.html`'s "Unmatched communications" section
surfaces whatever's still waiting.

**Recruiter contact extraction (2026-07-17).** LinkedIn InMail's `From:`
header is always a generic relay address (`inmail-hit-reply@linkedin.com`)
— useless for a contact record — but the actual recruiter's name, and
often their real email/phone, sit right in the message body: LinkedIn's
own template always renders a short "sender block" (`<Name> / Reply /
<thread URL>`), and many recruiters also sign off with a free-text block
like `Name | Company` / `Title` / `Email: ... | Cell: ...`.
`src/job_tracker/pipeline/signature.py`'s `parse_signature()` pulls both
signals out with plain regex (no LLM call) and both `scan_communications.py`
and `resolve_communication.py` now populate `job_contacts` from it
automatically. See it without touching Gmail:

```bash
list-leads --company "<company>" --title "<title>" --show-contacts
```
