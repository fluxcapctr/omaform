#!/bin/bash
# Install Omaform for this user, from this folder.
#
# Makes a Python environment inside this folder (.venv), installs Omaform into
# it, and adds the launcher entry, the icon, the command-line tools in
# ~/.local/bin and the Downloads watcher unit (left off until you turn it on).
# Works from a git clone and from the Omarchy plugin folder alike.
#
# Nothing is installed system-wide and no root access is used. If a system
# library is missing, this stops and prints the pacman command that adds it.
# Undo with ./uninstall.sh.

set -euo pipefail
here=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
cd "$here"

missing=()
command -v python3 >/dev/null || missing+=(python)
python3 - <<'PY' 2>/dev/null || missing+=(gtk4 libadwaita python-gobject python-cairo poppler-glib)
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1"); gi.require_version("Poppler", "0.18")
from gi.repository import Gtk, Adw, Poppler
import cairo
PY
command -v pdftoppm >/dev/null || missing+=(poppler)
if (( ${#missing[@]} )); then
  echo "Omaform needs a few system packages first. Install them with pacman, as root:"
  echo "  pacman -S --needed ${missing[*]}"
  exit 1
fi

# The environment lives outside this folder. An Omarchy plugin folder may not
# contain symlinks, and a Python environment is full of them; and a copy rather
# than a live link keeps Python from writing cache files in here, which the
# shell would take as the plugin changing. Run this again after an update.
venv="${OMAFORM_VENV:-${XDG_DATA_HOME:-$HOME/.local/share}/omaform-venv}"
echo "setting up the Python environment in $venv"
if [[ ! -x $venv/bin/python ]] || ! "$venv/bin/python" -c "import gi" 2>/dev/null; then
  rm -rf "$venv"
  python3 -m venv --system-site-packages "$venv"
fi
"$venv/bin/python" -m pip install --quiet --upgrade pip
build=$(mktemp -d)
trap 'rm -rf "$build"' EXIT
# Built from a copy, so the build leaves nothing behind in this folder.
tar --exclude=.git --exclude=video --exclude=tests -cf - . | tar -xf - -C "$build"
"$venv/bin/python" -m pip install --quiet --force-reinstall --no-deps "$build"
"$venv/bin/python" -m pip install --quiet "$build"

OMAFORM_VENV="$venv" ./packaging/install-desktop.sh

echo
"$venv/bin/omaform" doctor || true
echo
echo "Omaform is installed. Open it from the launcher; the first run walks you"
echo "through your details, the vault, and connecting your Omarchy agent."
