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
| `run.sh` | launchd program — frees port 3174, then execs local `vite` on `127.0.0.1:3174` |
| `install.sh` | writes plist under `~/Library/LaunchAgents/`, bootstraps the agent |
| `stop.sh` | `launchctl bootout` (leaves plist in place) |
| `pending-actions-ui/vite.config.ts` | pins `host`/`port`/`strictPort` so the bookmark stays stable |

Plist behavior: `RunAtLoad=true` (login/reboot), `KeepAlive=true` (restart if Vite exits), `ThrottleInterval=10`. `install.sh` bakes the current `node` absolute path into `EnvironmentVariables` because launchd does not source nvm.

| | |
|---|---|
| URL | http://127.0.0.1:3174/ |
| Label | `com.sbecker11.job-tracker.pending-actions-ui` |
| Plist | `~/Library/LaunchAgents/com.sbecker11.job-tracker.pending-actions-ui.plist` |
| Logs | `pending-actions-ui/logs/launchd.{out,err}.log` |

After an `nvm` Node upgrade, re-run `install.sh` so the plist picks up the new `node` path.

Check status: `launchctl print "gui/$(id -u)/com.sbecker11.job-tracker.pending-actions-ui"`
