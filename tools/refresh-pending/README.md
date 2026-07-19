# RefreshPending — regenerate `pending-actions.html` from the page

The static page cannot run Python itself. This helper registers:

```
refreshpending://run
refreshpending://run?no_rescore=1
refreshpending://run?no_open=1
```

and shells out to the existing script:

```bash
job-tracker/scripts/render_pending_actions.py
```

then re-opens `var/pending-actions.html` — unless `no_open=1` was passed, in
which case it just runs the script and returns, without touching the
browser. The page's own "Regenerate page" button (see
`render_pending_actions.py`'s `regen-btn` JS) uses `no_open=1` and reloads
its own tab in place once the process is done, instead of this helper
opening a second window — that mismatch (one click, two windows) was the
original bug. The terminal smoke test below still wants the open-a-browser
behavior, so `no_open` defaults to off.

## Install (once)

```bash
cd job-tracker/tools/refresh-pending
./install.sh
```

Installs `~/Applications/RefreshPending.app`. Paths to this checkout’s
`.venv` Python and script are baked into the app at install time.

## Smoke test

```bash
open 'refreshpending://run?no_rescore=1'
```
