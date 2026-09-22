"""What this machine has, and what to do about what it lacks.

Used three ways: `omaform doctor` prints it, the first-run setup shows it,
and the Omarchy bar widget reads `omaform doctor --json` to decide whether
to offer setup. Nothing here changes anything.
"""

from __future__ import annotations

import importlib
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Check:
    name: str
    ok: bool
    needed: bool          # False: Omaform works without it, one feature does not
    detail: str
    fix: str = ""


def _module(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:  # noqa: BLE001
        return False


def _gi(namespace: str, version: str) -> bool:
    try:
        import gi
        gi.require_version(namespace, version)
        importlib.import_module(f"gi.repository.{namespace}")
        return True
    except Exception:  # noqa: BLE001
        return False


def checks() -> list[Check]:
    from . import llm
    from .profile import Library, data_dir

    out = [
        Check("GTK 4 and libadwaita", _gi("Gtk", "4.0") and _gi("Adw", "1"), True,
              "the window", "pacman -S --needed gtk4 libadwaita python-gobject"),
        Check("Poppler", _gi("Poppler", "0.18") and bool(shutil.which("pdftoppm")), True,
              "reading and showing PDFs", "pacman -S --needed poppler poppler-glib"),
        Check("Python libraries", all(_module(m) for m in
                                      ("pikepdf", "cryptography", "argon2", "PIL", "cairo")),
              True, "PDF writing, the vault, images",
              "./install.sh (installs them into the project's own environment)"),
        Check("tesseract", bool(shutil.which("tesseract")), False,
              "reading scanned forms", "pacman -S --needed tesseract tesseract-data-eng"),
        Check("LibreOffice", bool(shutil.which("soffice") or shutil.which("libreoffice")),
              False, "showing Word documents and locking them as PDF",
              "pacman -S --needed libreoffice-fresh"),
        Check("Keyring", bool(shutil.which("secret-tool")), False,
              "remembering the vault passphrase", "pacman -S --needed libsecret"),
    ]
    agent = llm.agent_status()
    out.append(Check(
        "Omarchy agent", agent["usable"], False,
        (f"{agent['label']}, for confusing forms" if agent["usable"]
         else "for confusing forms (optional)"),
        agent["why"]))
    local = llm.pick_local_model()
    out.append(Check("Local model", bool(local), False,
                     f"Ollama: {local}" if local else "a private alternative to the agent",
                     "" if local else "install Ollama and pull a model, e.g. ollama pull omarchy"))
    desktop = Path.home() / ".local/share/applications/omaform.desktop"
    out.append(Check("Launcher entry", desktop.exists(), False,
                     "Omaform in the app launcher and Open With",
                     "./packaging/install-desktop.sh"))
    library = Library()
    out.append(Check("Identity", bool(library.slugs()), False,
                     f"{len(library.slugs())} stored" if library.slugs() else "none yet",
                     "open Omaform; setup walks you through it"))
    vault = data_dir() / "vault.enc"
    out.append(Check("Vault", vault.exists(), False,
                     "private data encrypted" if vault.exists() else "not created yet",
                     "created the first time you store private data"))
    return out


def report(as_json: bool = False) -> str:
    items = checks()
    if as_json:
        import json
        ready = all(c.ok for c in items if c.needed)
        try:
            from importlib.metadata import version
            installed = version("omaform")
        except Exception:  # noqa: BLE001
            installed = ""
        return json.dumps({"ready": ready, "version": installed,
                           "onboarded": bool(_onboarded()),
                           "checks": [asdict(c) for c in items]})
    lines = []
    for c in items:
        mark = "✓" if c.ok else ("✗" if c.needed else "·")
        lines.append(f"  {mark} {c.name:22} {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"      {c.fix}")
    missing = [c.name for c in items if c.needed and not c.ok]
    lines.append("")
    lines.append("ready to fill forms" if not missing
                 else f"missing: {', '.join(missing)}")
    return "\n".join(lines)


def _onboarded() -> bool:
    from .profile import Library
    library = Library()
    return bool(library.setting("onboarded")) or bool(library.slugs())
