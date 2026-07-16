# RefreshPending — regenerate `pending-actions.html` from the page

The static page cannot run Python itself. This helper registers:

```
refreshpending://run
refreshpending://run?no_rescore=1
```

and shells out to the existing script:

```bash
job-tracker/scripts/render_pending_actions.py
```

then re-opens `var/pending-actions.html`.

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
