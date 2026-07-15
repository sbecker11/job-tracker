# Job CRM Vision — Use Cases & Entity Model

**Status:** design doc, personal-use scope only (see [Non-goals](#non-goals-v1)).
**Owner:** `job-tracker` (contacts *data* stays owned by `comms-migration`; see
[Relationship to comms-migration](#relationship-to-comms-migration)).

**Implementation status (2026-07-14):** UC-1, UC-2, UC-3 (`scripts/add_job.py`),
UC-4 (`scripts/attach_document.py`), UC-5 (`scripts/log_contact.py --meeting`)
are built. UC-6's data (`awaiting_response_since` — see the job-tracker
README's "Whose turn is it" section) and message drafting for it
(`scripts/generate_message.py --kind status_check_in`, plus `thank_you` for
post-interview) are built; a standalone periodic "these N jobs have gone
quiet" sweep report is not yet built (`--waiting` on `list_leads.py` covers
the on-demand case). UC-7 (offer comparison), UC-8 (market-withdrawal
drafting), and UC-9 (transition validation) are not yet built.

## 1. Purpose

`job-tracker` today does one thing well: turn a recruiter email into a scored
lead and, optionally, a résumé/cover-letter package (`pipeline/triage.py`).
That's a single-shot, email-triggered pipeline. The gap the user identified:
a job search is not one event, it's a relationship that spans weeks or months
across multiple contacts, conversations, and documents — and today nothing
connects those together once the triggering email has been triaged.

This doc defines the use cases and entity model for treating **Job as a
first-class object** with a real lifecycle, so the five problems below have
somewhere to live:

1. Losing the connections between a job, its contacts, conversations, and
   documents (posted URL, JD, cover letter, résumé, RTR, availability sent,
   scheduled meetings).
2. Not noticing when multiple recruiters are contacting you about the same
   job.
3. Following up with the right contact on stale applications or new
   opportunities.
4. Comparing multiple final offers side by side.
5. Notifying recruiters you've accepted an offer and are off the market.

## 2. Guiding principles

- **Personal use first.** No marketization work now (see the earlier
  discussion on the crowded Teal/Huntr/Simplify market and the Google OAuth
  CASA-verification cost of a multi-tenant product). Everything below is
  scoped to a single user, single SQLite file, no auth.
- **Don't duplicate what comms-migration already owns.** `contacts/Contacts.yaml`
  is already a real address book (dedup, hub assignment, a web UI). job-tracker
  should *reference* a contact there, not re-implement name/email/phone
  storage.
- **Foundation before features.** Build the entity model + schema first, wire
  today's triage flow into it, then layer the 5 use cases on top as queries/
  reports over that model rather than five bespoke subsystems.
- **Human-in-the-loop for anything that leaves the building.** Drafting a
  withdrawal note to a recruiter is automatable; *sending* it is not (mirrors
  the `human_in_loop` flag already used throughout `rules/actions.yaml`).

## 3. Relationship to comms-migration

| Concern | Owner | Why |
|---|---|---|
| Who a person is (name, email, phone, LinkedIn) | `comms-migration` (`contacts/Contacts.yaml`) | Already exists, already deduped, already has a web UI. |
| Which hub a sender routes to (Professional/Personal) | `comms-migration` (`rules/senders.yaml`) | Unrelated to job search specifically. |
| Which job a contact is associated with, what they said, when to follow up | `job-tracker` (new tables, §4) | Job-search-specific relationship data; comms-migration has no concept of "job." |

job-tracker's `JobContact` row stores a `contact_ref` that *points at* a
`contacts/Contacts.yaml` id when a match is found there (by email), and falls
back to a locally-known name/email pair when it isn't (e.g. a recruiter who
emailed once and was never added to the address book). It never writes back
to `Contacts.yaml` — read-only linkage, one direction, consistent with the
existing repo boundary (`comms-migration` owns contacts data; `job-tracker`
does not).

## 4. Entity model

```
Job (job_leads row — already exists, keyed by normalized_key)
 │  status: LEAD_STAGES lifecycle (new -> ... -> accepted/skipped)
 │
 ├── JobContact[]        who is involved (recruiter, hiring manager, referral)
 │     └── contact_ref -> comms-migration Contacts.yaml id, or local name/email
 │
 ├── JobConversation[]   what was said, and when (email thread, call, etc.)
 │     └── linked to exactly one JobContact
 │
 ├── JobDocument[]       JD snapshot, résumé, cover letter, RTR, availability sent
 │     └── versioned (multiple résumé versions per job, most recent wins by default)
 │
 ├── JobMeeting[]        scheduled interviews/calls
 │     └── linked to one or more JobContacts
 │
 └── JobOffer[]          comp, benefits, deadline — for side-by-side comparison
```

`Job` itself is the existing `job_leads` table; nothing about its schema
needs to change for this doc except adding the join tables above (see
`schema_foundation` in the implementation plan).

## 5. Use cases

Each use case names the actor (always "you," the single user, since this is
personal-use only) and the trigger/flow/outcome.

### UC-1 — Ingest a recruiter email (existing, unchanged)
Trigger: new `Category/recruiter_job` message in `shawnbecker.recruiting@gmail.com`.
Flow: `triage_recruiter_inbox.py` -> `pipeline/triage.py` -> extract role(s) ->
resolve JD -> LLM-evaluate -> maybe generate package -> `upsert_lead`.
Outcome: unchanged from today. New: also creates a `JobContact` (the sender)
and a `JobConversation` (the triggering message) linked to the resulting Job.

### UC-2 — Detect "multiple recruiters, same job"
Trigger: a new inbound message's extracted (company, title) fuzzy-matches an
existing Job that already has a *different* `JobContact`.
Flow: dedupe check (§6) runs before `upsert_lead` creates a new Job. On a
match, the new sender is added as an additional `JobContact` + `JobConversation`
on the *existing* Job instead of creating a duplicate; `job_leads.times_seen`
increments as it does today.
Outcome: you see one Job with N contacts, not N duplicate Jobs. Surfaced via
`list-leads --show-contacts` (or similar) so you notice a hot role.

### UC-3 — Add a job manually (non-email origin)
Trigger: you find a role via a company's careers page, a referral, or a
conversation that never went through email.
Flow: a new CLI (`add-job` or similar) creates a Job with `source_label =
"manual"` and no `source_message_id`. Everything downstream (documents,
meetings, offers) works the same regardless of how the Job was created.
Outcome: the model doesn't assume every Job originates from an email —
addressing the gap flagged in the "Job as first-class object" discussion.

### UC-4 — Attach and retrieve documents
Trigger: a JD is resolved, a résumé/cover letter is generated, an RTR is
signed, or you send a recruiter your availability.
Flow: each is stored as a `JobDocument` row (type + path/URL + version +
timestamp) linked to the Job.
Outcome: `list-leads --show-documents <job>` (or a detail view) shows every
artifact ever produced for that job in one place — the "losing connections"
problem, directly addressed.

### UC-5 — Schedule and track meetings
Trigger: a recruiter proposes times, or an interview is confirmed.
Flow: a `JobMeeting` row records who, when, what kind (phone screen, onsite,
technical, etc.), and status (proposed/confirmed/completed/cancelled).
Outcome: no more losing track of "did we confirm Tuesday or Wednesday" — and
a query can answer "what's on my interview calendar this week" across jobs.

### UC-6 — Follow-up nudges
Trigger: periodic (e.g. daily) sweep, or an on-demand CLI command.
Flow: find Jobs where `status` is `applied`/`following_up`/`interviewing` and
the most recent `JobConversation.occurred_at` for that Job is older than a
configurable threshold (e.g. 10 business days).
Outcome: a report: "these N jobs have gone quiet — consider following up
with <contact>." Never auto-sends anything (human-in-the-loop).

### UC-7 — Compare final offers
Trigger: two or more Jobs reach `status = offered`.
Flow: a report reads every `JobOffer` row for Jobs in `offered` status and
renders a comparison table (base, bonus, equity, benefits notes, deadline,
location, plus the Job's own `llm_match_pct`/`llm_verdict` for context).
Outcome: a single side-by-side view instead of re-reading old emails.

### UC-8 — Notify the market you're off it
Trigger: a Job moves to `status = accepted`.
Flow: gather every `JobContact` across every *other* Job not in a terminal
state (`skipped`, or itself `accepted`), and draft (never auto-send) a
withdrawal note per contact, grouped so you can review before sending.
Outcome: nobody keeps pitching you roles you can no longer take, and you look
professional to recruiters you may want to work with again later — but you
stay the one who hits "send."

### UC-9 — Validate state transitions
Trigger: any `advance_status` call (or a new CLI to move a Job manually).
Flow: transitions are checked against an explicit allowed-transitions table,
not just "any string in `LEAD_STAGES`." An invalid transition (e.g.
`new -> accepted`, skipping every intermediate stage) is rejected with a
clear error rather than silently accepted.
Outcome: the DB stays a trustworthy timeline, not just a mutable label.
*(Left as an explicit open question in §7 — needs your input on how strict
this should be, since real job searches sometimes do skip stages.)*

## 6. Dedupe strategy for UC-2

Proposed default (flag if you'd rather do it differently): fuzzy-match on
normalized `(company, title)` — reusing the same `SequenceMatcher`-based
approach `contacts/store.py` already uses for organization-name dedup
(`organizations_match_for_dedup`, ratio >= 0.92) — and, when the JD text is
available for both, a secondary text-similarity check before merging. Below
threshold on company/title alone: no merge, even if the JD text looks similar
(different companies can legitimately post near-identical JDs from the same
template). Ambiguous cases (similarity 0.75–0.92) are surfaced for manual
confirmation rather than auto-merged or auto-rejected.

## 7. Open questions

These are genuine design decisions still needed from you, deliberately not
guessed at:

1. **Transition strictness (UC-9):** hard-reject invalid transitions, or just
   log a warning and allow them? Real searches sometimes really do skip
   stages (e.g. an offer with no formal "interviewing" stage logged).
2. **Job identity independent of extraction:** should two Jobs ever be
   considered "the same role" even if `(company, title)` differs a lot (e.g.
   a role gets re-titled mid-process)? If so, what's the merge trigger —
   manual only, or can the system suggest it?
3. **Follow-up cadence (UC-6):** what's the default "gone quiet" threshold
   per stage? Likely different for `applied` (no response yet) vs.
   `interviewing` (mid-loop).

## 8. Non-goals (v1)

- Marketization, multi-user support, or anything requiring Google OAuth
  verification/CASA assessment.
- Two-way sync with `comms-migration`'s `Contacts.yaml` (read-only linkage
  only, per §3).
- Auto-sending anything (withdrawal notes, follow-ups) without you reviewing
  it first.
- A UI beyond CLI reports — no web app for this yet.

## 9. Extensibility: beyond job emails

See `docs/CATEGORY_HANDLER_EXTENSIBILITY.md` for how this same
classify -> extract -> decide -> label/archive pattern (currently hardwired
to `Category/recruiter_job` -> `JobLead`) generalizes to other
`comms-migration` categories in the future, without a rearchitecture.
