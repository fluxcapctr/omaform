"""Drive the real window through the one flow that matters most.

Everything else in the suite tests logic with the widgets left out. This test
found a bug none of them could: a Gtk.PasswordEntry method that does not exist,
raised inside a dialog callback, which GLib logs and swallows. The window kept
running, the vault dialog never appeared, and a typed Social Security number
went nowhere. Only real widgets on a real display catch that class of failure,
so this needs a compositor and is skipped without one.
"""

import os

import pytest

pytest.importorskip("gi")

if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
    pytest.skip("needs a display to present real dialogs", allow_module_level=True)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib  # noqa: E402


def test_adding_a_missing_ssn_creates_the_vault_and_fills_the_form(tmp_path, monkeypatch, w9):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from omaform.profile import Library
    from omaform.ui import app as ui

    Library(tmp_path).create("Test Person")

    presented: list[Adw.AlertDialog] = []
    original = Adw.AlertDialog.choose

    def spy(self, parent, cancellable, callback, *rest):
        presented.append(self)
        return original(self, parent, cancellable, callback, *rest)

    monkeypatch.setattr(Adw.AlertDialog, "choose", spy)

    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, w9)
            self.win.present()
            GLib.timeout_add(1500, self.press_add)

        def press_add(self):
            outcome["vault_before"] = self.win.vault.exists
            self.win._add_missing(None, "ssn")
            GLib.timeout_add(500, self.type_value)
            return False

        def type_value(self):
            dialog = presented[-1]
            outcome["value_dialog"] = dialog.get_heading()
            dialog.entry.set_text("123-45-6789")
            dialog.emit("response", "save")
            GLib.timeout_add(500, self.type_passphrase)
            return False

        def type_passphrase(self):
            outcome["dialogs"] = [d.get_heading() for d in presented]
            if len(presented) < 2:
                self.quit()
                return False
            box = presented[-1].get_extra_child()
            child = box.get_first_child()
            while child is not None:
                child.set_text("a test passphrase")
                child = child.get_next_sibling()
            presented[-1].emit("response", "create")
            GLib.timeout_add(3000, self.inspect)
            return False

        def inspect(self):
            outcome["vault_after"] = self.win.vault.exists
            outcome["keys"] = self.win.vault.key_names() if self.win.vault.exists else []
            plan = self.win.plan
            outcome["filled_ssn"] = [e.value for e in plan.filled
                                     if e.match and e.match.key == "ssn"] if plan else None
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test")
    GLib.timeout_add(15000, harness.quit)
    harness.run([])

    assert outcome.get("vault_before") is False
    assert outcome.get("value_dialog") == "Set Social Security number"
    assert outcome.get("dialogs") == ["Set Social Security number",
                                      "Choose a vault passphrase"], \
        "the vault-creation dialog must follow the value dialog"
    assert outcome.get("vault_after") is True, "the vault was never written"
    assert outcome.get("keys") == ["test-person/ssn"]
    assert outcome.get("filled_ssn") == ["123", "45", "6789"], \
        "a secret added because the form asked for it must land in that form"


