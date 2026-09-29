#!/usr/bin/env bash
# Unload the pending-actions-ui LaunchAgent (does not delete the plist).
set -euo pipefail

LABEL="com.sbecker11.job-tracker.pending-actions-ui"
API_LABEL="com.sbecker11.job-tracker.find-mail-api"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"
API_PLIST_PATH="${HOME}/Library/LaunchAgents/${API_LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
  || launchctl unload "${PLIST_PATH}" 2>/dev/null \
  || true
launchctl bootout "gui/$(id -u)/${API_LABEL}" 2>/dev/null \
  || launchctl unload "${API_PLIST_PATH}" 2>/dev/null \
  || true

if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -tiTCP:3175 -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    # shellcheck disable=SC2086
    kill ${pids} 2>/dev/null || true
  fi
fi

echo "Stopped: ${LABEL} and ${API_LABEL}"
echo "Plists left at ${PLIST_PATH} and ${API_PLIST_PATH} (re-run install.sh to start again)."
