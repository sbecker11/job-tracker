#!/usr/bin/env bash
# Find-mail API for Pending Actions "Find JD email" (127.0.0.1:3175).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${ROOT}/../.." && pwd)"
PYTHON="${REPO_ROOT}/.venv/bin/python"
API_SCRIPT="${REPO_ROOT}/scripts/find_mail_api.py"
HOST="${PENDING_ACTIONS_UI_HOST:-127.0.0.1}"
PORT="${FIND_MAIL_API_PORT:-3175}"

if [[ ! -x "${PYTHON}" ]]; then
  echo "pending-actions-ui-server: python missing at ${PYTHON}" >&2
  exit 1
fi
if [[ ! -f "${API_SCRIPT}" ]]; then
  echo "pending-actions-ui-server: API script missing at ${API_SCRIPT}" >&2
  exit 1
fi

cd "${REPO_ROOT}"
exec "${PYTHON}" "${API_SCRIPT}" --host "${HOST}" --port "${PORT}"