def test_switching_the_editor_to_another_identity_sticks_and_it_can_be_deleted(
        tmp_path, monkeypatch):
    """Choosing an identity in the editor rebuilds the page. The rebuilt
    dropdown used to take its selection from the Fill tab, so the choice
    reverted at once and only the default could ever be edited or deleted."""
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gio
    from omaform.profile import Library
    from omaform.ui import app as ui

    library = Library(tmp_path)
    library.create("Eric Stevens")
    library.create("Eric Stevens Design", kind="business")

    presented: list[Adw.AlertDialog] = []
    original = Adw.AlertDialog.choose

    def spy(self, parent, cancellable, callback, *rest):
        presented.append(self)
        return original(self, parent, cancellable, callback, *rest)

    monkeypatch.setattr(Adw.AlertDialog, "choose", spy)
    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self)
            self.win.present()
            self.win.stack.set_visible_child_name("identities")
            GLib.timeout_add(1200, self.pick_second)

        def pick_second(self):
            self.win.editing_row.set_selected(1)
            GLib.timeout_add(600, self.check_and_delete)
            return False

        def check_and_delete(self):
            editing = self.win._editing()
            outcome["editing_after_switch"] = editing.slug if editing else None
            outcome["dropdown_after_switch"] = self.win.editing_row.get_selected()
            self.win._delete_identity()
            GLib.timeout_add(500, self.confirm)
            return False

        def confirm(self):
            presented[-1].emit("response", "delete")
            GLib.timeout_add(800, self.finish)
            return False

        def finish(self):
            outcome["remaining"] = Library(tmp_path).slugs()
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test2",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(15000, harness.quit)
    harness.run([])

    assert outcome.get("editing_after_switch") == "eric-stevens-design"
    assert outcome.get("dropdown_after_switch") == 1, "the dropdown reverted"
    assert outcome.get("remaining") == ["eric-stevens"]


def test_changing_who_is_filling_sticks_and_replans(tmp_path, monkeypatch, w9):
    """The fill page rebuilds itself after every plan. The rebuilt "Filling as"
    dropdown used to read its selection back from the new, unset row, so the
    choice reverted to the first identity the moment it was made."""
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gio
    from omaform.profile import Library
    from omaform.ui import app as ui

    library = Library(tmp_path)
    library.create("Alex Rivera", {"full_name": "Alex Rivera"})
    library.create("Rivera Design Co", {"full_name": "Rivera Design Co"},
                   kind="business")
    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, w9)
            self.win.present()
            GLib.timeout_add(2500, self.pick_second)

        def pick_second(self):
            outcome["before"] = self.win.current_identity().slug
            self.win.identity_row.set_selected(1)
            GLib.timeout_add(2500, self.inspect)
            return False

        def inspect(self):
            outcome["after"] = self.win.current_identity().slug
            outcome["dropdown"] = self.win.identity_row.get_selected()
            names = [e.value for e in self.win.plan.filled
                     if e.match and e.match.key == "full_name"] if self.win.plan else []
            outcome["planned_name"] = names
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test3",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(15000, harness.quit)
    harness.run([])

    assert outcome.get("before") == "alex-rivera"
    assert outcome.get("after") == "rivera-design-co"
    assert outcome.get("dropdown") == 1, "the dropdown reverted"
    assert outcome.get("planned_name") == ["Rivera Design Co"], \
        "the plan must be rebuilt for the identity just chosen"


def test_a_black_out_drawn_on_the_page_is_burned_in_on_save(tmp_path, monkeypatch, w9):
    """Drag a box in Black out mode, save, and the saved copy has neither the
    words under it nor the fields on that page. The source is untouched."""
    import shutil
    import subprocess

    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gio
    from omaform.profile import Library
    from omaform.ui import app as ui

    source = tmp_path / "fw9.pdf"
    shutil.copy(w9, source)
    before = source.read_bytes()
    Library(tmp_path).create("Alex Rivera", {"full_name": "Alex Rivera"})
    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, str(source))
            self.win.present()
            GLib.timeout_add(2500, self.draw_box)

        def draw_box(self):
            preview = self.win.preview
            preview.set_blackout(True)
            # The "Social security number" caption on the W-9, in PDF points.
            preview._redact_by_hand = None  # not used; the drag goes through the callback
            preview.on_redact(1, (240.0, 405.0, 330.0, 435.0))
            outcome["listed"] = len(self.win.redactions)
            GLib.timeout_add(1500, self.save)
            return False

        def save(self):
            self.win.do_fill()
            GLib.timeout_add(1500, self.inspect)
            return False

        def inspect(self):
            out = source.with_name("fw9-filled.pdf")
            outcome["exists"] = out.exists()
            if out.exists():
                outcome["text"] = subprocess.run(
                    ["pdftotext", str(out), "-"], capture_output=True, text=True).stdout
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test4",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(20000, harness.quit)
    harness.run([])

    assert outcome.get("listed") == 1
    assert outcome.get("exists"), "nothing was saved"
    page_one = outcome["text"].split("\f")[0]
    assert "Social security number" not in page_one
    assert "Alex Rivera" not in page_one, \
        "page 1 became a picture, so the typed name is pixels now"
    assert "Specific Instructions" in outcome["text"], "other pages keep their text"
    assert source.read_bytes() == before, "the source must never change"


