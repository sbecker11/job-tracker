# Pending actions UI (React)

Stage-based workflow for recruiter leads:

**Clarify → Send résumé → Wait / schedule → Decide / apply**

Data comes from `scripts/render_pending_actions.py` (not a live API yet).

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
Regenerate writes:

- `var/pending-actions.json`
- `pending-actions-ui/public/pending-actions.json`
- `var/pending-actions.html` (legacy static page)

## Notes

- Channel (LinkedIn / Email) is a badge only; sort priority is contact attempts then age.
- Dismiss / folder helpers still use the existing `dlr://` and `revealfolder://` Mac helpers.
- Legacy HTML remains until this UI is the daily driver.
