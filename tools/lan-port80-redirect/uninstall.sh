#!/bin/zsh
# Removes the port-80 -> 3174 redirect installed by install.sh. Requires
# sudo (same reason as install.sh).
set -euo pipefail

ANCHOR="com.apple/pending-actions-redirect"

echo "Flushing rules from anchor ${ANCHOR}..."
sudo pfctl -a "${ANCHOR}" -F all
echo "Done. LAN port 80 no longer redirects to 3174."