def test_tab_and_typing_fill_fields_on_the_page(tmp_path, monkeypatch, w9):
    """Tab reaches the first field, letters land in it, Tab commits and moves
    on, Shift+Tab comes back, Space ticks a box."""
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gdk, Gio
    from omaform.model import BlankKind
    from omaform.profile import Library
    from omaform.ui import app as ui

    Library(tmp_path).create("Alex Rivera", {"full_name": "Alex Rivera"})
    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, w9)
            self.win.present()
            GLib.timeout_add(2500, self.type_away)

        def type_away(self):
            preview = self.win.preview
            key = lambda kv, state=0: preview._key(None, kv, 0, Gdk.ModifierType(state))  # noqa: E731
            key(Gdk.KEY_Tab)
            first = preview.active
            outcome["first_is_name"] = preview._value_of(preview._active_blank()) == "Alex Rivera"
            # Retype the name.
            for _ in range(len("Alex Rivera")):
                key(Gdk.KEY_BackSpace)
            for ch in "Sam":
                key(Gdk.unicode_to_keyval(ord(ch)))
            outcome["live"] = self.win._entry_for(first).value
            key(Gdk.KEY_Tab)
            second = preview.active
            outcome["moved"] = second != first
            outcome["committed"] = self.win.overrides.get(first)
            for ch in "Rivera Design":
                key(Gdk.unicode_to_keyval(ord(ch)))
            key(Gdk.KEY_ISO_Left_Tab)
            outcome["back"] = preview.active == first
            outcome["second_kept"] = self.win.overrides.get(second)
            # On to the first checkbox and tick it.
            box = next(b for b in preview._typeable() if b.kind is BlankKind.CHECKBOX)
            preview.enter_field(box)
            was = self.win._entry_for(box.id).filled
            key(Gdk.KEY_space)
            outcome["toggled"] = self.win._entry_for(box.id).filled != was
            key(Gdk.KEY_Escape)
            outcome["left"] = preview.active
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test5",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(15000, harness.quit)
    harness.run([])

    assert outcome.get("first_is_name"), "Tab should land on the name box first"
    assert outcome.get("live") == "Sam"
    assert outcome.get("moved") and outcome.get("committed") == "Sam"
    assert outcome.get("back") and outcome.get("second_kept") == "Rivera Design"
    assert outcome.get("toggled"), "Space should flip the box"
    assert outcome.get("left") is None


