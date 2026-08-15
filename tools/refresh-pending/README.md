# RefreshPending — regenerate pending-actions data from the page

The React / static pending-actions UI cannot run Python itself. This helper
registers:

```
refreshpending://run
refreshpending://run?no_rescore=1
refreshpending://run?no_open=1
refreshpending://run?open=1
```

and shells out to:

```bash
job-tracker/scripts/render_pending_actions.py
```

**It does not open a browser by default.** Opening
`var/pending-actions.html` via `NSWorkspace` was spawning a second Chrome
tab while the already-open React UI (`http://127.0.0.1:3174/`) stayed put.
The in-page **Regenerate** button polls/reloads that same tab after the
script finishes.

Pass `?open=1` only when you explicitly want the helper to open the static
HTML file (terminal smoke test).

`?no_open=1` is still accepted (and is now the default behavior).

## Install (once)

```bash
cd job-tracker/tools/refresh-pending
./install.sh
```

Installs `~/Applications/RefreshPending.app`. Paths to this checkout’s
`.venv` Python and script are baked into the app at install time.

## Smoke test (no new browser tab)

```bash
open 'refreshpending://run?no_rescore=1'
```

Then refresh / wait for the React tab to pick up the new `generatedAt`.

## Smoke test (open static HTML on purpose)

```bash
open 'refreshpending://run?open=1&no_rescore=1'
```
