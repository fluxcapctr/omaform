"""A form landing in a folder gets one notice; anything else gets none."""

import shutil

import pytest

pytest.importorskip("gi")
from gi.repository import GLib  # noqa: E402

from omaform import watch  # noqa: E402


def test_a_form_is_a_form_and_a_blank_page_is_not(w9, tmp_path):
    assert watch.is_form(w9) > 3
    import pikepdf
    empty = tmp_path / "empty.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(str(empty))
    assert watch.is_form(str(empty)) == 0
    assert watch.is_form(str(tmp_path / "missing.pdf")) == 0


def test_watcher_notices_a_settled_pdf_once(w9, tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "SETTLE_SECONDS", 0.2)
    seen: list = []
    opened: list = []
    watcher = watch.Watcher(str(tmp_path), on_form=lambda p, n: seen.append((p, n)),
                            notifier=lambda p, n: "open", opener=opened.append)
    loop = GLib.MainLoop()

    def drop() -> bool:
        shutil.copy(w9, tmp_path / "arrived.pdf")
        (tmp_path / "notes.txt").write_text("not a pdf")
        return False

    def again() -> bool:
        watcher.consider(str(tmp_path / "arrived.pdf"))  # a second change event
        return False

    def stop() -> bool:
        loop.quit()
        return False

    from gi.repository import Gio
    monitor = Gio.File.new_for_path(str(tmp_path)).monitor_directory(
        Gio.FileMonitorFlags.WATCH_MOVES, None)
    monitor.connect("changed", lambda _m, f, _o, _e: watcher.consider(f.get_path()))
    GLib.timeout_add(100, drop)
    GLib.timeout_add(600, again)
    GLib.timeout_add(3000, stop)
    loop.run()
    # The announce runs on a thread; give it a moment.
    import time
    for _ in range(50):
        if opened:
            break
        time.sleep(0.1)
    assert len(seen) == 1 and seen[0][0].endswith("arrived.pdf") and seen[0][1] > 3
    assert opened == [str(tmp_path / "arrived.pdf")]