def test_a_word_form_opens_fills_and_saves(tmp_path, monkeypatch, word_form):
    """No page of its own to draw: the list is the interface, the page view
    arrives from LibreOffice when it is ready, and saving writes a .docx."""
    import zipfile

    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gio
    from omaform.profile import Library
    from omaform.ui import app as ui

    Library(tmp_path).create("Alex Rivera", {"full_name": "Alex Rivera", "city": "Springfield"})
    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, word_form)
            self.win.present()
            GLib.timeout_add(2500, self.check)

        def check(self):
            outcome["filled"] = {e.blank.label.best(): e.value for e in self.win.plan.filled}
            outcome["blackout_hidden"] = not any(
                "Black out" in (getattr(w, "get_title", lambda: "")() or "")
                for w in _walk(self.win.fill_body))
            self.win.do_fill()
            GLib.timeout_add(1500, self.inspect)
            return False

        def inspect(self):
            out = tmp_path / "application-filled.docx"
            outcome["saved"] = out.exists()
            if out.exists():
                with zipfile.ZipFile(out) as z:
                    outcome["xml"] = z.read("word/document.xml").decode()
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test6",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(20000, harness.quit)
    harness.run([])

    assert outcome.get("filled", {}).get("Name") == "Alex Rivera"
    assert outcome.get("filled", {}).get("City") == "Springfield"
    assert outcome.get("blackout_hidden"), "black-outs are a PDF thing"
    assert outcome.get("saved"), "no .docx was written"
    assert "Alex Rivera" in outcome["xml"] and "Springfield" in outcome["xml"]


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def test_editing_keeps_the_page_where_it_was_and_black_outs_come_off(tmp_path, monkeypatch, i9):
    """Clicking on the page gives it focus, and a focused child used to scroll
    its viewport back to page 1. A black-out's corner × removes it."""
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gio
    from omaform.profile import Library
    from omaform.ui import app as ui

    Library(tmp_path).create("Alex Rivera", {"full_name": "Alex Rivera"})
    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, i9)
            self.win.present()
            GLib.timeout_add(2500, self.scroll)

        def scroll(self):
            self.win.preview.go_to_page(3)
            GLib.timeout_add(600, self.edit)
            return False

        def edit(self):
            preview = self.win.preview
            adj = preview.scroller.get_vadjustment()
            outcome["before"] = adj.get_value()
            preview.area.grab_focus()
            entry = next(e for e in self.win.plan.entries
                         if e.blank.kind.value == "text" and not e.blank.readonly)
            self.win._set_override(entry.blank.id, "hello")
            self.win.preview.on_redact(3, (100.0, 100.0, 300.0, 200.0))
            GLib.timeout_add(800, self.close_box)
            return False

        def close_box(self):
            preview = self.win.preview
            outcome["after"] = preview.scroller.get_vadjustment().get_value()
            placed = preview._layout[2]
            s = 16 / placed.scale
            # The middle of the × in screen space.
            px = 8 + (300.0 - s / 2) * placed.scale
            py = placed.top + (placed.pdf_h - (200.0 - s / 2)) * placed.scale
            outcome["handle"] = preview._close_handle_at(px, py) is not None
            preview.on_unredact(preview._close_handle_at(px, py))
            outcome["left"] = len(self.win.redactions)
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test7",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(15000, harness.quit)
    harness.run([])

    assert outcome.get("before", 0) > 500
    assert abs(outcome.get("after", 0) - outcome["before"]) < 1, "the page jumped"
    assert outcome.get("handle"), "the × should be where it is drawn"
    assert outcome.get("left") == 0


def test_pages_grid_moves_a_page_by_drop(tmp_path, monkeypatch, w9):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gio
    from omaform.ui import app as ui

    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, w9)
            self.win.present()
            GLib.timeout_add(2000, self.move)

        def move(self):
            tab = self.win.pages_tab
            before = [s.index for s in tab.sequence]
            tab.move(4, 0)
            outcome["order"] = [s.index for s in tab.sequence]
            outcome["before"] = before
            outcome["cards"] = len(list(_walk(tab.grid)))
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test8",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(15000, harness.quit)
    harness.run([])

    assert outcome["before"] == [0, 1, 2, 3, 4, 5]
    assert outcome["order"] == [4, 0, 1, 2, 3, 5]
    assert outcome["cards"] > 6


