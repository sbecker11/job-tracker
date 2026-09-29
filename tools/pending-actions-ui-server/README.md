# pending-actions-ui LaunchAgent

Keeps the React pending-actions Vite server running across login/reboot.

Also documented in:

- `AGENTS.md` (local-helpers one-liner)
- `README.md` → Setup → “Pending-actions React UI (LaunchAgent)”
- `PRIMER.md` → “React pending-actions UI (login/reboot LaunchAgent)”

## Install / stop

```bash
./tools/pending-actions-ui-server/install.sh   # start now + on reboot
./tools/pending-actions-ui-server/stop.sh      # unload until re-installed
```

Manual (no agent): `cd pending-actions-ui && npm run dev`

## Implementation

| Piece | Role |
|---|---|
| `run.sh` | launchd program — frees port 3174, then execs local `vite` on `127.0.0.1:3174`; starts find-mail API only if `:3175` is not already listening |
| `run-api.sh` | launchd program for Find JD email — `scripts/find_mail_api.py` on `127.0.0.1:3175` |
| `install.sh` | writes both plists under `~/Library/LaunchAgents/`, bootstraps the agents |
| `stop.sh` | `launchctl bootout` for UI + API (leaves plists in place) |
| `pending-actions-ui/vite.config.ts` | pins `host`/`port`/`strictPort` so the bookmark stays stable |

Plist behavior: `RunAtLoad=true` (login/reboot), `KeepAlive=true` (restart if Vite exits), `ThrottleInterval=10`. `install.sh` bakes the current `node` absolute path into `EnvironmentVariables` because launchd does not source nvm.

| | |
|---|---|
| URL | http://127.0.0.1:3174/ |
| Find-mail API | http://127.0.0.1:3175/api/find-mail?q= |
| Label | `com.sbecker11.job-tracker.pending-actions-ui` |
| API label | `com.sbecker11.job-tracker.find-mail-api` |
| Plist | `~/Library/LaunchAgents/com.sbecker11.job-tracker.pending-actions-ui.plist` |
| API plist | `~/Library/LaunchAgents/com.sbecker11.job-tracker.find-mail-api.plist` |
| Logs | `pending-actions-ui/logs/launchd.{out,err}.log` |
| API logs | `pending-actions-ui/logs/find-mail-api.launchd.{out,err}.log` |

After an `nvm` Node upgrade, re-run `install.sh` so the plist picks up the new `node` path.

Check status: `launchctl print "gui/$(id -u)/com.sbecker11.job-tracker.pending-actions-ui"`
