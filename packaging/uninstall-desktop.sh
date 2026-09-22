#!/bin/bash
set -euo pipefail
rm -f "$HOME/.local/bin/omaform" "$HOME/.local/bin/omaform-desktop" \
      "$HOME/.local/bin/omaform-askpass" \
      "$HOME/.local/share/applications/omaform.desktop"
for size in 16 24 32 48 64 128 256 512; do
  rm -f "$HOME/.local/share/icons/hicolor/${size}x${size}/apps/omaform.png"
done
update-desktop-database "$HOME/.local/share/applications" >/dev/null 2>&1 || true
command -v omarchy-refresh-applications >/dev/null 2>&1 && omarchy-refresh-applications || true
echo "removed."