def test_save_as_writes_where_asked(tmp_path, monkeypatch, w9):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from pathlib import Path
    from gi.repository import Gio
    from omaform.profile import Library
    from omaform.ui import app as ui

    Library(tmp_path).create("Alex Rivera", {"full_name": "Alex Rivera"})
    target = tmp_path / "elsewhere" / "my w9.pdf"
    target.parent.mkdir()
    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, w9)
            self.win.present()
            GLib.timeout_add(2500, self.save)

        def save(self):
            self.win.do_fill(target)
            outcome["saved"] = self.win.last_saved
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test9",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(15000, harness.quit)
    harness.run([])

    assert target.exists() and outcome.get("saved") == target
    assert not Path(w9).with_name("fw9-filled.pdf").exists()


def test_a_placed_date_can_be_dragged_and_is_saved_where_it_was_dropped(tmp_path, monkeypatch, w9):
    """Right-click to place a date, drag it, save: the ink is where it was dropped."""
    import subprocess

    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gio
    from omaform.model import BlankKind
    from omaform.profile import Library
    from omaform.ui import app as ui

    Library(tmp_path).create("Alex Rivera", {"full_name": "Alex Rivera"})
    outcome: dict = {}
    out = tmp_path / "moved.pdf"

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, w9)
            self.win.present()
            GLib.timeout_add(2500, self.place)

        def place(self):
            self.win._place_by_hand(1, BlankKind.DATE, 100.0, 150.0)
            GLib.timeout_add(600, self.drag)
            return False

        def drag(self):
            preview = self.win.preview
            placed = preview._layout[0]
            blank = next(b for b in self.win.document.blanks if "~hand-date" in b.id)
            # Screen point in the middle of the date box.
            px = 8 + (blank.rect[0] + 20) * placed.scale
            py = placed.top + (placed.pdf_h - blank.rect[1] - 6) * placed.scale
            outcome["grabbable"] = preview._draggable_at(px, py) is blank
            preview._drag_begin(None, px, py)
            preview._drag_update(None, 200 * placed.scale, -300 * placed.scale)
            preview._drag_end(None, 200 * placed.scale, -300 * placed.scale)
            outcome["offset"] = blank.offset
            self.win.do_fill(out)
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test10",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(15000, harness.quit)
    harness.run([])

    assert outcome.get("grabbable"), "the placed date should be pick-up-able"
    assert outcome.get("offset") == (200.0, 300.0)
    bbox = subprocess.run(["pdftotext", "-f", "1", "-l", "1", "-bbox", str(out), "-"],
                          capture_output=True, text=True).stdout
    import re
    from datetime import date
    today = date.today().strftime("%m/%d/%Y")
    m = re.search(r'xMin="([\d.]+)" yMin="([\d.]+)"[^>]*>' + re.escape(today), bbox)
    assert m, "the date was not written"
    x, y_top = float(m.group(1)), float(m.group(2))
    assert 295 < x < 310, x                  # 100 + 200, plus the text inset
    assert 792 - 470 < y_top < 792 - 440, y_top   # moved up by 300 from y=150


