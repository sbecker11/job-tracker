# Requirements — recruiting pipeline & Pending Actions

**Read this before changing behavior anywhere in `job-tracker/`.** It states
what the system must do, in numbered rules. Code that disagrees with a rule is
a bug, unless the rule is marked **DECIDE** — then Shawn decides and this file
is updated first.

- **Candidate facts** (experience, dealbreakers, house rules, comp floor,
  package craft) live only in `~/CLAUDE.md`. Rules here cite its sections;
  never copy profile facts into this file.
- **Mail routing** is owned by `comms-migration/`; **scheduling / HALT** by
  `recruiting-automation/`. Section 2 lists only what job-tracker depends on.
- Surveyed from the code on 2026-10-22. File references name the file and
  function, not line numbers (lines drift).

## How to use this file (any agent)

1. Find the rule IDs for the area you're touching. If none covers your change,
   add one in the same PR.
2. Every rule names what **enforces** it:
   `test:` a pytest file · `check:` a card-check id (`card_audit.py`, runs after
   every regenerate) · `—` nothing yet.
3. Status marks: **OK** enforced and true today · **GAP** true but unenforced ·
   **BROKEN** code violates it today (see §13) · **DECIDE** conflicting
   behavior; needs Shawn's call (see §14).
4. A behavior change = edit the rule → add/adjust the test or check → change
   code. A bug fix = add the missing test/check so it can't come back.
5. Each decision is computed in **one** place (§12). Don't add a second copy;
   call the owner.

---

## 1. Glossary

| Term | Meaning |
|---|---|
| **Lead** | One job opportunity — `job_leads` row, key `company::title` (lowercased). |
| **Contact** | A person or inbox tied to a lead — `job_contacts`. |
| **Conversation** | One logged message on a lead — `job_conversations` (inbound / outbound / other). |
| **Unmatched message** | Mail not yet tied to a lead — `unmatched_messages`. |
| **Stub lead** | Placeholder lead ("End-client TBD via <name>", "Role TBD") created so a conversation has a home. |
| **Card** | One row on the Pending Actions page. |
| **Stage** | Card column: Clarify → Send résumé → Wait → Decide/apply. |
| **Channel** | How a conversation travels: `linkedin`, `email`, `sms`, `other`. |
| **Relay** | An address that isn't a person: LinkedIn notification/reply relays, job-board senders, no-reply, role mailboxes (careers@, hr@…). |
| **Thread link** | `https://www.linkedin.com/messaging/thread/<id>/` for a LinkedIn conversation. |
| **Send route** | A button that gets a message to the recruiter (Gmail compose, LinkedIn thread, text…). |
| **Turn** | Whose move it is. `awaiting_response_since` set = waiting on them. |

---

## 2. System shape and external contracts

| ID | Rule | Enforced | Status |
|---|---|---|---|
| SYS-1 | Hourly cycle (`recruiting-automation/run_cycle.sh`) runs, in order: classify → Gmail triage → scan sent+received → Spexture IMAP → iCloud IMAP → promote unmatched (LLM) → auto-skip → LLM review sweep → resync labels → render Pending Actions → render contacts. | test: `recruiting-automation/tests/test_schedule_rules.bats` | OK |
| SYS-2 | A 3-minute fast tick (`scripts/comms_fast_cycle.py`) runs the same intake steps with no package generation: job-tracker steps run `--offline --no-generate` (no LLM), and Pending Actions re-renders with `--no-rescore`. Its first step, comms-migration's classifier, may call the LLM for a brand-new sender (verdict cached per sender). | test: `test_schedule_rules.py` | OK |
| SYS-3 | Every scheduled writer to `var/leads.db` holds `var/comms_fast.lock` (`with_db_lock.py` / `db_lock`). Scripts called by a lock holder never take the lock themselves. The page's helper apps (decline-lead, set-lead-status, RecordReply, refresh-pending) write without the lock: their writes are single short transactions covered by WAL + a 30 s busy timeout, so a click never waits out an hourly step. (Decided 2026-10-22.) | test: `test_schedule_rules.py`, `recruiting-automation/tests/test_schedule_rules.bats` | OK |
| SYS-4 | Any hourly step failing or timing out (1800 s) writes `state/HALT` and stops the schedule; exit 75 (lock busy) retries once after 60 s. Gmail quota/network errors defer, never HALT. HALT means stopped on purpose; agents don't remove it. | test: `recruiting-automation/tests/test_schedule_rules.bats`, `recruiting-automation/tests/test_cycle_safety.bats`; deferral: comms-migration `test_run.py`, `test_triage_recruiter_inbox.py` | OK |
| SYS-5 | HALT stops the hourly cycle only; the 3-minute intake tick keeps running, and a login (`ensure_running.sh`) restarts the schedule. (Decided 2026-10-22.) | test: `recruiting-automation/tests/test_schedule_rules.bats`, `recruiting-automation/tests/test_ensure_running.bats` | OK |
| SYS-6 | job-tracker reads Gmail `label:Category/recruiter_job` (set by comms-migration) minus its own `JobTracker/*` labels, LinkedIn replies by sender (`hit-reply@` / `inmail-hit-reply@linkedin.com`, which comms-migration files under `Category/social`), plus the Spexture and iCloud IMAP mailboxes. comms-migration never touches `leads.db`. | test: `test_schedule_rules.py` | OK |

