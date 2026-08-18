# TriageImap — process a shawn.becker@spexture.com message right now

The React pending-actions page cannot run Python itself. This helper registers:

```
triageimap://run
```

and shells out to:

```bash
job-tracker/scripts/triage_imap_now.py --wait-lock-seconds 90
```

That runs the **full** treatment for the Spexture IMAP mailbox — live JD
resolution, LLM extraction fallback, LLM scoring, and package generation on
a "pursue" verdict — then regenerates `pending-actions.json`. This is more
than the 3-minute `comms_fast_cycle.py` LaunchAgent tick does for this same
mailbox (that one runs `triage_imap_inbox.py --offline --no-generate`,
cheap and fast, just enough to park a stub lead); this is for the moment you
want the real verdict immediately — e.g. a recruiter just emailed you mid-call
and you want to see the score/package before you hang up, rather than waiting
for the next hourly `run_cycle.sh` tick to fill it in.

The page's **Check inbox now** button fires `triageimap://run` in a hidden
iframe and polls `pending-actions.json` until `generatedAt` changes — same
mechanism as **Regenerate** / **Reply sent** — then diffs the lead set
before/after to find whichever lead is newly present and scrolls straight to
it in whatever tab it landed in (Contact priority, Decide/apply, or Archived).

Shares `comms_fast_cycle.py`'s lock file, so this never runs at the same time
as that 3-minute tick — it waits up to 90s for the lock rather than failing
immediately, since clicking mid-call is exactly when a background tick might
also be running.

## Install (once)

```bash
cd job-tracker/tools/triage-imap-now
./install.sh
```

Installs `~/Applications/TriageImap.app`. Paths to this checkout's `.venv`
Python and script are baked into the app at install time.

## Smoke test

```bash
open 'triageimap://run'
```

Then refresh `http://127.0.0.1:3174/` (or wait for the in-page spinner).