def test_round2_window_findings(tmp_path, monkeypatch, i9):
    """Astra round 2, findings 1, 9 and 12, on the real window:
    1. saving a black-out over a source named like the old staging file,
       to the name the staging was derived from, keeps the source;
    9. opening another file while a model reading runs frees the button;
    12. a signature drawn onto a form's plain text field can be dragged."""
    import shutil
    import time as clock

    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from gi.repository import Gio
    from omaform.profile import Library
    from omaform.ui import app as ui

    source = tmp_path / "original.redacting.pdf"
    shutil.copy(i9, source)
    before = source.read_bytes()
    Library(tmp_path).create("Alex Rivera", {"full_name": "Alex Rivera"})
    outcome: dict = {}

    class Harness(Adw.Application):
        def do_activate(self):
            self.win = ui.Window(self, str(source))
            self.win.present()
            GLib.timeout_add(2500, self.save)

        def save(self):
            self.win.preview.on_redact(1, (100.0, 100.0, 200.0, 150.0))
            self.win.do_fill(tmp_path / "original.pdf")
            outcome["source_kept"] = source.exists() and source.read_bytes() == before
            outcome["saved"] = (tmp_path / "original.pdf").exists()
            # 12: put a signature image on the employee signature text field.
            entry = next(e for e in self.win.plan.entries
                         if "Signature of Employee" in e.blank.id)
            outcome["kind"] = entry.blank.kind.value
            import io
            import cairo
            surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 120, 40)
            buf = io.BytesIO()
            surface.write_to_png(buf)
            self.win._set_override(entry.blank.id, buf.getvalue())
            preview = self.win.preview
            placed = preview._layout[entry.blank.page - 1]
            r = preview._hit_rect(entry.blank)
            px = 8 + (r[0] + r[2]) / 2 * placed.scale
            py = placed.top + (placed.pdf_h - (r[1] + r[3]) / 2) * placed.scale
            outcome["draggable"] = preview._draggable_at(px, py) is entry.blank
            # 9: a model reading in flight, then another file.
            self.win.model_busy = ("a model", clock.monotonic())
            self.win.load(i9)
            outcome["busy_after_load"] = self.win.model_busy
            self.quit()
            return False

    harness = Harness(application_id="co.ericstevens.omaform.test11",
                      flags=Gio.ApplicationFlags.NON_UNIQUE)
    GLib.timeout_add(20000, harness.quit)
    harness.run([])

    assert outcome.get("source_kept"), "the source was overwritten or removed"
    assert outcome.get("saved")
    assert outcome.get("kind") == "text"
    assert outcome.get("draggable"), "a signature on a text field should move"
    assert outcome.get("busy_after_load") is None


def test_first_run_setup_creates_the_identity_and_remembers_the_choice(tmp_path, monkeypatch):
    """A fresh install opens setup by itself; the pages store the name, and
    finishing records the agent choice and that setup has been done."""
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    monkeypatch.delenv("OMAFORM_NO_ONBOARDING", raising=False)
    from omaform.profile import Library
    from omaform.ui import app as ui
    from omaform.ui import onboarding

    monkeypatch.setattr(onboarding.Onboarding, "_set_watching", staticmethod(lambda on: None))
    monkeypatch.setattr(onboarding.Onboarding, "_watching", staticmethod(lambda: False))
    monkeypatch.setattr(onboarding.Onboarding, "_default_pdf", staticmethod(lambda: True))
    outcome: dict = {}

    class Harness(ui.Application):
        def do_activate(self):
            self.win = ui.Window(self)
            self.win.present()
            GLib.timeout_add(1500, self.walk)

        def walk(self):
            dialog = self.win.get_visible_dialog()
            outcome["shown"] = dialog is not None
            if dialog is None:
                self.quit()
                return False
            you = dialog._you()
            dialog.nav.push(you)
            dialog.entries["full_name"].set_text("Alex Rivera")
            dialog.entries["city"].set_text("Springfield")
            # "Continue" on the You page, then through to the end.
            for button in _walk(you):
                if isinstance(button, Gtk.Button) and button.get_label() == "Continue":
                    button.emit("clicked")
                    break
            agent = dialog._agent()
            dialog.backend_row.set_selected(1)          # a local model
            for button in _walk(agent):
                if isinstance(button, Gtk.Button) and button.get_label() == "Continue":
                    button.emit("clicked")
                    break
            forms = dialog.nav.get_visible_page()
            for button in _walk(forms):
                if isinstance(button, Gtk.Button) and button.get_label() == "Finish":
                    button.emit("clicked")
                    break
            self.quit()
            return False

    from gi.repository import Gtk  # noqa: F401
    globals()["Gtk"] = Gtk
    Harness().run([])

    library = Library(tmp_path)
    assert outcome.get("shown"), "setup should open on a fresh install"
    [identity] = library.all()
    assert identity.label == "Alex Rivera" and identity.values["city"] == "Springfield"
    assert library.setting("llm_backend") == "ollama"
    assert library.setting("onboarded") is True