---

## 3. Intake: direction, channel, identity

| ID | Rule | Enforced | Status |
|---|---|---|---|
| ING-1 | **Direction comes from the message, not the code path.** A reply or new message from one of Shawn's own addresses (`store.OWN_ADDRESSES`: `*@spexture.com`, `shawnbecker.recruiting@gmail.com`, `scbboston@gmail.com`) is outbound — logged by the Gmail Sent scan — and is never triaged as inbound. | test: `test_own_address.py` | OK (2026-10-22; older rows cleaned the same day) |
| ING-1b | **Forwards are input, not replies.** Mail Shawn forwards to himself (`Fwd:` / `FW:`) is triaged on the forwarded content as a new item; it never matches a lead by his own address. | test: `test_own_address.py` | OK (2026-10-22) |
| ING-2 | **One message, one conversation row.** The same message seen via Gmail and IMAP, or by two scripts, is logged once. Every intake path uses `store.is_communication_seen` (checks processed, conversations and unmatched) before logging, and `add_job_conversation` refuses a second inbound copy on the same lead (`store.find_duplicate_inbound`: same id, or same subject within 3 h with a ≥ 85 % similar body). | check: `duplicate_inbound`; test: `test_duplicate_inbound.py` | OK for new mail (2026-10-22); older copies cleaned on every lead with a live card (48 copies on 23 leads); older leads without cards still have some |
| ING-3 | **Channel is set at intake** from the sender: any `linkedin.com` sender or LinkedIn thread link → `linkedin`; call/phone/text logs → `sms`; else `email`. Later stages read the stored channel; they don't re-derive it. | check: `email_on_linkedin` | **BROKEN** — intake always writes `email`; LinkedIn is inferred later in ~7 places |
| ING-4 | Message ids: Gmail hex id; IMAP `imap:<Message-Id>` (or `imap-uid:<folder>:<uid>`); synthetic `status-sent:`, `package-sent:`, `dismissed:`, `reply-recorded:`. | test: `test_imap_reader.py` | OK |
| ING-5 | `occurred_at` is the message's own send time (Date header), not the scan time. | — | **BROKEN** — scan time is used |
| ING-6 | Shawn's own addresses are never saved as a contact (`add_job_contact` returns None) and never match a lead (`find_job_by_contact_email`). | test: `test_own_address.py` | OK (2026-10-22; older own-address contacts cleaned the same day) |
| ING-7 | Unmatched copies of one conversation collapse: same LinkedIn thread id, or same sender + normalized subject within 3 h. A copy of an already-resolved/dismissed message inherits that outcome. | test: `test_unmatched_dedupe.py` | OK |

## 4. Matching messages to leads

