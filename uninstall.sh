#!/bin/bash
# Remove what install.sh added: the launcher entry, the icons, the commands in
# ~/.local/bin, the Downloads watcher unit, and the Python environment.
#
# Your identities, vault and remembered forms in ~/.local/share/omaform are
# kept. Pass --purge to delete those too; it asks first, because the vault
# cannot be recovered once it is gone.

set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)

systemctl --user disable --now omaform-watch >/dev/null 2>&1 || true
rm -f "$HOME/.config/systemd/user/omaform-watch.service"
systemctl --user daemon-reload >/dev/null 2>&1 || true
rm -f "$HOME/.local/bin/omaform-ui"
"$here/packaging/uninstall-desktop.sh"
rm -rf "${OMAFORM_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/omaform-venv}"

if [[ ${1:-} == "--purge" ]]; then
  data="${OMAFORM_HOME:-$HOME/.local/share/omaform}"
  read -r -p "Delete $data, including your vault and identities? Type yes: " answer
  if [[ $answer == "yes" ]]; then
    rm -rf "$data"
    echo "deleted $data"
  else
    echo "kept $data"
  fi
fi
echo "Omaform is uninstalled."
