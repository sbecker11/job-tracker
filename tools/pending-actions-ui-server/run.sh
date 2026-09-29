#!/usr/bin/env bash
# Start the pending-actions Vite UI (used by launchd KeepAlive agent).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${ROOT}/../.." && pwd)"
UI_DIR="${REPO_ROOT}/pending-actions-ui"
VITE="${UI_DIR}/node_modules/.bin/vite"
HOST="${PENDING_ACTIONS_UI_HOST:-127.0.0.1}"
PORT="${PENDING_ACTIONS_UI_PORT:-3174}"
API_PORT="${FIND_MAIL_API_PORT:-3175}"
API_RUN="${ROOT}/run-api.sh"

# Prefer a real node binary (launchd has a minimal PATH; nvm isn't sourced).
# Override with PENDING_ACTIONS_UI_NODE if needed after an nvm upgrade.
NODE_BIN="${PENDING_ACTIONS_UI_NODE:-}"
if [[ -z "${NODE_BIN}" || ! -x "${NODE_BIN}" ]]; then
  for candidate in \
    "${HOME}/.nvm/versions/node/v24.19.0/bin/node" \
    "$(command -v node 2>/dev/null || true)"
  do
    if [[ -n "${candidate}" && -x "${candidate}" ]]; then
      NODE_BIN="${candidate}"
      break
    fi
  done
fi

if [[ -z "${NODE_BIN}" || ! -x "${NODE_BIN}" ]]; then
  echo "pending-actions-ui-server: node not found. Set PENDING_ACTIONS_UI_NODE." >&2
  exit 1
fi

if [[ ! -x "${VITE}" ]]; then
  echo "pending-actions-ui-server: vite missing at ${VITE} (run npm install in pending-actions-ui)." >&2
  exit 1
fi

# Free the port before bind (stale vite/node from a prior KeepAlive or manual run).
free_port() {
  local p="$1"
  if ! command -v lsof >/dev/null 2>&1; then
    return 0
  fi
  local pids
  pids="$(lsof -tiTCP:"${p}" -sTCP:LISTEN 2>/dev/null || true)"
  if [[ -n "${pids}" ]]; then
    echo "pending-actions-ui-server: freeing port ${p} (PIDs: ${pids})" >&2
    # shellcheck disable=SC2086
    kill ${pids} 2>/dev/null || true
    sleep 0.4
    pids="$(lsof -tiTCP:"${p}" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      # shellcheck disable=SC2086
      kill -9 ${pids} 2>/dev/null || true
      sleep 0.2
    fi
  fi
}

free_port "${PORT}"
# Do not kill :3175 here — a sibling KeepAlive agent owns the find-mail API.
# If that agent is not loaded (manual vite), start a fallback listener.
mkdir -p "${REPO_ROOT}/pending-actions-ui/logs"
if command -v lsof >/dev/null 2>&1; then
  if ! lsof -tiTCP:"${API_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    if [[ -x "${API_RUN}" ]]; then
      nohup "${API_RUN}" >>"${REPO_ROOT}/pending-actions-ui/logs/find-mail-api.log" 2>&1 &
      disown || true
      echo "pending-actions-ui-server: find-mail API fallback on ${HOST}:${API_PORT} (pid $!)" >&2
    fi
  fi
fi

export PATH="$(dirname "${NODE_BIN}"):${PATH}"
cd "${UI_DIR}"
exec "${NODE_BIN}" "${VITE}" --host "${HOST}" --port "${PORT}" --strictPort