| ID | Rule | Enforced | Status |
|---|---|---|---|
| MAT-1 | Match order: (1) same thread id, (1b) same LinkedIn thread link held by exactly one lead, (2) the other party's email is a contact on the lead, (3) company + title fuzzy match ≥ 0.92 (0.75–0.92 = ambiguous, stays unmatched), (4) LLM extraction (hourly only). | test: `test_communications.py`, `test_unmatched_dedupe.py` | OK |
| MAT-2 | Relay addresses never match by email (tier 2). One canonical relay list lives in `store.GENERIC_RELAY_ADDRESSES`; `@reply.linkedin.com` matches only via a contact's `linkedin_reply_to`. An address on ≥ 3 leads is a broadcaster and doesn't match. Shared platform domains (`ashbyhq.com`, `greenhouse-mail.io`, `linkedin.com`, the job boards) never match, even on one lead; a company's own no-reply sender still does. | test: `test_communications.py`, `test_linkedin_reply_to.py`, `test_platform_sender_match.py` | OK (but the list is copied in 3 files — §12) |
| MAT-3 | When a message on an agency/TBD lead names exactly one other live lead's company and title, it belongs to that lead. | — | GAP |
| MAT-4 | Gmail and IMAP copies of the same thread must resolve to the same lead (thread ids differ across mailboxes, so match by Message-Id / References too). | test: `test_cross_mailbox_thread.py` | OK — 2026-10-22: both readers keep Message-Id, References and In-Reply-To; conversations store the Message-Id; `comms_match` tier `message_id_ref` runs right after the thread-id tier and only links when every hit is one lead. The same email from the other mailbox is also caught as a duplicate by Message-Id. Gmail rows logged before today have no stored Message-Id, so only IMAP-first threads match backward |
| MAT-5 | Unmatched inbound goes to `unmatched_messages` (Gmail label `JobTracker/NeedsFollowup`). Unmatched outbound is logged once it can be matched later, not dropped. | test: `test_communications.py` | OK — 2026-10-22: unmatched outbound isn't marked seen, so each run retries it until it matches or ages out of the 14-day `--newer-than` window |
| MAT-6 | A reply Shawn records for an unmatched message (`record-unmatched-reply`) creates or reuses a stub lead, resolves every copy, logs the reply outbound and starts the wait clock. | test: `test_record_unmatched_reply.py` | OK |

## 5. Contacts

| ID | Rule | Enforced | Status |
|---|---|---|---|
| CON-1 | A contact's **name** is a person's name — never an email address or relay. When the lead has none, take it from the signature or the LinkedIn sender block (including the InMail "Name, PhD / headline / profile link" layout). | check: `email_as_name`, `nameless_greeting`; test: `test_contact_fields.py`, `test_qualifying_reply.py` | OK (2026-10-22; `store.clean_contact_fields`, `name_from_email` for first.last addresses, never guessed otherwise) |
| CON-2 | A contact's **email** is a real, person-reachable address: not a relay, role mailbox, job board, Shawn's own, or a URL. A `@reply.linkedin.com` address goes to `linkedin_reply_to`, never `email`. | check: `bad_email`; test: `test_contact_fields.py` | OK (2026-10-22; one rule, `store.is_person_email`, used by the page builder and the card check; LinkedIn links typed as email move to `linkedin_url`) |
| CON-3 | Phones keep their label. A number labelled office / main / desk / fax is callable but not textable. | test: `test_reply_plan.py`; check: `text_to_office_line` | OK — 2026-10-22: the reply plan offers Call, not Text, for an office-labelled number |
| CON-4 | Contacts dedupe within a lead by email, then `linkedin_reply_to`, then name; a repeat only fills missing fields. | test: `test_store.py` | OK |
| CON-5 | One contact-building function is used by every intake path (signature parse, Reply-To, relay filtering). | test: `test_contact_intake.py` | OK — 2026-10-22: `pipeline/contact_intake.record_message_contact` is used by Gmail triage, IMAP triage and the communications scan, including the new-lead path, which used to save the raw From address (relay senders included). A digest that names several roles skips the signature |
| CON-6 | "The recruiter" for a lead is chosen one way everywhere: role `recruiter` first, then the most recently active human contact. | — | **BROKEN** — five different picks (§12) |

