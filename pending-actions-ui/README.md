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

Open the Vite URL (usually http://localhost:5173).  
Legacy `var/pending-actions.html` is still written but left alone as the old UI.
