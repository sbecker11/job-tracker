#!/usr/bin/env bash
# Build DismissLinkedInReply.app and install it to ~/Applications so
# dlr://dismiss?kind=... invokes the dismiss-linkedin-reply console script.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${ROOT}/../.." && pwd)"
BUILD_DIR="${ROOT}/build"
APP_NAME="DismissLinkedInReply.app"
DEST_DIR="${HOME}/Applications"
DEST_APP="${DEST_DIR}/${APP_NAME}"

BIN="${REPO_ROOT}/.venv/bin/dismiss-linkedin-reply"
if [[ ! -x "${BIN}" ]]; then
  echo "error: ${BIN} not found — run 'pip install -e .' in ${REPO_ROOT} first." >&2
  exit 1
fi

DB="${REPO_ROOT}/var/leads.db"

mkdir -p "${BUILD_DIR}/${APP_NAME}/Contents/MacOS"
mkdir -p "${BUILD_DIR}/${APP_NAME}/Contents/Resources"

swiftc -O -framework AppKit -o "${BUILD_DIR}/${APP_NAME}/Contents/MacOS/DismissLinkedInReply" "${ROOT}/main.swift"
cp "${ROOT}/Info.plist" "${BUILD_DIR}/${APP_NAME}/Contents/Info.plist"

python3 - <<PY
import json
from pathlib import Path
cfg = {
    "binPath": "${BIN}",
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

open "${DEST_APP}"
sleep 0.5

echo "Installed: ${DEST_APP}"
echo "URL scheme: dlr://dismiss?kind=lead|unmatched&key=...&message_id=..."
echo ""
echo "Runs: ${BIN} --db ${DB} --kind <kind> [--key ...] [--message-id ...]"
