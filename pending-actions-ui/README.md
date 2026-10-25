# Pending actions UI (React)

**Contact priority** (sticky at top) is the single source of truth for outbound
manual work. Every lead that needs a reply, résumé send, or wait sits in one
ranked list (attempts → age). Generated drafts expand on that same row.

Filter chips (All contact / Clarify / Send résumé / Wait / Decide·apply) narrow
the list — they do not create a second priority model. Decide/apply is package
funnel work, shown when that chip is selected.

## Setup

```bash
cd pending-actions-ui
npm install
```

## Regenerate data + run

From the job-tracker repo root:

```bash
.venv/bin/python scripts/render_pending_actions.py --no-rescore
cd pending-actions-ui && npm run dev
```

Open http://127.0.0.1:3174/ (LaunchAgent) or the Vite URL (usually http://localhost:5173).
Legacy `var/pending-actions.html` is still written but left alone as the old UI.

## Find JD email

The **Find JD email** box searches every lead (including skipped/closed),
every stored field (JD text, notes, LLM review, contacts, conversations,
documents, meetings, offers, processed mail), résumé-package files under
`~/Desktop/Resumes/2026/`, and Spexture IMAP across all folders. It needs
`scripts/find_mail_api.py` on `127.0.0.1:3175`, which
`tools/pending-actions-ui-server/run.sh` starts next to Vite.

```bash
python scripts/find_mail_api.py   # if the UI is already up without the API
```

## Paste a LinkedIn thread

Copy the thread in LinkedIn (the message, including any `linkedin.com/messaging/thread/…` link), then from the job-tracker repo:

```bash
python scripts/paste_linkedin_lead.py
```

That reads the clipboard and posts it to `POST /api/linkedin-lead` on the API above. The lead shows on Pending Actions within a minute. A pipe works too: `pbpaste | python scripts/paste_linkedin_lead.py`.
