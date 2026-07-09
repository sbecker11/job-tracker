# Extensibility: triaging categories beyond `recruiter_job`

**Status:** design doc — documents a refactor target, not yet implemented.
Nothing here changes behavior until the refactor in §3 is actually done.

## 1. Why this matters

Today `scripts/triage_recruiter_inbox.py` is a single, hardwired pipeline:

```
Category/recruiter_job mail
  -> classify (job_tracker.email.classifier)
  -> extract roles (pipeline/extract.py)
  -> resolve JD, LLM-score, maybe generate a résumé/cover letter (pipeline/triage.py)
  -> upsert_lead / advance_status (pipeline/store.py)
  -> relabel JobTracker/PURSUE|SKIP|NEEDS_REVIEW + archive (email/gmail_writer.py)
  -> record_message_processed (dedup / no re-billing)
```

`comms-migration`'s taxonomy (`rules/actions.yaml`) already has a dozen other
categories — `security_alert`, `billing`, `financial_admin`, `investing`,
etc. — any of which *could* eventually want its own "read it, decide
something, act on it, remember you did" flow the same shape as the one
above. Right now that would mean copy-pasting
`triage_recruiter_inbox.py` and hand-editing every step. This doc defines the
seam to cut along instead.

## 2. The generic shape

Every category-specific triage flow, including today's recruiter one, is the
same five steps with different bodies:

1. **Select** — a Gmail query scoped to one `Category/<name>` label, minus
   whatever this repo's own outcome labels already exclude.
2. **Decide** — turn one `EmailMessage` into a structured outcome: an outcome
   string/label, optional structured records to persist (a `JobLead` today;
   could be an `Incident`, an `Invoice`, whatever the category needs), and a
   human-readable reason.
3. **Act on Gmail** — apply an outcome-specific label and (usually) archive.
4. **Persist** — write whatever structured records the decision produced.
5. **Remember** — record that this message was processed, so a re-run never
   double-bills or double-labels it.

Steps 1, 3, and 5 are already category-agnostic in
`triage_recruiter_inbox.py` — they only reference `Category/recruiter_job`,
`JobTracker/*`, and `processed_messages` via configuration, not hardcoded
logic. Only step 2 ("Decide") and the specific shape of "whatever structured
records" in step 4 are recruiter-job-specific today.

## 3. Proposed refactor (not yet done)

Introduce a small protocol that any future category implements:

```python
# job_tracker/pipeline/category_handler.py (proposed)

from typing import Protocol

class CategoryHandler(Protocol):
    category: str          # e.g. "recruiter_job" — matches Category/<name>
    label_prefix: str       # e.g. "JobTracker" — outcome labels live under this
    outcomes: tuple[str, ...]  # e.g. ("PURSUE", "SKIP", "NEEDS_REVIEW")

    def decide(self, message: EmailMessage) -> "HandlerResult":
        """Classify + extract + decide. No Gmail or DB side effects here —
        mirrors today's pipeline/triage.py, which only decides."""
        ...

    def persist(self, conn, result: "HandlerResult") -> list[str]:
        """Write whatever structured records this category produces (leads,
        incidents, invoices, ...). Returns their keys for processed_messages
        bookkeeping."""
        ...
```

`scripts/triage_recruiter_inbox.py`'s current `main()` becomes a generic
`triage_inbox(handler: CategoryHandler, ...)` that any category-specific
script (or a single `--category recruiter_job|security_alert|...` CLI) can
call. `pipeline/triage.py`'s existing logic becomes the `RecruiterJobHandler`
implementation of this protocol — a rename/regroup, not a rewrite.

## 4. What this buys later (examples, none built yet)

- `security_alert` handler: decide whether an alert needs action (e.g. an
  unrecognized sign-in) vs. is routine (e.g. an expected 2FA code), and if
  action is needed, leave it labeled/un-archived instead of relabeling —
  comms-migration's existing `human_in_loop: true` on that category already
  says a human must look, so this handler's whole job might just be adding a
  triage note, not resolving anything automatically.
- `billing` handler: extract amount/vendor/due-date into a lightweight
  `Bill` record for a future "what auto-renews this month" report — the same
  shape as `JobLead`, different columns.

None of these are being built now — the point of this doc is only that
adding one later means implementing one class against `CategoryHandler`, not
another 200-line copy of `triage_recruiter_inbox.py`.

## 5. Non-goals

- This is not a plugin system with dynamic loading/config files — one new
  Python class per category, registered explicitly, is enough for a
  single-user tool.
- Not doing the refactor in §3 speculatively before a second real category
  needs it. Tracked as a follow-up once (if) `security_alert` or `billing`
  triage actually gets built — see `JOB_CRM_VISION.md` for what's being built
  now instead (the Job entity model).
