# Mailbox coverage matrix

**Phase 2 deliverable** — every recruiting-relevant address mapped to how the
hourly pipeline sees it. Update this when adding a forward, account, or cycle
step.

Ref: [`comms-migration/routing-inventory.md`](../comms-migration/routing-inventory.md),
[`recruiting-automation/run_cycle.sh`](../recruiting-automation/run_cycle.sh).

---

## Hourly cycle order

| Step | Repo | Command | What it reads |
|------|------|---------|---------------|
| 1 | comms-migration | `run_classifier.py --account personal_hub` | `scbboston@gmail.com` |
| 2 | comms-migration | `run_classifier.py --account recruiting_funnel` (+ spam sweep) | `shawnbecker.recruiting@gmail.com` |
| 3 | job-tracker | `triage_recruiter_inbox.py` | recruiting funnel Gmail (`Category/recruiter_job`) |
| 4 | job-tracker | `scan_communications.py` | LinkedIn replies (`Category/social`) + Sent folder |
| 5 | job-tracker | `triage_imap_inbox.py --imap-prefix SPEXTURE` | `shawn.becker@spexture.com` (Hostinger IMAP) |
| 6 | job-tracker | `process_awaiting_llm_review.py` | DB only (stuck high-gate leads) |
| 7 | job-tracker | `resync_labels.py` | Gmail JobTracker/* vs DB verdicts |
| 8 | job-tracker | `render_pending_actions.py` | DB → static UI JSON |

Fast tick (every 3 min): `comms_fast_cycle.py` — lightweight inbox check, shares DB lock with hourly steps.

---

## Address → pipeline coverage

| Address / surface | Lands where | Cycle step | Gap / notes |
|-------------------|-------------|------------|-------------|
| `shawnbecker.recruiting@gmail.com` | Recruiting funnel (native) | classify → triage | Primary path |
| `shawn.becker@spexture.com` | Hostinger → IMAP | triage_imap_inbox | Gmail API never sees this |
| `scb_boston@yahoo.com` | Forward → recruiting funnel | classify → triage | Verified 2026-07-04 |
| `sbecker@alum.mit.edu` | Forward → recruiting funnel | classify → triage | Verified 2026-07-04; check alumni backlog once |
| `shawn.becker@spexture.com` (Hostinger copy) | Hostinger mailbox | triage_imap | keep-a-copy backup on forwarder |
| `scbboston@gmail.com` | Personal hub (no auto-forward to funnel) | classify personal_hub only | Job mail here needs `triage_recruiter_inbox --account personal_hub` or manual forward |
| LinkedIn InMail replies | `Category/social` on funnel | scan_communications | Not triage_recruiter_inbox |
| Sent-folder replies | Gmail Sent | scan_communications `--include-sent` | Tier-1 match only |
| Archives / old SKIP rejections | Gmail All Mail | **Phase 2:** `scan_rejection_backlog.py` | Not live triage; run weekly or after HALT gap |
| VoIP / Nextiva SMS | Nextiva (not Gmail) | **Not wired** | Track B comms runbook |

---

## Phase 2 ops commands

```bash
# Rejection backlog (dry-run default)
cd job-tracker && python scripts/scan_rejection_backlog.py

# Apply after review
python scripts/scan_rejection_backlog.py --apply --yes

# Label↔DB drift audit (Phase 3)
python scripts/audit_label_drift.py
python scripts/resync_labels.py --dry-run
```

---

## Healthy target

Matrix shows **0 unwatched active sources** for addresses you still publish to
recruiters. Anything in "Gap / notes" with no cycle step gets a manual playbook
entry in [`PRIMER.md`](../job-tracker/PRIMER.md) § Backfill.
