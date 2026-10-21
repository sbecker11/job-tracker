#!/usr/bin/env bash
# Build SendText.app and install it to ~/Applications so
# sendtext://send?key=...&to=...&message=...&status=... (from Manage lead's
# "Text <name>" button) confirms, sends via Messages, and logs the text.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${ROOT}/../.." && pwd)"
BUILD_DIR="${ROOT}/build"
APP_NAME="SendText.app"
DEST_DIR="${HOME}/Applications"
DEST_APP="${DEST_DIR}/${APP_NAME}"

BIN="${REPO_ROOT}/.venv/bin/set-lead-status"
DECLINE_BIN="${REPO_ROOT}/.venv/bin/decline-lead"
for b in "${BIN}" "${DECLINE_BIN}"; do
  if [[ ! -x "${b}" ]]; then
    echo "error: ${b} not found — run 'pip install -e .' in ${REPO_ROOT} first." >&2
    exit 1
  fi
done

DB="${REPO_ROOT}/var/leads.db"

mkdir -p "${BUILD_DIR}/${APP_NAME}/Contents/MacOS"
mkdir -p "${BUILD_DIR}/${APP_NAME}/Contents/Resources"

swiftc -O -framework AppKit -o "${BUILD_DIR}/${APP_NAME}/Contents/MacOS/SendText" "${ROOT}/main.swift"
cp "${ROOT}/Info.plist" "${BUILD_DIR}/${APP_NAME}/Contents/Info.plist"

python3 - <<PY
import json
from pathlib import Path
cfg = {
    "statusBinPath": "${BIN}",
    "declineBinPath": "${DECLINE_BIN}",
    "dbPath": "${DB}",
}
Path("${BUILD_DIR}/${APP_NAME}/Contents/Resources/config.json").write_text(
    json.dumps(cfg, indent=2) + "\n"
)
PY

if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "${BUILD_DIR}/${APP_NAME}" 2>/dev/null || true
fi

mkdir -p "${DEST_DIR}"
rm -rf "${DEST_APP}"
cp -R "${BUILD_DIR}/${APP_NAME}" "${DEST_APP}"

/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "${DEST_APP}"

if [[ "${JOB_TRACKER_HELPER_SKIP_OPEN:-}" != "1" ]]; then
  open "${DEST_APP}"
  sleep 0.5
fi

echo "Installed: ${DEST_APP}"
echo "URL scheme: sendtext://send?key=<normalized_key>&to=<phone>&name=<who>&message=<text>&status=<stage>"
echo "  (decline: &decline_reason=<code>&decline_note=<text> instead of &status)"
echo ""
echo "First Send asks once to let SendText control Messages (System Settings → Privacy & Security → Automation)."
