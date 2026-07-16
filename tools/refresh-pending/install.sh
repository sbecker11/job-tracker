#!/usr/bin/env bash
# Build RefreshPending.app and install it to ~/Applications so
# refreshpending://run invokes scripts/render_pending_actions.py.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${ROOT}/../.." && pwd)"
BUILD_DIR="${ROOT}/build"
APP_NAME="RefreshPending.app"
DEST_DIR="${HOME}/Applications"
DEST_APP="${DEST_DIR}/${APP_NAME}"

PYTHON="${REPO_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

SCRIPT="${REPO_ROOT}/scripts/render_pending_actions.py"
HTML="${REPO_ROOT}/var/pending-actions.html"

mkdir -p "${BUILD_DIR}/${APP_NAME}/Contents/MacOS"
mkdir -p "${BUILD_DIR}/${APP_NAME}/Contents/Resources"

swiftc -O -framework AppKit -o "${BUILD_DIR}/${APP_NAME}/Contents/MacOS/RefreshPending" "${ROOT}/main.swift"
cp "${ROOT}/Info.plist" "${BUILD_DIR}/${APP_NAME}/Contents/Info.plist"

# Paths baked in at install time so the helper always hits this checkout.
python3 - <<PY
import json
from pathlib import Path
cfg = {
    "repoRoot": "${REPO_ROOT}",
    "pythonPath": "${PYTHON}",
    "scriptPath": "${SCRIPT}",
    "htmlPath": "${HTML}",
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
echo "URL scheme: refreshpending://run"
echo "  (optional) refreshpending://run?no_rescore=1"
echo ""
echo "Runs: ${PYTHON} ${SCRIPT}"
echo "Then opens: ${HTML}"
