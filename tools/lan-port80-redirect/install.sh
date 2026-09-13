#!/bin/zsh
# One-time-per-boot: redirect this Mac's LAN port 80 -> the loopback-bound
# Pending Actions Vite server on 127.0.0.1:3174, so another machine on the
# same network (e.g. mini1) can browse http://<this-mac>.local/ with no
# port number and no SSH tunnel.
#
# Requires sudo — pf (the packet filter) is root-only on macOS. Run this
# script directly (it calls sudo itself for just the two pf commands);
# you'll get one password prompt.
#
# NOT persistent across reboot by itself (pf state loaded via `pfctl -a`
# resets on restart) — re-run this after a reboot, or promote it to a
# LaunchDaemon (root-owned, /Library/LaunchDaemons) with RunAtLoad if you
# want it to survive reboots unattended.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
RULE_FILE="${ROOT}/pf-rule.conf"
ANCHOR="com.apple/pending-actions-redirect"

if [[ ! -f "${RULE_FILE}" ]]; then
  echo "install-port80-redirect: missing ${RULE_FILE}" >&2
  exit 1
fi

echo "Enabling pf (no-op if already enabled)..."
sudo pfctl -e 2>&1 | grep -v "^pfctl: pf already enabled$" || true

echo "Loading redirect rule into anchor ${ANCHOR}..."
sudo pfctl -a "${ANCHOR}" -f "${RULE_FILE}"

echo
echo "Done. LAN port 80 -> 127.0.0.1:3174 (interface en0)."
echo "From another machine on the same LAN, browse: http://$(scutil --get LocalHostName 2>/dev/null || hostname).local/"
echo "Remove this rule with: ${ROOT}/uninstall.sh"
