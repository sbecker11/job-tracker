#!/usr/bin/env bash
# Install/reload LaunchAgent so pending-actions-ui starts at login/reboot.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${ROOT}/../.." && pwd)"
LABEL="com.sbecker11.job-tracker.pending-actions-ui"
API_LABEL="com.sbecker11.job-tracker.find-mail-api"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
API_PLIST_PATH="${HOME}/Library/LaunchAgents/${API_LABEL}.plist"
RUN_SH="${ROOT}/run.sh"
API_RUN="${ROOT}/run-api.sh"
LOG_DIR="${REPO_ROOT}/pending-actions-ui/logs"

mkdir -p "${LOG_DIR}"
chmod +x "${RUN_SH}" "${API_RUN}"

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

# LAN access: PENDING_ACTIONS_UI_HOST=0.0.0.0 lets other machines on the home
# network open the page; PENDING_ACTIONS_UI_ALLOWED_HOSTS lists the hostnames
# they use (e.g. mini1.local). Unset values are carried over from the existing
# plist so a plain re-install keeps them.
plist_env() {
  [[ -f "${PLIST_PATH}" ]] || return 0
  plutil -extract "EnvironmentVariables.$1" raw -o - "${PLIST_PATH}" 2>/dev/null || true
}
UI_HOST="${PENDING_ACTIONS_UI_HOST:-$(plist_env PENDING_ACTIONS_UI_HOST)}"
UI_HOST="${UI_HOST:-127.0.0.1}"
UI_ALLOWED_HOSTS="${PENDING_ACTIONS_UI_ALLOWED_HOSTS:-$(plist_env PENDING_ACTIONS_UI_ALLOWED_HOSTS)}"

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
    <key>PENDING_ACTIONS_UI_HOST</key>
    <string>${UI_HOST}</string>
    <key>PENDING_ACTIONS_UI_ALLOWED_HOSTS</key>
    <string>${UI_ALLOWED_HOSTS}</string>
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

cat >"${API_PLIST_PATH}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${API_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>${API_RUN}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${REPO_ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/find-mail-api.launchd.out.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/find-mail-api.launchd.err.log</string>
</dict>
</plist>
EOF

# bootout returns before the old job is gone; bootstrapping too soon fails
# with "5: Input/output error", so retry for a few seconds.
reload_agent() {
  local label="$1" plist="$2" try
  launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
  for try in 1 2 3 4 5 6 7 8 9 10; do
    launchctl bootstrap "gui/$(id -u)" "${plist}" 2>/dev/null && return 0
    sleep 1
  done
  launchctl bootstrap "gui/$(id -u)" "${plist}"
}
reload_agent "${LABEL}" "${PLIST_PATH}"
reload_agent "${API_LABEL}" "${API_PLIST_PATH}"

echo "Installed: ${PLIST_PATH}"
echo "           ${API_PLIST_PATH}"
echo "Label:     ${LABEL}"
echo "API label: ${API_LABEL}"
echo "Node:      ${NODE_BIN}"
echo "UI:        http://127.0.0.1:3174/"
if [[ "${UI_HOST}" != "127.0.0.1" ]]; then
  echo "LAN:       listening on ${UI_HOST}:3174 (allowed hosts: ${UI_ALLOWED_HOSTS:-IP addresses only})"
  echo "           e.g. http://$(scutil --get LocalHostName 2>/dev/null || hostname -s).local:3174/"
fi
echo "Find-mail: http://127.0.0.1:3175/api/find-mail?q="
echo "Logs:      ${LOG_DIR}/launchd.{out,err}.log"
echo "           ${LOG_DIR}/find-mail-api.launchd.{out,err}.log"
echo ""
echo "Starts at login/reboot (RunAtLoad) and restarts if it dies (KeepAlive)."
echo "Stop with: ${ROOT}/stop.sh"

if [[ "${JOB_TRACKER_SKIP_HANDLER_INSTALL:-}" == "1" ]]; then
  echo "Skipping Pending Actions URL handlers (JOB_TRACKER_SKIP_HANDLER_INSTALL=1)."
else
  echo ""
  echo "Installing Pending Actions URL handlers…"
  "${ROOT}/../install-pending-action-handlers.sh"
fi
