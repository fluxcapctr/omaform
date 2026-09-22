#!/bin/bash
# Development install: puts Omaform in the application launcher straight from a
# checkout, with no packaging. The AUR package in Phase 10 does this properly,
# system wide. Undo with uninstall-desktop.sh.

set -euo pipefail
repo=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
bindir="$HOME/.local/bin"
apps="$HOME/.local/share/applications"
icons="$HOME/.local/share/icons/hicolor"

mkdir -p "$bindir" "$apps"

venv="${OMAFORM_VENV:-$repo/.venv}"
if [[ -x $venv/bin/omaform ]]; then
  # A virtualenv hardcodes absolute paths in its shebangs, so it does not
  # survive the checkout being moved or renamed. Catch that here rather than
  # let it surface later as "bad interpreter" from the launcher, where there is
  # no terminal to read the error on.
  if ! "$venv/bin/omaform" --help >/dev/null 2>&1; then
    echo "the .venv in this checkout does not run, which usually means the" >&2
    echo "directory was moved. Recreate it:" >&2
    echo "  rm -rf .venv && python -m venv --system-site-packages .venv" >&2
    echo "  .venv/bin/pip install -e ." >&2
    exit 1
  fi
  ln -sf "$venv/bin/omaform" "$bindir/omaform"
  ln -sf "$venv/bin/omaform-ui" "$bindir/omaform-ui"
elif ! command -v omaform >/dev/null; then
  echo "omaform is not installed and there is no .venv; run pip install -e . first" >&2
  exit 1
fi
ln -sf "$repo/packaging/omaform-desktop" "$bindir/omaform-desktop"
ln -sf "$repo/packaging/omaform-askpass" "$bindir/omaform-askpass"

for size in 16 24 32 48 64 128 256 512; do
  install -Dm644 "$repo/assets/omaform-$size.png" \
    "$icons/${size}x${size}/apps/omaform.png"
done
install -Dm644 "$repo/packaging/omaform.desktop" "$apps/omaform.desktop"

install -Dm644 "$repo/packaging/omaform-watch.service" \
  "$HOME/.config/systemd/user/omaform-watch.service"
systemctl --user daemon-reload >/dev/null 2>&1 || true

update-desktop-database "$apps" >/dev/null 2>&1 || true
gtk-update-icon-cache -f -t "$icons" >/dev/null 2>&1 || true
command -v omarchy-refresh-applications >/dev/null 2>&1 && omarchy-refresh-applications || true

case ":$PATH:" in
  *":$bindir:"*) ;;
  *) echo "note: $bindir is not on your PATH, so the omaform command will not be found" ;;
esac
echo "installed. Look for Omaform in the launcher, or right-click a PDF and Open With."
echo "To be offered a Fill button whenever a form lands in ~/Downloads:"
echo "  systemctl --user enable --now omaform-watch"
echo "To make Omaform what opens every PDF (it is only offered as an option by default):"
echo "  xdg-mime default omaform.desktop application/pdf"
