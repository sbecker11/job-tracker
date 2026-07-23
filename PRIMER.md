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
                              scan_communications.py (gmail.modify — labels Linked/NeedsFollowup, see below)

Stage 7 (bounded by however many leads qualify, runs right after Stage 6 — see "Sweep leads stuck past the score gate" below)
Leads past the free-score gate, no full review yet  →  same two-tier pipeline as Stage 4, per lead  →  llm_verdict + package on pursue
                              process_awaiting_llm_review.py (no gmail access — local DB + Anthropic API only)

Stage 8 (free, runs right after Stage 5/6/7 in the hourly cycle — see "Re-sync stale labels" below)
Already-triaged mail  →  re-derive outcome from CURRENT lead verdict(s)  →  swap JobTracker/* label if stale
                              resync_labels.py (gmail.modify — label-only, no re-evaluation, no archive changes)
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
script via `tools/refresh-pending/install.sh` (`refreshpending://run`), then
reloads the SAME browser tab in place (2026-07-19 fix — it used to also
`NSWorkspace.open` the file, which popped a second window/tab on every
click; the button now passes `no_open=1` and reloads itself instead, see
`tools/refresh-pending/README.md`). **Auto-refresh** (checkbox next to it,
on by default) reloads that same tab every 5 minutes purely from disk — no
rescore, no LLM call, no Swift helper involved — just to pick up whatever
the hourly `run_cycle.sh` cycle already regenerated on its own; it skips a
cycle rather than clobbering an in-progress search-box filter, and restores
your scroll position across the reload. Browsers will ask to allow each
custom URL scheme the first time you click a `refreshpending://` /
`revealfolder://` / `setdro://` link. Every funnel table also has an
unlabeled column right after **Age (days)** — a `<select>` for that lead's
`direct_recruiter_outreach` (Undecided / Yes / No — see
models.JobLead's docstring for what it means); picking a new option fires
`setdro://` immediately (`tools/set-direct-recruiter-outreach/install.sh`),
no separate save step, no page reload — it's a fire-and-forget write
straight to `leads.db`, same pattern as the other two helpers, just for an
edit instead of a read-only action. The **Ready to apply** table also has its own
**Apply** column (2026-07-19, scoped down to just that one section on
2026-07-19 per feedback — the other 4 sections are "review it first," not
"go apply," so a live apply link there would invite skipping the review
step) — a plain `target="_blank"` link straight to that lead's `apply_url`,
reading live from the DB (not from `ApplyURL.webloc` or the docx), so it's
always current; shows a greyed-out "No link" pill instead when no apply URL
was ever captured. A horizontal strip of 5 boxes runs
target-to-farthest, left to right, each clickable to jump to its section
below:

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

## Human playbook: what to do at each decision point

The funnel section above describes what each dashboard box *means*. This is
the follow-up: given a specific lead in front of you, what do you actually
*do* — one short recipe per scenario, cross-referenced back to the exact
commands above rather than repeating them.

**Before deciding anything, check the lead's own history:**

```bash
python scripts/list_leads.py --company "Acme" --title "Software Engineer" --show-review          # the full stored verdict/dealbreaker sweep/skills alignment
python scripts/list_leads.py --company "Acme" --title "Software Engineer" --show-communications   # every archived message, oldest first
```

(Or on `pending-actions.html` itself: click the 💬 badge next to the title
for a one-click PDF of the same communications history — see
`tools/view-communications/README.md`.) A nonzero 💬 count is not
automatically a live conversation worth acting on — check it; LinkedIn/ATS
job-alert digests get archived here too, not just real recruiter replies.

**Box 3 — "Needs your decision" (`llm_verdict='review'`, `status='new'`, no
package yet):**
- **Pursue** → `python scripts/apply_package.py --company "..." --title "..." --force`
  (`--force` because the LLM's own verdict wasn't literally "pursue" — see
  Stage 4 below). This generates the package and lands the lead in box 2
  instead of box 1, which is the intended signal that a human, not the
  model, made this call.
- **Pass** → `python scripts/list_leads.py --company "..." --title "..." --set-status skipped`

**Box 2 — "Needs your decision (forced package)" (package already exists
on a non-pursue verdict — someone already ran `--force`, possibly you, in
an earlier session):**
- **Submit anyway** → same as "I decided to submit" below.
- **Pass** → `--set-status skipped`, same as above.

**Box 5 — "JD unresolved" (`verdict='REVIEW NEEDED'`, no usable JD text):**
Find the real posting (see "Resolving JDs for link-only digest leads"
below), then ask the assistant to run the full chain against the text you
found: store it as `jd_text` → free rescore (`no_llm_review.py --write`) →
full LLM review if it clears the gate (`apply_package.py`). Don't bother if
the lead already has `jd_resolved=1` and a stored `llm_verdict` — that
means Stage 2's review already ran; only worth re-running if the JD you
found is materially different from what's on file (check with
`--show-jd-text` first).

**Box 4 — "Awaiting full-LLM-review":** nothing to decide yet — either wait
for the next hourly cycle, or force it now:
```bash
python scripts/apply_package.py --company "..." --title "..."   # no --force: respects both gates
```

**"I decided to submit an application":**
1. Check the apply URL first — on the dashboard, the Apply button/link
   already reads `apply_url` live; if it shows "No link" instead, tell the
   assistant the URL you found so it can be added (there's no CLI flag for
   this today — see Stage 4's "Which URL wins" note for how it's normally
   captured automatically).
2. Fill out and submit the application using the docx package already on
   disk. Nothing to tell the assistant mid-way — there's no "in progress"
   status in `LEAD_STAGES` (`package_generated` → `applied` is a direct
   jump), so narrating "I'm starting" doesn't move anything.
3. Once submitted: `python scripts/list_leads.py --company "..." --title "..." --set-status applied`
   — do this even though a post-application confirmation email will often
   auto-advance the status on its own (see below); the email can lag by
   hours and you already know the real answer right now.

**What's automatic now vs. what still needs you to say so (2026-07-22):**
Once a lead is linked to an inbound reply — by `triage_recruiter_inbox.py`,
`scan_communications.py`, or `triage_imap_inbox.py` — a rejection,
"application received" confirmation, or interview invite in that message's
text auto-advances `status` on its own (`pipeline/post_application.py`),
respecting a forward-only guard so nothing already past that stage gets
walked backward. This covers the passive case (a company/ATS emails you
first); it does **not** cover you *deciding* to act (submitting, following
up, withdrawing) — those are still always the human calling it, via
`--set-status` as above.

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
- `--force-llm-review` (2026-07-18) bypasses only gate 2 — still respects
  gate 3, so a résumé/cover letter only gets generated on an actual "pursue"
  verdict from the LLM. Use this to get the LLM's nuanced read on a lead
  sitting in the 50–69% band (rule-based `pursue_min_pct`..`llm_review_min_pct`)
  without blindly generating documents for whichever ones it ends up passing
  on — unlike bare `--force`.
- `JobDescription.docx` always includes an **Apply URL** line right under the
  heading (added 2026-07-18, backfilled into every pre-existing package too)
  — the submission link survives even after the source email is archived/deleted.
  Every package folder also gets a companion **`ApplyURL.webloc`** (added
  2026-07-19) — a real double-clickable Finder shortcut straight into the
  browser, since the plain-text line in the docx isn't even a clickable
  hyperlink. `pending-actions.html`'s per-lead **Apply** button (below) reads
  straight from the DB instead and doesn't need either file.
- **Which URL wins, and why it matters (fixed 2026-07-19):** `apply_url`
  prefers the ATS-resolved canonical posting URL (`boards-api.greenhouse.io`
  et al.) over whatever URL the source email itself carried, specifically
  when that email URL is a `linkedin.com` link — see
  `pipeline/run.choose_apply_url`'s docstring. LinkedIn's own "job reminder"
  notification emails carry single-use, time-limited tracking redirects
  (`trackingId=`, `midToken=`, `otpToken=`, ...) that silently expire into a
  bare LinkedIn *search* for the URL text itself — "...did not match any
  documents." Before this fix, that expiring link always won even when a
  durable ATS URL was available, all the way through to `apply_url` on the
  lead. `scripts/backfill_apply_urls.py` did a one-time repair pass over
  every already-stored lead affected the same way (re-resolves via the ATS
  APIs and swaps in the canonical URL when a confident match exists);
  `scripts/backfill_apply_url_weblocs.py` is the equivalent one-time catch-up
  for `ApplyURL.webloc` on packages generated before that file existed.

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
| 6 (`scan_communications.py`) | Gmail (**read/write** — `gmail.modify`, since 2026-07-19), Anthropic API only with `--llm-fallback` | Free (thread-id/contact-email matching) + ~$0.001–0.01/message with `--llm-fallback` (Haiku, cached per message_id); labels + archives/leaves-in-inbox the message it touches |
| 7 (`process_awaiting_llm_review.py`) | Local SQLite + Anthropic API only (no Gmail access) | Same per-lead cost as Stage 4's full review (~$0.05–0.10) + résumé/cover letter (~+$0.10–0.20) on a pursue — but only for leads that already cleared the free-score gate, so cost scales with backlog size, not a flat per-cycle rate |
| 8 (`resync_labels.py`) | Gmail (**read/write** — `gmail.modify`) only, no Anthropic API calls | Free — pure label swap based on already-stored verdicts, no re-evaluation, no INBOX/archive changes |

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
# Writes to var/leads.db AND to Gmail (gmail.modify, since 2026-07-19): a
# resolved inbound message gets JobTracker/Linked + is archived; a parked
# (unmatched) one gets JobTracker/NeedsFollowup and is LEFT in the inbox —
# see "Making Gmail labels trustworthy enough to stop checking it directly"
# below for why. --dry-run skips both the DB and Gmail writes.
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
  happens here. No ATS lookup, no `no-LLM-review`/`full-LLM-review` docx, no
  résumé/cover-letter generation *in this step* — a deliberate choice so a
  vague LinkedIn pitch never turns into a fully-packaged application with
  nobody having read the JD. Stage 7 (`process_awaiting_llm_review.py`,
  below) is what actually finishes the job on an hourly cadence once that
  stub's free rule-based score clears the review gate — before Stage 7
  existed, a real gap: leads sat here for 12+ days with a 100% match and
  zero further action, since nothing ever revisited them.

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

## Stage 7 — Sweep leads stuck past the score gate with no full review yet

Background (2026-07-19, found live): `var/pending-actions.html`'s "Awaiting
full-LLM-review" bucket — leads whose free rule-based score already cleared
`llm_review_min_pct` but have no `llm_verdict` yet — carried a code comment
promising this was "purely a 'wait for the pipeline' state." No step in
`recruiting-automation/run_cycle.sh` actually did that waiting-for; nothing
ever revisited the bucket. Verified live: **21 leads** sitting there, several
12+ days old (one at a 100% rule-based match), all landed there by one of
two paths — most commonly Stage 6's own stub-lead creation (deliberately
rule-score-only, see above), but a normal digest whose score cleared the gate
before Stage 5's real LLM call got to it can land here too.

```bash
python scripts/process_awaiting_llm_review.py             # sweeps + processes, live
python scripts/process_awaiting_llm_review.py --dry-run    # lists candidates, touches nothing
python scripts/process_awaiting_llm_review.py --limit 5    # spend circuit-breaker for a backlog catch-up run
```

For every candidate (`store.list_leads_awaiting_full_llm_review`: `status='new'`,
a real rule-based verdict — not the "REVIEW NEEDED" unresolved-JD marker,
that's Stage 1's problem, not this one — JD text on file, no `llm_verdict`
yet, and `match_pct` at or above the same gate `apply_package.py` uses), this
runs the identical `pipeline/llm_apply.generate_two_tier_package` call
`apply_package.py` runs by hand for one lead: full LLM review always (these
already cleared the score gate that decides whether that's worth spending
on), résumé + cover letter only on an actual "pursue" verdict — and advances
`status` to `package_generated`/`skipped` the same way Stage 5 does after its
own call, so there's exactly one code path for that state transition, not
two. Safe to run every hour: a lead only ever leaves the candidate set (by
getting an `llm_verdict` stamped), so nothing gets billed twice.

Wired into `recruiting-automation/run_cycle.sh`'s hourly cycle, right after
Stage 6.

## Stage 8 — Re-sync stale JobTracker/* labels (making Gmail trustworthy enough to stop checking it directly)

Background (2026-07-19): `triage_recruiter_inbox.py` (Stage 5) applies its
`JobTracker/PURSUE|SKIP|NEEDS_REVIEW` label exactly once, at initial triage —
often from just the free rule-based pass, before a full LLM review has even
run. Nothing after that ever revisits it: a later `apply_package.py
--force-llm-review`, a `run_full_llm_review_for_pursue_leads.py` batch, or a
human calling `list_leads.py --set-status` can all change a lead's effective
verdict, and the Gmail label just sits there, now wrong. Verified live: a
handful of leads whose initial "pursue" was later overturned to "pass" by
the full LLM review still carried `JobTracker/PURSUE` in Gmail weeks later.

That staleness matters for a specific reason: the whole point of labeling
`Category/recruiter_job` mail in the first place is so you can eventually
stop reviewing recruiting email directly in the Gmail client and trust this
pipeline + `var/pending-actions.html` instead — maybe even build a
client-side filter on top of the label. Neither works if the label can't be
trusted to reflect the CURRENT decision.

```bash
python scripts/resync_labels.py            # re-syncs, live
python scripts/resync_labels.py --dry-run  # prints what WOULD change, touches nothing
```

For every message `triage_recruiter_inbox.py` already labeled (tracked in
`processed_messages.lead_keys`), this re-derives today's outcome from the
CURRENT `job_leads.llm_verdict` (falling back to the rule-based `verdict` if
no full review has run yet) for every lead that message is linked to — same
PURSUE > NEEDS_REVIEW > SKIP priority rule initial triage uses
(`pipeline.triage.decide_outcome_from_verdicts`) — and swaps the label if
it's changed. No LLM spend (it never re-evaluates anything, just reads
already-stored verdicts) and no INBOX/archive changes (whether a message is
visible in the inbox was decided once, based on extraction completeness, and
has nothing to do with verdict drift). Wired into
`recruiting-automation/run_cycle.sh`'s hourly cycle, right after Stage 7.

Together with Stage 6's new `JobTracker/Linked` / `JobTracker/NeedsFollowup`
labels on `Category/social` traffic, this is what closes the loop: every
category of recruiting mail this pipeline touches now carries a label that
reflects its CURRENT state, not just a snapshot from whenever it was first
seen. What's left un-labeled or still carrying `NeedsFollowup` /
`NEEDS_REVIEW` is, by construction, exactly what still needs a human look —
either directly in Gmail or via `var/pending-actions.html`.
