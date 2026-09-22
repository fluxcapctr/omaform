"""Notice a form arriving in Downloads and offer to fill it.

Most forms arrive as an email attachment saved from the browser. This watches
a folder, and when a new PDF settles there and turns out to have blanks in it,
puts up one desktop notification with a Fill button. Nothing opens on its own,
nothing is read beyond the file's blanks, and a PDF with nothing to fill, like
an invoice or a paper, is left alone without a word.

Runs as `omaform watch`, usually under the user service in packaging/, and
needs no display of its own: the notification daemon draws the notice and
the window is launched only when the button is pressed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

MIN_BLANKS = 3
SETTLE_SECONDS = 1.5


def is_form(path: str) -> int:
    """How many blanks the file has, or 0 if it is not a form (or not readable)."""
    from .adapters import for_path

    try:
        adapter = for_path(path)
        if adapter is None:
            return 0
        doc = adapter.discover(path)
    except Exception:  # noqa: BLE001, a half-written or odd file is just not a form
        return 0
    return len(doc.blanks) if len(doc.blanks) >= MIN_BLANKS else 0


def notify(path: str, blanks: int) -> str:
    """Put up the notice; returns the action chosen, "open" or "" (dismissed)."""
    if shutil.which("notify-send") is None:
        return ""
    done = subprocess.run(
        ["notify-send", "-a", "Omaform", "-i", "omaform", "-A", "open=Fill it",
         "--wait", f"A form arrived: {Path(path).name}",
         f"{blanks} blanks to fill"],
        capture_output=True, text=True)
    return done.stdout.strip()


def open_in_window(path: str) -> None:
    exe = shutil.which("omaform-ui") or str(Path(os.sys.executable).with_name("omaform-ui"))
    subprocess.Popen([exe, path], start_new_session=True)


class Watcher:
    """One folder, one file monitor, one notice per settled PDF."""

    def __init__(self, folder: str, *, on_form=None, notifier=notify, opener=open_in_window):
        self.folder = Path(folder).expanduser()
        self.on_form = on_form
        self.notifier = notifier
        self.opener = opener
        self.pending: dict[str, int] = {}
        self.seen: set[str] = set()

    def consider(self, path: str) -> None:
        """A PDF changed. Wait for it to stop changing, then look at it."""
        from gi.repository import GLib

        if not path.lower().endswith((".pdf", ".docx")) or path in self.seen \
                or path in self.pending:
            return
        # Browsers write in chunks and rename at the end; poll until the size
        # holds still for a moment before reading it. One poll per file: the
        # burst of change events while it is written all lands here.
        self.pending[path] = -1

        def check() -> bool:
            try:
                size = os.path.getsize(path)
            except OSError:
                self.pending.pop(path, None)
                return False
            if size != self.pending.get(path) or size == 0:
                self.pending[path] = size
                return True  # keep polling
            self.pending.pop(path, None)
            self.seen.add(path)
            threading.Thread(target=self._announce, args=(path,), daemon=True).start()
            return False

        GLib.timeout_add(int(SETTLE_SECONDS * 1000), check)

    def _announce(self, path: str) -> None:
        blanks = is_form(path)
        if not blanks:
            return
        if self.on_form is not None:
            self.on_form(path, blanks)
        if self.notifier(path, blanks) == "open":
            self.opener(path)

    def run(self) -> None:
        """Watch until the process is stopped."""
        from gi.repository import Gio, GLib

        self.folder.mkdir(parents=True, exist_ok=True)
        monitor = Gio.File.new_for_path(str(self.folder)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)

        def changed(_m, file, other, event) -> None:
            if event in (Gio.FileMonitorEvent.CREATED, Gio.FileMonitorEvent.CHANGED,
                         Gio.FileMonitorEvent.CHANGES_DONE_HINT,
                         Gio.FileMonitorEvent.MOVED_IN, Gio.FileMonitorEvent.RENAMED):
                target = other if event == Gio.FileMonitorEvent.RENAMED and other else file
                self.consider(target.get_path())

        monitor.connect("changed", changed)
        self._monitor = monitor
        loop = GLib.MainLoop()
        try:
            loop.run()
        except KeyboardInterrupt:
            pass