## 6. Turn tracking (whose move)

| ID | Rule | Enforced | Status |
|---|---|---|---|
| TURN-1 | Outbound sets `awaiting_response_since` to its time; inbound from the other party clears it; `other` leaves it. Explicit overrides allowed (e.g. voicemail left). | test: `test_store.py`, `test_log_contact.py` | OK |
| TURN-2 | Automated mail (digests, auto-acks, job alerts) never counts as the recruiter replying. | test: `test_pending_workflow.py` (digest) | OK |
| TURN-3 | Timestamps are compared as parsed datetimes, never as strings. | test: `test_timestamp_order.py` | OK (2026-10-22; 27 stored rows use `-06:00`, which sorted wrong as strings). SQL `ORDER BY` for display is unchanged. |

## 7. Evaluation (scoring and review)

Profile inputs: dealbreakers `~/CLAUDE.md` §3, framework §11, comp floor §2.
Machine copy: `config/framework.yaml` (kept in sync by `verify_framework_sync`).

| ID | Rule | Enforced | Status |
|---|---|---|---|
| EVAL-1 | Rule-based score: dealbreaker sweep (load-bearing = hits in hard-requirement text ≥ `min_hits`; "nice to have" sections don't count), then match % = matched skill weight / recognized JD weight; < 5 recognized weight → 0 %. | test: `test_scorer.py` | OK |
| EVAL-2 | Verdict bands come only from `framework.yaml` (pursue ≥ 50, review ≥ 25). No code default may differ. | — | OK (2026-10-22; fallbacks now 50/25) |
| EVAL-3 | The LLM review gate (match ≥ 70 %, `thresholds.llm_review_min_pct`) is read from `framework.yaml` in one place. | test: `test_scorer.py` | OK (value duplicated in 4 files — §12) |
| EVAL-4 | A rule-based "pursue" (50–69 %, below the LLM gate) may move a lead to `pursued` without an LLM review. (Decided 2026-10-22.) | — | GAP |
| EVAL-5 | Dealbreaker keywords must not misfire: "1099 only" is not C2C (§3 allows 1099 direct); "go" as English isn't Go; `java` doesn't match JavaScript; each vocabulary term is counted once. | test: `test_scorer.py` (`test_eval5_keywords_do_not_misfire`) | OK (2026-10-22; Go matches only capitalised "Go", not "Go-To-Market"/"Go through"; `\bjava\b`; duplicate `ansible` removed; the scorer counts a term once) |
| EVAL-6 | US-citizen / no-sponsorship JD requirements are a clean ✅ in every evaluation (§2, §3). | test: `test_no_llm_review.py` | OK |
| EVAL-7 | LLM evaluation returns a verdict in {pursue, review, pass}; anything else is rejected and retried, not defaulted. | test: `test_llm_apply.py` | OK (2026-10-22; one retry, then `LLMApplyError`) |
| EVAL-8 | Unresolvable JD (gated link / thin snippet) → `match_pct = 0`, verdict `REVIEW NEEDED` (§12). | test: `test_llm_apply.py` | OK |
| EVAL-9 | A rejection from the same company+role within 90 days forces `pass` in every evaluation path, including the LLM sweep. | tests: `test_triage.py`, `test_process_awaiting_llm_review.py` | OK (2026-10-22; the sweep skips the lead with a note before any LLM call) |
| EVAL-10 | Cost reporting uses current model prices. | — | OK (2026-10-22). Anthropic made $2/$10 Sonnet 5's standard price, so the numbers were right; the stale comment is fixed and there is now one price table (`llm_extract`). |
| EVAL-11 | Every Anthropic API call (job-tracker and comms-migration) appends one line to `<workspace>/var/llm-calls.jsonl`; `spend_report` totals it per month and source so it can be checked against Anthropic's bills (`~/CLAUDE.md` §14). (Added 2026-10-22.) | tests: `test_llm_call_log.py`, comms `test_llm_classify.py` | OK |

## 8. Lead lifecycle

Statuses (`pipeline/models.py` `LEAD_STAGES`): forward path `new → pursued →
package_generated → applied → following_up → interviewing → offered → accepted
→ started`; off-ramps `skipped`, `rejected`, `deleted`, `unavailable`, `hired`;
`reopened`.

| ID | Rule | Enforced | Status |
|---|---|---|---|
| LIFE-1 | Automatic moves only go forward and never leave an off-ramp; manual moves (`set-lead-status`) may go anywhere and are noted. | test: `test_store.py`, `test_set_lead_status.py` | OK |
| LIFE-2 | Auto-skip `new`/`pursued` leads whose last communication is > 21 days old, or that require Eastern time/hours (§3). Each skip writes a reason note. | test: `test_auto_skip_leads.py` | OK |
| LIFE-3 | Declining sets `skipped` with a reason from `DECLINE_REASONS`, logs the letter (if sent) outbound, and stops the wait clock. A later recruiter reply that changes the declined term resurfaces the lead. | test: `test_decline.py` | OK |
| LIFE-4 | Application-received → `applied`; interview invite → `interviewing`; rejection → `rejected` (recorded from any status). Off-ramp and `reopened` leads never auto-advance. | test: `test_post_application.py` | OK |
| LIFE-5 | Generating a package on a lead moves it to `package_generated` — or the tool refuses/warns when the lead is skipped. | test: `test_apply_package_status.py` | OK — `--force` moves an off-ramp lead with a note; without it the tool warns (2026-10-22) |
| LIFE-6 | Every status value used anywhere is in `LEAD_STAGES`. | test: `test_lead_stage_values.py` (SQL status literals) | OK (2026-10-22; removed `duplicate` ×2 and `withdrawn`) |
| LIFE-7 | `scan_rejection_backlog` imports and runs. | test: `test_cli_imports.py` | OK (2026-10-22) |

## 9. Packages

Craft and naming: `~/CLAUDE.md` §4, §5, §10, §12.

| ID | Rule | Enforced | Status |
|---|---|---|---|
| PKG-1 | Every artifact lives in `~/Desktop/Resumes/2026/<Company>/<Role>/`; résumé and cover letter are generated only on an LLM *pursue* (or explicit force). | test: `test_llm_apply.py` | OK |
| PKG-2 | Post-generation safety: banned terms, work-authorization language and compensation figures are checked in **both** résumé and cover letter, and catch common phrasings ("$120,000 per year", "$75 an hour", "$80 hourly"). | test: `test_llm_apply.py` | OK (2026-10-22; both documents checked; no false hits on the 51 existing packages) |
| PKG-3 | Every résumé entry has a `role_note` title; no degree/cert years. | — | GAP |
| PKG-4 | New cover letters are named `Shawn_Becker_Cover_Letter_<Company>_<Role>.docx` (`~/CLAUDE.md` §12); existing files are not renamed. (Decided 2026-10-22.) | test: `test_llm_apply.py` | OK (2026-10-22) |

## 10. Pending Actions cards

### 10a. What appears, where

| ID | Rule | Enforced | Status |
|---|---|---|---|
| CARD-1 | Each lead or unmatched conversation appears as **one** card, in exactly one stage. The backend guarantees it (not the page's de-duplication). | tests: `test_pending_workflow.py`; card check `two_stages` | OK (2026-10-22). Precedence: Clarify > Send résumé > Wait > Decide/apply. Fixed 3 doubled leads (Intentsify, Artech, Data ML Engineer). |
| CARD-2 | **Clarify** = it's Shawn's turn to reply to a human: unanswered human inbound on a lead or unmatched message, ≤ 14 days old for first-touch pitches, ≤ 30 days for replies on live threads. | test: `test_pending_workflow.py` | OK |
| CARD-3 | **Send résumé** = résumé + cover are on disk and a person asked for the résumé (or is the direct recruiter). | test: `test_pending_workflow.py` | OK |
| CARD-4 | **Wait** = waiting on them (`awaiting_response_since` set); follow-up becomes due at `wait_followup_days` (7). | test: `test_pending_workflow.py` | OK |
| CARD-5 | **Decide/apply** = scored leads needing Shawn's verdict or an ATS application. | test: `test_render_pending_actions.py` | OK |
| CARD-6 | Age limits, deliberately different (decided 2026-10-22): first-touch Clarify cards drop after 14 days; leads auto-skip after 21 quiet days (LIFE-2); replies on live threads stay in Clarify up to 30 days; leads with no wait clock count as probably dead after 30 quiet days, waiting leads after 45. All five live in `config/framework.yaml`. | — | GAP — values are code constants today |
| CARD-7 | One "today" order across stages (`annotate_today_order`): replies owed first, then requested résumés, then due follow-ups, then ready-to-apply, then other decisions, then quiet waits, then probably-dead. Within a tier the backend's order is final; the page doesn't re-sort. | test: `test_pending_workflow.py` | **BROKEN** — renderer and page disagree on Decide order |
| CARD-8 | `replyDue`, `unansweredDays` mean the same thing on every card: days since the oldest unanswered human inbound. | test: `test_timestamp_order.py` (`_unanswered_days`) | OK (2026-10-22). The 30-day cut-off still uses the newest unanswered message, so an old thread with a fresh reply stays visible. |

### 10b. How to reach the recruiter (send routes)

| ID | Rule | Enforced | Status |
|---|---|---|---|
| ROUTE-1 | The backend computes one **reply plan** per card: channel, recipient (name / email / phone / thread link), and ordered routes. The page renders it and never re-derives channel, recipient or LinkedIn-ness. | test: `test_reply_plan.py`, `test_pending_workflow.py`, `test_next_action_wording.py` | OK — 2026-10-22: `pipeline/reply_plan.py` attaches `replyPlan` to every card; the page's `sendRoutes` only words its routes, and the Clarify step text follows the plan's first route |
| ROUTE-2 | Route order: (1) Gmail compose to a real recruiter email, prefilled; (2) LinkedIn thread; (3) LinkedIn profile; (4) the recruiter's real email thread in Gmail; (5) company inbox, only if no recruiter owns the lead; (6) Text, mobile/unlabelled numbers only; Call for any number. | test: `test_reply_plan.py` | OK |
| ROUTE-3 | A LinkedIn conversation with no real recruiter email offers **only LinkedIn routes**. LinkedIn's notification email is never offered as a reply route (replying goes to a relay); if there's no thread link, it's offered as "Open LinkedIn notification" with instructions to use its Reply button. | check: `email_on_linkedin`, `missing_thread` | OK (fixed 2026-10-22, `7982355`) |
| ROUTE-4 | Thread links are recognised in every LinkedIn form (`/messaging/thread/`, `/comm/messaging/thread/`, with or without `www`, with tracking params) and normalised to `https://www.linkedin.com/messaging/thread/<id>/`. One extractor (`qualifying_reply.extract_linkedin_thread_url`). | test: `test_qualifying_reply.py`, `test_contact_fields.py`; check: `missing_thread` | OK (2026-10-22; `_thread_url_for_lead` uses the shared extractor and falls back to a link saved on a contact) |
| ROUTE-5 | Role mailboxes (careers@, hr@, jobs@…) and addresses quoted inside a JD are never a send route. | test: `test_set_lead_status.py`, `test_contact_fields.py`, `test_page_person_email.py` | OK — 2026-10-22: the page's `extractSenderDetails` uses `isPersonEmail`, a copy of the backend rule that the test keeps in sync |
| ROUTE-7 | Every stage picks the card's channel one way (`pending_workflow._card_channel`): a real person's email → email; otherwise a LinkedIn thread link or LinkedIn source → LinkedIn. A reply link found by searching Gmail by company + title is used only if that message names the lead's full company name; hits found by the recruiter's own name or email don't need it. | check: `email_on_linkedin`; test: `test_contact_fields.py`, `test_reply_continuity.py` | OK (2026-10-22) |
| ROUTE-6 | The **Send to** box always shows what's known (name, address/number/link, subject for email) with Copy buttons, so a missing link never blocks a manual send. | — | OK |

### 10c. Next-action text

| ID | Rule | Enforced | Status |
|---|---|---|---|
| NEXT-1 | Cards show "Current status: …" and "Manage lead (N steps):" as a numbered list: review the message → the chosen route's step → click **Message sent** (omitted when the route sends and logs itself). | — | OK |
| NEXT-2 | Step wording matches the actual route. Never "LinkedIn continuity copy" on an email card, never "Open LinkedIn thread" without a link. | test: `test_next_action_wording.py` | OK — fixed 2026-10-22 |

## 11. Drafts and logging what was sent

### 11a. Drafts

House rules for every message: `~/CLAUDE.md` §4, §12 (no comp figures, no
work-authorization, no availability; asking for their band is fine).

| ID | Rule | Enforced | Status |
|---|---|---|---|
| DRAFT-1 | Greeting is `Hi <first name>,` from the lead's contact name, else the signature / LinkedIn sender block. Never an email address; `Hi,` only when no name exists anywhere. | check: `nameless_greeting`; test: `test_qualifying_reply.py`, `test_contact_fields.py` | OK (2026-10-22) |
| DRAFT-2 | **First-touch reply** (JD-first track): "Thanks for reaching out on <date> [about the <role> role]." then only the still-open clarifiers — JD, end client, W2 or 1099-direct (not set up for C2C), remote / on-site place, rate or salary band. | test: `test_qualifying_reply.py` | OK |
| DRAFT-3 | **Screen-first track** when the recruiter wants a résumé or call before sharing a JD: offer the generic latest résumé and a call (§12). | test: `test_qualifying_reply.py` | OK |
| DRAFT-4 | **Reply on a live thread** reads the whole conversation: no first-touch "Thanks for reaching out", and never re-asks a question the recruiter already answered (remote, engagement type, rate, end client, stack). | check: `first_touch_on_followup`, `reasks_answered`; test: `test_qualifying_reply.py` | OK (2026-10-22; `draft_qualifying_reply(followup=True, thread_replies=…)`) |
| DRAFT-5 | Every short-form message (qualifying, follow-up, decline, résumé-send) ends with `qualifying_reply.SIGNATURE`: "Best, Shawn" + `shawn.becker@spexture.com`. (Decided 2026-10-22.) | tests: `test_decline.py`, `test_set_lead_status.py` | OK (2026-10-22) |
| DRAFT-6 | Location wording in every message: "100% remote, or within ~15 minutes of Lehi". (Decided 2026-10-22; `~/CLAUDE.md` §12 updated the same day.) | `qualifying_reply._draft_screen_first_body` | OK |
| DRAFT-7 | Every draft is editable on the page before sending, and the edited text is what gets copied and logged. | — | OK (`f02ec2a`) |
| DRAFT-8 | A C2C-only mention still triggers the "not set up for C2C" clarifier. | test: `test_qualifying_reply.py` | OK (2026-10-22; a recruiter *reply* saying C2C still counts as answered) |

### 11b. Logging what was sent

| ID | Rule | Enforced | Status |
|---|---|---|---|
| LOG-1 | After sending, one click (**Message sent** / **Reply sent**) logs the exact text as an outbound conversation with the channel actually used, and starts the wait clock. The **via** selector defaults to the route's channel and can be changed. | test: `test_set_lead_status.py` | OK |
| LOG-2 | Texts sent through SendText are logged by the helper itself; no extra click. | — | OK |
| LOG-3 | LinkedIn replies can't be seen by the pipeline (LinkedIn never emails back what Shawn sends), so the page always offers a way to record them; a LinkedIn data export importer can backfill later. | test: `test_record_unmatched_reply.py` | OK (importer: GAP) |
| LOG-4 | A sent message is logged exactly once, even when the scan later sees the Gmail Sent copy (dedupe the `status-sent:` row against the real sent message). | test: `test_duplicate_outbound.py`; check: `duplicate_outbound` | OK (2026-10-22; `store.is_same_sent_message`; 13 existing copies removed) |
| LOG-5 | The page's "done" acknowledgement is recorded only after the helper confirms, not at click time. | — (page logic, `App.tsx` `recordPending`) | OK — 2026-10-22: the click shows "Confirm in RecordReply…"; the ack is saved only once regenerated data no longer shows the message reply-due, and is dropped after 10 min |

## 12. One owner per decision

Today the same decision is computed in several places, and the copies have
drifted — the root cause of most repeat bugs. Target owners:

| Decision | Owner (single source) | Current extra copies to remove |
|---|---|---|
| Channel of a conversation | intake (ING-3), stored on `job_conversations` | `_channel_from_addresses` ×7 call sites, `_next_action_clarify`, UI `messageTarget`, `links.ts`, `card_audit` |
| Who "the recruiter" is | one store function (CON-6) | `_recruiter_contact`, renderer oldest-by-id, `_send_route_fields` newest-by-id, archived-row pick, draft-name precedence |
| Is an address usable / a relay | one module (CON-2) | `_is_system_email`, `_usable_email`, `_NO_REPLY_RE`, UI `OWN_EMAIL_RE`/`SYSTEM_EMAIL_RE`, `card_audit._BOARD_RE`, LinkedIn sender sets ×3 |
| Reply plan / send routes | backend (ROUTE-1) | UI `sendRoutes`, `messageTarget`, `mailtoUrl`, `linkedinNotice` ×2 |
| Thread-link extraction | `extract_linkedin_thread_url` (ROUTE-4) | `_thread_url_for_lead`, `card_audit._LI_THREAD_RE` |
| Thresholds (14/21/30/45 days, 70 %, 50/25 %) | `config/framework.yaml` | constants in renderer, workflow, CLIs |
| Stub-lead first-touch scan | one builder | renderer `_build_linkedin_reply_queue` + workflow Source C |
| Reply panel on the page | `ManageLeadStatus` | the older steps panel in `ContactPriorityQueue`; unused `ClarifyQueue.tsx` |
| Lead status list | `LEAD_STAGES` | hard-coded list in `ManageLeadStatus.tsx` |

## 13. Known violations (fix queue)

Highest impact first. Each fix ships with the test/check named in its rule.

1. ~~**ING-1 / ING-6 data cleanup**~~ — done 2026-10-22: 19 own-address contacts removed, 21 duplicate inbound copies deleted, 16 replies flipped to outbound, 74 misfiled forwards detached (not re-triaged; they were old alerts). The two "Spexture" leads were already skipped and are left as is.
2. ~~**EVAL-5**~~ — done 2026-10-22. Rescoring stored JDs under the new rules flips 14 skipped leads from pass to pursue (old misfire: English "go"); none were reopened.
3. **ROUTE-1 / §12** — page re-derives channel and recipients (source of today's LinkedIn/email confusion).
4. ~~**DRAFT-4 / DRAFT-1 / CON-1 / CON-2**~~ — done 2026-10-22; the card check is clean.
5. ~~**PKG-2**~~ — done 2026-10-22.
6. ~~**CARD-1 / CARD-8 / TURN-3**~~ — done 2026-10-22.
7. **LOG-5, MAT-5, NEXT-2, ROUTE-5 (page side), LIFE-5** — smaller fixes.

## 14. Open decisions for Shawn

None open. Decided 2026-10-22: SYS-5, EVAL-4, CARD-6, DRAFT-5, DRAFT-6, PKG-4.

## 15. Changing this file

- Add a rule before (or with) the code that implements it; give it the next
  free ID in its area. Never reuse a retired ID — mark it **RETIRED** instead.
- When a **BROKEN** rule is fixed, set it to **OK** and name the test/check.
- When Shawn decides a **DECIDE** item, write the decision into the rule and
  remove it from §14.
- Candidate-profile changes go to `~/CLAUDE.md`, not here.
- [`requirements.html`](requirements.html) is a read-only, searchable view of the rules,
  generated by `scripts/render_requirements.py`. Don't edit it. The
  pre-commit hook (`.githooks/pre-commit`; enable once per clone with
  `git config core.hooksPath .githooks`) regenerates it whenever this file is
  committed, and `test_requirements_html.py` fails if the two drift.
