#!/usr/bin/env bash
# Install every Pending Actions custom-URL helper into ~/Applications.
#
# Each tools/<name>/install.sh can still be run alone. This wrapper is the
# one-shot path, and tools/pending-actions-ui-server/install.sh calls it so
# the React UI never ships without its macOS URL handlers.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${ROOT}/.." && pwd)"

# URL-scheme helpers the Pending Actions UI / static HTML fire.
# Keep in sync with tools/*/install.sh (except pending-actions-ui-server and
# lan-port80-redirect, which are not URL handlers).
HANDLERS=(
  reveal-folder
  view-communications
  refresh-pending
  reply-sent
  triage-imap-now
  set-direct-recruiter-outreach
  dismiss-linkedin-reply
  mark-package-sent
  set-lead-status
  decline-lead
)

CONSOLE_SCRIPTS=(
  export-communications
  set-direct-recruiter-outreach
  dismiss-linkedin-reply
  mark-package-sent
  set-lead-status
  decline-lead
)

if ! command -v swiftc >/dev/null 2>&1; then
  echo "error: swiftc not found. Install Xcode Command Line Tools first (xcode-select --install)." >&2
  exit 1
fi

PYTHON="${REPO_ROOT}/.venv/bin/python"
PIP="${REPO_ROOT}/.venv/bin/pip"
if [[ ! -x "${PYTHON}" ]]; then
  echo "error: ${PYTHON} not found — create the job-tracker venv first." >&2
  exit 1
fi

need_editable=0
for name in "${CONSOLE_SCRIPTS[@]}"; do
  if [[ ! -x "${REPO_ROOT}/.venv/bin/${name}" ]]; then
    need_editable=1
    break
  fi
done
if [[ "${need_editable}" -eq 1 ]]; then
  echo "Installing job-tracker console scripts into .venv (pip install -e .)…"
  "${PIP}" install -e "${REPO_ROOT}" -q
fi

# Batch install: lsregister is enough; do not cold-launch nine apps.
export JOB_TRACKER_HELPER_SKIP_OPEN=1

failed=0
for name in "${HANDLERS[@]}"; do
  installer="${ROOT}/${name}/install.sh"
  if [[ ! -x "${installer}" ]]; then
    echo "error: missing ${installer}" >&2
    failed=1
    continue
  fi
  echo "=== ${name} ==="
  if ! "${installer}"; then
    echo "error: ${name} install failed" >&2
    failed=1
  fi
done

if [[ "${failed}" -ne 0 ]]; then
  echo "Pending Actions URL handlers: one or more installs failed." >&2
  exit 1
fi

echo ""
echo "Installed ${#HANDLERS[@]} Pending Actions URL handlers into ${HOME}/Applications"
echo "Re-run this script after moving the checkout; paths are baked into each .app."
echo "Individual reinstall: ${ROOT}/<name>/install.sh"
