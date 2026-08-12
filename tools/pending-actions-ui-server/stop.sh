#!/usr/bin/env bash
# Unload the pending-actions-ui LaunchAgent (does not delete the plist).
set -euo pipefail

LABEL="com.sbecker11.job-tracker.pending-actions-ui"
PLIST_PATH="${HOME}/Library/LaunchAgents/${LABEL}.plist"

launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null \
  || launchctl unload "${PLIST_PATH}" 2>/dev/null \
  || true

echo "Stopped: ${LABEL}"
echo "Plist left at ${PLIST_PATH} (re-run install.sh to start again)."
