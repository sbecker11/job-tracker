#!/usr/bin/env bash
# Build RevealFolder.app and install it to ~/Applications so the
# revealfolder:// URL scheme is registered with Launch Services.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="${ROOT}/build"
APP_NAME="RevealFolder.app"
DEST_DIR="${HOME}/Applications"
DEST_APP="${DEST_DIR}/${APP_NAME}"

mkdir -p "${BUILD_DIR}/${APP_NAME}/Contents/MacOS"
mkdir -p "${BUILD_DIR}/${APP_NAME}/Contents/Resources"

swiftc -O -framework AppKit -o "${BUILD_DIR}/${APP_NAME}/Contents/MacOS/RevealFolder" "${ROOT}/main.swift"
cp "${ROOT}/Info.plist" "${BUILD_DIR}/${APP_NAME}/Contents/Info.plist"

# Ad-hoc sign so Gatekeeper is happier on first launch (unsigned local builds
# still usually work; this avoids some "damaged" false positives).
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "${BUILD_DIR}/${APP_NAME}" 2>/dev/null || true
fi

mkdir -p "${DEST_DIR}"
rm -rf "${DEST_APP}"
cp -R "${BUILD_DIR}/${APP_NAME}" "${DEST_APP}"

# Register the URL scheme with Launch Services.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "${DEST_APP}"

# Cold-launch once so macOS binds revealfolder:// → this app.
open "${DEST_APP}"
sleep 0.5

echo "Installed: ${DEST_APP}"
echo "URL scheme: revealfolder://reveal?path=/absolute/path/to/folder"
echo ""
echo "Smoke test:"
echo "  open 'revealfolder://reveal?path=${HOME}/Desktop'"
