# ReplySent — kick a Sent scan after you BCC a LinkedIn reply

The React pending-actions page cannot run Python itself. This helper registers:

```
replysent://run
```

and shells out to:

```bash
job-tracker/scripts/comms_fast_cycle.py --no-open --wait-lock-seconds 90
```

That is the same fast tick as the 3-minute LaunchAgent: scan Sent (and light
inbox triage), then regenerate `pending-actions.json` so Clarify → Wait can
update as soon as your BCC self-copy hits recruiting Gmail.

The page’s **Reply sent** button fires `replysent://run` in a hidden iframe
and polls until `generatedAt` changes — it does **not** open a second browser
tab.

## Install (once)

```bash
cd job-tracker/tools/reply-sent
./install.sh
```

Installs `~/Applications/ReplySent.app`. Paths to this checkout’s `.venv`
Python and script are baked into the app at install time.

## Smoke test

```bash
open 'replysent://run'
```

Then refresh `http://127.0.0.1:3174/` (or wait for the in-page spinner).
