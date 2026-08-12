#!/usr/bin/env bash
# Install/reload LaunchAgent so pending-actions-ui starts at login/reboot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${ROOT}/../.." && pwd)"
LABEL="com.sbecker11.job-tracker.pending-actions-ui"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
RUN_SH="${ROOT}/run.sh"
LOG_DIR="${REPO_ROOT}/pending-actions-ui/logs"

mkdir -p "${LOG_DIR}"
chmod +x "${RUN_SH}"

# Resolve node at install time and bake into the plist environment.
NODE_BIN=""
export NVM_DIR="${HOME}/.nvm"
if [[ -s "${NVM_DIR}/nvm.sh" ]]; then
  # shellcheck disable=SC1090
  . "${NVM_DIR}/nvm.sh"
fi
NODE_BIN="$(command -v node || true)"
if [[ -z "${NODE_BIN}" || ! -x "${NODE_BIN}" ]]; then
  echo "install: could not find node on PATH. Install node (nvm/Homebrew) first." >&2
  exit 1
fi

cat >"${PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${RUN_SH}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}/pending-actions-ui</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>$(dirname "${NODE_BIN}"):/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PENDING_ACTIONS_UI_NODE</key>
    <string>${NODE_BIN}</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/launchd.err.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/${LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" "${PLIST_PATH}"

echo "Installed: ${PLIST_PATH}"
echo "Label:     ${LABEL}"
echo "Node:      ${NODE_BIN}"
echo "UI:        http://127.0.0.1:3174/"
echo "Logs:      ${LOG_DIR}/launchd.{out,err}.log"
echo ""
echo "Starts at login/reboot (RunAtLoad) and restarts if it dies (KeepAlive)."
echo "Stop with: ${ROOT}/stop.sh"
