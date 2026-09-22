"""The Omaform window.

Small on purpose. Open a form, choose who is filling it in, fill it. The second
tab is where identities are edited, so that keeping your details current never
means going back to a terminal.

Once a form is open, the page itself is shown with the fill drawn over it, and
the list beside it is where a value is checked and adjusted. A right-click on
the page signs or dates at that point, for the lines the detector missed.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import llm  # noqa: E402
from .. import memory  # noqa: E402
from .. import plan as planning  # noqa: E402
from .. import redact  # noqa: E402
from .. import vault as vaulting  # noqa: E402
from ..adapters import for_path  # noqa: E402
from ..model import BlankKind  # noqa: E402
from ..profile import (BY_KEY, CHOICES, KINDS, SCHEMA, SENSITIVE_KEYS,  # noqa: E402
                       Identity, Library, Profile, data_dir)
from .. import signature as signing  # noqa: E402
from ..adapters.pdf import placed_by_hand  # noqa: E402
from .pages_tab import PagesTab  # noqa: E402
from .preview import PagePreview  # noqa: E402
from .signature_dialog import SignatureDialog  # noqa: E402
from .theme import Theme  # noqa: E402

APP_ID = "co.ericstevens.omaform"

# Which keys appear in which group of the identity editor, in this order.
EDITOR_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Name", ("full_name", "first_name", "middle_initial", "last_name",
              "business_name", "job_title")),
    ("Address", ("address1", "address2", "city", "state", "zip", "country")),
    ("Contact", ("phone", "email", "website")),
)


def vault_path() -> Path:
    return data_dir() / "vault.enc"


class PassphraseDialog(Adw.AlertDialog):
    """Asks for the vault passphrase, and nothing else."""

    def __init__(self, message: str) -> None:
        super().__init__(heading="Unlock your vault", body=message)
        self.entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True,
                                       margin_top=8)
        self.set_extra_child(self.entry)
        self.add_response("cancel", "Cancel")
        self.add_response("unlock", "Unlock")
        self.set_response_appearance("unlock", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("unlock")
        self.set_close_response("cancel")


class ValueDialog(Adw.AlertDialog):
    """Asks for one sensitive value, without echoing it."""

    def __init__(self, title: str) -> None:
        super().__init__(heading=f"Set {title}",
                         body="Stored encrypted. It is never shown again, only used.")
        self.entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True,
                                       margin_top=8)
        self.set_extra_child(self.entry)
        self.add_response("cancel", "Cancel")
        self.add_response("save", "Save")
        self.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        self.set_default_response("save")
        self.set_close_response("cancel")


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, path: str | None = None) -> None:
        super().__init__(application=app, title="Omaform",
                         default_width=720, default_height=680)
        self.add_css_class("omaform")

        self.library = Library()
        self.vault = vaulting.Vault(vault_path())
        self.document = None
        self.doc_path: str | None = None
        self.preview_path: str | None = None  # what the page view shows; a PDF
        self._preview_tmp: str | None = None   # a Word file's render, deleted on close
        self.connect("close-request", lambda *_: (self._drop_preview_dir(), False)[1])
        self.plan = None
        # Which identity the editor tab is showing. Kept here, not read back
        # from a widget: the page is rebuilt on every change, and a rebuilt
        # dropdown that takes its selection from somewhere else forgets what
        # was just picked.
        self.editing_slug: str | None = None
        # Which identity the fill page is filling as. Same reason: the page is
        # rebuilt after every plan, and a rebuilt dropdown starts at index 0.
        self.filling_slug: str | None = None
        # What you changed on the open form: values set by hand keyed by blank
        # id, and blanks you cleared. Kept apart from the plan, which is rebuilt
        # whenever the identity changes, so that your edits survive a replan.
        self.overrides: dict[str, object] = {}
        self.removed: set[str] = set()
        # What a model made of the open form, if you asked one, and how.
        self.reading: llm.Reading | None = None
        self.model_notes: list[str] = []
        self.model_changes: list = []
        # How this form was filled the last time it was saved, if ever.
        self.memory: memory.Memory | None = None
        self.memory_changes: list = []
        self.llm_backend = self.library.setting(
            "llm_backend", "agent" if llm.agent_available() else "ollama")
        self.lock_on_save = False
        # Set while a model is reading the form: its name, and when it started.
        self.model_busy: tuple[str, float] | None = None
        self._busy_label: Gtk.Label | None = None
        # Regions to black out on save. For this file and this occasion only:
        # what has to go from a form is not a property of the form.
        self.redactions: list[redact.Region] = []
        # The copy just saved, so the page can offer it for sending.
        self.last_saved: Path | None = None

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        self.stack = Adw.ViewStack()
        self.fill_page = self._build_fill_page()
        self.stack.add_titled_with_icon(self.fill_page, "fill", "Fill",
                                        "document-edit-symbolic")
        self.pages_tab = PagesTab(self)
        self.stack.add_titled_with_icon(self.pages_tab, "pages", "Pages",
                                        "view-paged-symbolic")
        self.identities_page = Adw.Bin()
        self.stack.add_titled_with_icon(self.identities_page, "identities",
                                        "Identities", "avatar-default-symbolic")

        header = Adw.HeaderBar()
        open_button = Gtk.Button(label="Open", tooltip_text="Open a form")
        open_button.connect("clicked", lambda *_: self.choose_file())
        header.pack_start(open_button)
        setup_button = Gtk.Button(icon_name="emblem-system-symbolic",
                                  tooltip_text="Setup: your details, the vault, your Omarchy agent")
        setup_button.connect("clicked", lambda *_: self.show_onboarding())
        header.pack_end(setup_button)
        header.set_title_widget(Adw.ViewSwitcher(stack=self.stack,
                                                 policy=Adw.ViewSwitcherPolicy.WIDE))

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.toasts.set_child(toolbar)

        self.stack.connect("notify::visible-child-name", self._on_tab_changed)
        # Nothing stored and never set up: the first run. Decided before
        # refresh_identities, which makes an empty "Me" to fill as.
        first_run = not self.library.all() and not self.library.setting("onboarded") \
            and not os.environ.get("OMAFORM_NO_ONBOARDING")
        self.refresh_identities()
        if first_run:
            GLib.idle_add(lambda: (self.show_onboarding(), False)[1])
        # OMAFORM_UI_TAB=identities opens on that tab. For screenshots and
        # driven tests; harmless otherwise.
        if os.environ.get("OMAFORM_UI_TAB"):
            self.stack.set_visible_child_name(os.environ["OMAFORM_UI_TAB"])
        if path:
            self.load(path)

    # -- chrome ----------------------------------------------------------

    def show_onboarding(self) -> None:
        from .onboarding import Onboarding
        Onboarding(self).present(self)

    def _choose(self, dialog: Adw.AlertDialog, callback) -> None:
        """Present a dialog with its callback guarded.

        An exception raised inside a GLib callback is printed to stderr and
        then dropped: the window carries on as if nothing happened. That is
        how a typed Social Security number vanished without a word. Here it
        becomes a toast, which is at least a wrong answer you can see.
        """
        def guarded(dlg, result) -> None:
            try:
                callback(dlg, result)
            except Exception as exc:  # noqa: BLE001, deliberately broad
                self.toast(f"Something went wrong: {exc}")
                import traceback
                traceback.print_exc()

        dialog.choose(self, None, guarded)

    def toast(self, text: str, *, action: str = "", on_action=None) -> None:
        toast = Adw.Toast(title=text, timeout=6)
        if action and on_action:
            toast.set_button_label(action)
            toast.connect("button-clicked", lambda *_: on_action())
        self.toasts.add_toast(toast)

    def _on_tab_changed(self, *_args) -> None:
        if self.stack.get_visible_child_name() == "identities":
            self.identities_page.set_child(self._build_identities_page())

    # -- the fill tab ----------------------------------------------------

    def _build_fill_page(self) -> Gtk.Widget:
        self.fill_body = Adw.Bin()
        scroller = self.fill_scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_child(self.fill_body)
        scroller.set_size_request(380, -1)

        self.preview = PagePreview(self._place_by_hand, self._activate_blank,
                                   self._redact_by_hand, self._unredact,
                                   self._typed_on_page, self._toggled_on_page)
        self.preview.set_size_request(320, -1)
        self.preview.on_moved = lambda _blank: None  # kept on the blank; saved with the form

        # The page is the primary thing once a form is open; the list beside it
        # is where a value is checked and adjusted. Before a form is open the
        # preview has nothing to show and is hidden.
        self.split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL,
                               shrink_start_child=False, shrink_end_child=True,
                               resize_start_child=True, resize_end_child=False)
        self.split.set_start_child(self.preview)
        self.split.set_end_child(scroller)
        # No fixed split: the list takes its natural width and the page the rest.
        self.preview.set_visible(False)
        self._show_empty_state()
        return self.split

    # -- asking a model ----------------------------------------------------

    def _ask_model(self) -> None:
        """Offer the two backends, say what leaves the machine, then ask."""
        if not self.document:
            return
        local = llm.pick_local_model()
        agent = llm.agent_available()
        options: list[tuple[str, str, str]] = []
        if local:
            options.append(("ollama", f"On this machine (Ollama: {local})",
                            "Nothing leaves the computer. The first answer can take a "
                            "minute while the model loads."))
        if agent:
            remote = agent in llm.REMOTE_AGENTS
            options.append(("agent", f"Omarchy's default agent ({agent})",
                            "Runs the agent in its headless mode. "
                            + ("This is a cloud service: the form's questions and your "
                               "key names go to it. Your values never do."
                               if remote else "Local, as configured.")))
        if not options:
            self.toast("No model is available: start Ollama, or pick a default agent "
                       "with `omarchy default agent`")
            return

        dialog = Adw.AlertDialog(
            heading="Have a model read this form",
            body="For forms with conditions the simple matcher cannot read, such as "
                 "\u201ccomplete this part only if\u2026\u201d. It sees the form's "
                 "questions and the names of your stored details, never the values, "
                 "and its reading is kept for this form so it runs once.")
        combo = Adw.ComboRow(title="Ask",
                             model=Gtk.StringList.new([o[1] for o in options]))
        chosen = next((i for i, o in enumerate(options) if o[0] == self.llm_backend), 0)
        combo.set_selected(chosen)
        note = Gtk.Label(label=options[chosen][2], wrap=True, xalign=0,
                         margin_top=6, margin_start=4, margin_end=4)
        note.add_css_class("dim-label")
        combo.connect("notify::selected",
                      lambda row, _p: note.set_text(options[row.get_selected()][2]))
        group = Adw.PreferencesGroup()
        group.add(combo)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(group)
        box.append(note)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        if self.reading is not None:
            dialog.add_response("forget", "Forget its reading")
        dialog.add_response("ask", "Ask")
        dialog.set_response_appearance("ask", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ask")
        dialog.set_close_response("cancel")

        def responded(dlg, result) -> None:
            try:
                response = dlg.choose_finish(result)
            except GLib.Error:
                return
            if response == "forget":
                llm.forget(self.document)
                self.reading, self.model_notes = None, []
                self.rebuild_plan()
                return
            if response != "ask":
                return
            backend, label, _ = options[combo.get_selected()]
            self.llm_backend = backend
            self.library.set_setting("llm_backend", backend)
            self._run_model(backend, label)

        self._choose(dialog, responded)

    def _run_model(self, backend: str, label: str) -> None:
        identity = self.current_identity()
        if identity is None or self.plan is None:
            return
        import time

        doc, kind = self.document, identity.kind
        filled = {e.blank.id: e.match.key for e in self.plan.filled if e.match}
        # Values in hand, so they can be cut out of the labels; never sent.
        known = [v for v in self.plan.values().values() if v != "checked"]
        known += self.build_profile(identity).values_in_hand()
        self.model_busy = (label, time.monotonic())
        self._render_fill()
        GLib.timeout_add(400, self._tick_busy)

        def work() -> None:
            try:
                reading = llm.read_form(doc, kind, filled, backend, known_values=known)
            except llm.ModelError as exc:
                GLib.idle_add(self._model_failed, str(exc), doc)
                return
            GLib.idle_add(self._model_done, reading, doc)

        threading.Thread(target=work, daemon=True).start()

    def _busy_text(self) -> str:
        import time

        if self.model_busy is None:
            return ""
        seconds = int(time.monotonic() - self.model_busy[1])
        return f"Reading now{'.' * (1 + seconds % 3):<3}  {seconds}s"

    def _tick_busy(self) -> bool:
        if self.model_busy is None:
            return False
        if self._busy_label is not None:
            self._busy_label.set_text(self._busy_text())
        return True

    def _model_failed(self, message: str, doc=None) -> bool:
        if doc is not None and doc is not self.document:
            return False  # another form was opened meanwhile
        self.model_busy = None
        self._render_fill()
        self.toast(f"The model could not read it: {message}")
        return False

    def _model_done(self, reading: llm.Reading, doc=None) -> None:
        # A reading belongs to the form it was asked about. If another form
        # is open now, it was remembered for that one and waits there.
        if doc is not None and doc is not self.document:
            return
        self.model_busy = None
        self.reading = reading
        self.rebuild_plan()

        def summarise() -> bool:
            cleared = sum(1 for e in self.model_changes if not e.filled)
            added = sum(1 for e in self.model_changes if e.filled)
            self.toast(f"The model cleared {cleared} and filled or corrected {added}"
                       + (f", and has {len(self.model_notes)} questions"
                          if self.model_notes else ""))
            return False

        GLib.timeout_add(1200, summarise)

    def _forget_form(self) -> None:
        if self.document is None:
            return
        memory.forget(self.document)
        self.memory, self.memory_changes = None, []
        self.toast("Forgotten. It will be read fresh from now on.")
        self.rebuild_plan()

    def _render_docx_preview(self, path: str) -> None:
        from ..adapters import docx as docx_adapter

        if docx_adapter.soffice() is None:
            self.toast("No page view for Word documents without LibreOffice")
            return
        # A render of a filled document holds its answers in plain text, so it
        # lives in a private folder that goes when the file is closed, another
        # is opened, or the window closes.
        self._drop_preview_dir()
        folder = self._preview_dir()
        out = Path(folder) / (Path(path).stem + ".pdf")

        def work() -> None:
            try:
                docx_adapter.to_pdf(path, str(out))
            except Exception as exc:  # noqa: BLE001
                if self.doc_path == path and self._preview_tmp == folder:
                    GLib.idle_add(self.toast, f"No page view: {exc}")
                return
            if self._preview_tmp != folder:
                import shutil
                shutil.rmtree(folder, ignore_errors=True)  # closed meanwhile
                return
            GLib.idle_add(done)

        def done() -> bool:
            if self.doc_path != path:
                return False  # another file was opened meanwhile
            self.preview_path = str(out)
            if self.plan is not None:
                self.preview.show(self.preview_path, self.document.blanks, self.plan)
                self.preview.set_visible(True)
            return False

        threading.Thread(target=work, daemon=True).start()

    def _preview_dir(self) -> str:
        if not getattr(self, "_preview_tmp", None):
            import tempfile
            self._preview_tmp = tempfile.mkdtemp(prefix="omaform-preview-")
        return self._preview_tmp

    def _drop_preview_dir(self) -> None:
        import shutil
        folder = getattr(self, "_preview_tmp", None)
        self._preview_tmp = None
        if folder:
            shutil.rmtree(folder, ignore_errors=True)

    def _redact_by_hand(self, page_no: int, rect) -> None:
        """Dragged a box in Black out mode: that region goes on save."""
        self.redactions.append(redact.Region(page_no, tuple(rect)))
        self.preview.redactions = self.redactions
        self.preview.area.queue_draw()
        self._render_fill()

    def _unredact(self, region) -> None:
        self.redactions = [r for r in self.redactions if r != region]
        self.preview.redactions = self.redactions
        self.preview.area.queue_draw()
        self._render_fill()

    def _place_by_hand(self, page_no: int, kind, x: float, y: float) -> None:
        """Right-click on the page: sign or date exactly there."""
        if not self.document:
            return
        identity = self.current_identity()
        if identity is None:
            return
        # Asked of the profile, the same way planning asks, so the answer
        # agrees with what the page already shows.
        if kind is BlankKind.SIGNATURE and not self.build_profile(identity).has("signature"):
            self.toast("Draw a signature first")
            self._draw_signature(identity)
            return
        blank = placed_by_hand(page_no, kind, x, y)
        if kind is BlankKind.TEXT:
            def typed(text: str) -> None:
                if not text.strip():
                    return
                self.document.blanks.append(blank)
                self.overrides[blank.id] = text
                self.rebuild_plan()

            self._ask_text("Type here", "", typed)
            return
        self.document.blanks.append(blank)
        if kind is BlankKind.CHECKBOX:
            self.overrides[blank.id] = "checked"
        self.rebuild_plan()

    def _show_empty_state(self) -> None:
        status = Adw.StatusPage(
            icon_name="document-open-symbolic",
            title="Open a form",
            description="A PDF with form fields. Omaform fills in what it "
                        "recognises from the identity you choose.")
        button = Gtk.Button(label="Choose a file", halign=Gtk.Align.CENTER)
        button.add_css_class("suggested-action")
        button.add_css_class("pill")
        button.connect("clicked", lambda *_: self.choose_file())
        status.set_child(button)
        self.fill_body.set_child(status)

    def choose_file(self) -> None:
        dialog = Gtk.FileDialog(title="Open a form")
        pdfs = Gtk.FileFilter(name="Forms")
        for pattern in ("*.pdf", "*.PDF", "*.docx", "*.DOCX"):
            pdfs.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(pdfs)
        dialog.set_filters(filters)
        downloads = Path.home() / "Downloads"
        if downloads.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(downloads)))
        dialog.open(self, None, self._file_chosen)

    def _file_chosen(self, dialog, result) -> None:
        try:
            file = dialog.open_finish(result)
        except GLib.Error:
            return  # cancelled
        if file:
            self.load(file.get_path())

    def load(self, path: str) -> None:
        self._drop_preview_dir()
        self.doc_path = path
        try:
            self.document = for_path(path).discover(path)
        except Exception as exc:
            self.document = None
            self.toast(f"Could not read {Path(path).name}: {exc}")
            self.preview.set_visible(False)
            self._show_empty_state()
            return
        self.preview.document = None  # a new file starts on its first page
        self.preview_path = path
        self.preview.set_blackout(False)
        if self.document.fmt == "docx":
            # A Word document has no pages of its own to show. LibreOffice
            # makes a PDF to look at, in the background; the field list is
            # the interface meanwhile, and after.
            self.preview_path = None
            self._render_docx_preview(path)
        else:
            self.pages_tab.load(path)
        self.overrides, self.removed = {}, set()
        self.redactions, self.preview.redactions = [], []
        self.last_saved = None
        self.model_busy = None   # a reading still running belongs to the old file
        self.reading, self.model_notes = llm.remembered(self.document), []
        self.memory, self.memory_changes = memory.recall(self.document), []
        if self.memory:
            memory.restore_placements(self.memory, self.document)
            self.toast("Filled as last time; the rows say which")
        elif self.reading:
            self.toast("Using what the model worked out about this form last time")
        self.rebuild_plan()

    # -- deciding what goes where ----------------------------------------

    def current_identity(self) -> Identity | None:
        """Who the fill page is filling as: the chosen identity, else the default."""
        if self.filling_slug:
            chosen = self.library.get(self.filling_slug)
            if chosen is not None:
                return chosen
        return self.library.resolve(None)

    def build_profile(self, identity: Identity) -> Profile:
        """The identity's details, with the vault attached but not opened."""
        profile = Profile(dict(identity.values))
        if self.vault.exists:
            keys = frozenset(self.vault.keys_for(identity.slug))
            if keys:
                profile.vault_keys = keys
                profile.unlocker = lambda: self._unlock_blocking(identity, keys)
        return profile

    def _unlock_blocking(self, identity: Identity, keys) -> dict[str, str]:
        """Unlock from inside plan building, which is not on the main loop.

        Plan building runs in a worker thread so a slow document cannot freeze
        the window, and it may discover mid-way that it needs a secret. The
        dialog has to be raised on the main loop and waited for here.
        """
        if self.vault.unlocked:
            return self.vault.values_for(identity.slug)

        stored = vaulting.keyring_lookup()
        if stored:
            try:
                self.vault.unlock(stored)
                return self.vault.values_for(identity.slug)
            except vaulting.VaultError:
                pass

        done = threading.Event()
        answer: dict[str, str] = {}

        def ask() -> None:
            names = ", ".join(BY_KEY[k].title if k in BY_KEY else k
                              for k in sorted(keys))
            dialog = PassphraseDialog(f"This form needs {names} for "
                                      f"{identity.label}.")

            def responded(dlg, result) -> None:
                try:
                    response = dlg.choose_finish(result)
                except GLib.Error:
                    response = "cancel"
                if response == "unlock":
                    phrase = dialog.entry.get_text()
                    try:
                        self.vault.unlock(phrase)
                        answer.update(self.vault.values_for(identity.slug))
                    except vaulting.VaultError as exc:
                        GLib.idle_add(self.toast, str(exc))
                done.set()

            self._choose(dialog, responded)

        GLib.idle_add(ask)
        done.wait(timeout=300)
        return answer

    def rebuild_plan(self) -> None:
        if not self.document:
            return
        identity = self.current_identity()
        if identity is None:
            self.toast("Create an identity first, on the Identities tab")
            return
        self._render_fill(busy=True)
        # Everything the worker needs is captured now. A later request (another
        # identity, another file) bumps the generation, and an answer for an
        # older one is dropped rather than laid over the form now open.
        self._plan_generation = getattr(self, "_plan_generation", 0) + 1
        generation = self._plan_generation
        doc, reading, remembered = self.document, self.reading, self.memory

        def work() -> None:
            # Memory and the model resolve keys from the profile, which may
            # need the vault. That has to happen here, off the main loop: the
            # passphrase dialog is raised on the main loop and waited for.
            profile = self.build_profile(identity)
            try:
                result = planning.build(doc, profile, identity.preference())
                memory_changes = (memory.apply(remembered, result, profile, identity.slug)
                                  if remembered is not None else [])
                model_changes, model_notes = (llm.apply(reading, doc, result, profile)
                                              if reading is not None else ([], []))
            except Exception as exc:  # a bad document should not kill the window
                GLib.idle_add(self.toast, f"Could not plan the fill: {exc}")
                return
            GLib.idle_add(self._plan_ready, generation, doc, result,
                          memory_changes, model_changes, model_notes)

        threading.Thread(target=work, daemon=True).start()

    def _plan_ready(self, generation, doc, result, memory_changes=(),
                    model_changes=(), model_notes=()) -> bool:
        if generation != getattr(self, "_plan_generation", 0) or doc is not self.document:
            return False
        self.memory_changes = list(memory_changes)
        if self.reading is not None:
            self.model_changes, self.model_notes = list(model_changes), list(model_notes)
        self._apply_edits(result)
        self.plan = result
        self._render_fill()
        if self.doc_path and self.document:
            if self.preview_path is None:
                self.preview.set_visible(False)   # still rendering, or not renderable
            elif self.preview.document is None or not self.preview.get_visible():
                self.preview.show(self.preview_path, self.document.blanks, result)
                self.preview.set_visible(True)
            else:
                self.preview.blanks = self.document.blanks
                self.preview.set_plan(result)

    # -- what you changed by hand ------------------------------------------

    def _apply_edits(self, plan) -> None:
        """Lay your own changes over a freshly built plan."""
        for entry in plan.entries:
            blank_id = entry.blank.id
            if blank_id in self.removed:
                entry.value, entry.image, entry.note, entry.cleared = None, None, "cleared by you", True
            elif blank_id in self.overrides:
                value = self.overrides[blank_id]
                if isinstance(value, bytes):
                    entry.image = value
                else:
                    entry.value = str(value)
                entry.note = "set by you"

    def _entry_for(self, blank_id: str):
        if self.plan is None:
            return None
        return next((e for e in self.plan.entries if e.blank.id == blank_id), None)

    def _remove(self, entry) -> None:
        """Clear a value: it stays clear through replans until set again."""
        self.removed.add(entry.blank.id)
        self.overrides.pop(entry.blank.id, None)
        entry.value, entry.image, entry.note, entry.cleared = None, None, "cleared by you", True
        self._render_fill()
        self.preview.set_plan(self.plan)

    def _set_override(self, blank_id: str, value, *, rerender: bool = True) -> None:
        self.overrides[blank_id] = value
        self.removed.discard(blank_id)
        entry = self._entry_for(blank_id)
        if entry is not None:
            if isinstance(value, bytes):
                entry.image = value
            else:
                entry.value = str(value)
            entry.note = "set by you"
        if rerender:
            self._render_fill()
        self.preview.set_plan(self.plan)

    def _typed_on_page(self, blank, text: str, final: bool) -> None:
        """Keys went straight into a box on the page. The list is rebuilt only
        when the box is left, so typing stays smooth."""
        entry = self._entry_for(blank.id)
        if entry is None:
            return
        if final and text == "":
            if entry.filled:
                self._remove(entry)
            return
        self._set_override(blank.id, text, rerender=final)

    def _toggled_on_page(self, blank) -> None:
        entry = self._entry_for(blank.id)
        if entry is None:
            return
        if entry.filled:
            self._remove(entry)
        else:
            self._set_override(blank.id, "checked")

    def _activate_blank(self, blank, _x: float, _y: float) -> None:
        """A click on a field in the page: toggle a box, or ask for text."""
        entry = self._entry_for(blank.id)
        if entry is None:
            return
        if blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
            if entry.filled:
                self._remove(entry)
            else:
                self._set_override(blank.id, "checked")
            return
        if blank.kind is BlankKind.SIGNATURE:
            if not entry.filled:
                identity = self.current_identity()
                if identity is not None:
                    self._draw_signature(identity)
                return
            # A signature already there, placed or found: the click offers to
            # take it off, which was otherwise only possible from the list.
            dialog = Adw.AlertDialog(heading="Remove this signature?",
                                     body="It comes off this form only; the stored "
                                          "signature is kept.")
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("remove", "Remove")
            dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_close_response("cancel")

            def responded(dlg, result) -> None:
                try:
                    if dlg.choose_finish(result) == "remove":
                        self._remove(entry)
                except GLib.Error:
                    pass

            self._choose(dialog, responded)
            return
        self._ask_text(f"{BY_KEY[entry.match.key].title if entry.match else 'This box'}: "
                       f"\u201c{self._label_of(entry)}\u201d",
                       entry.value or "",
                       lambda text: self._set_override(blank.id, text),
                       on_clear=(lambda: self._remove(entry)) if entry.filled else None)

    def _ask_text(self, heading: str, current: str, then, on_clear=None) -> None:
        """Ask for a box's text. A box that already has some can be cleared
        here too, rather than by finding its row and deleting the text."""
        dialog = Adw.AlertDialog(heading=heading, body="")
        entry = Adw.EntryRow(title="Text", text=current)
        group = Adw.PreferencesGroup()
        group.add(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        if on_clear is not None and current:
            dialog.add_response("clear", "Clear")
            dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")
        dialog.set_close_response("cancel")

        def responded(dlg, result) -> None:
            try:
                response = dlg.choose_finish(result)
            except GLib.Error:
                return
            if response == "clear" and on_clear is not None:
                on_clear()
            elif response == "save":
                then(entry.get_text())

        self._choose(dialog, responded)

    # -- the fill view ---------------------------------------------------

    def _render_fill(self, busy: bool = False) -> None:
        if not self.document or not self.doc_path:
            self._show_empty_state()
            return

        page = Adw.PreferencesPage()
        page.add_css_class("omaform-page")

        group = Adw.PreferencesGroup(title=Path(self.doc_path).name)
        group.add_css_class("omaform-filename")
        identities = self.library.all()
        model = Gtk.StringList()
        for identity in identities:
            model.append(identity.label)
        self.identity_row = Adw.ComboRow(title="Filling as", model=model)
        slugs = [i.slug for i in identities]
        current = self.current_identity()
        if current and current.slug in slugs:
            self.identity_row.set_selected(slugs.index(current.slug))

        def switched(row, _param) -> None:
            # The bug this replaces: reading the choice back from a row that
            # had just been rebuilt, which put every choice back to the first.
            index = row.get_selected()
            if 0 <= index < len(slugs) and slugs[index] != self.filling_slug:
                self.filling_slug = slugs[index]
                self.rebuild_plan()

        self.identity_row.connect("notify::selected", switched)
        group.add(self.identity_row)
        page.add(group)

        self._busy_label = None
        if self.model_busy is not None:
            reading = Adw.PreferencesGroup()
            row = Adw.ActionRow(title=f"Asking {self.model_busy[0]}",
                                subtitle="The first answer can take a minute. You can "
                                         "keep working; its changes arrive here.")
            row.add_prefix(Adw.Spinner())
            self._busy_label = Gtk.Label(label=self._busy_text())
            self._busy_label.add_css_class("omaform-reading")
            row.add_suffix(self._busy_label)
            reading.add(row)
            page.add(reading)

        if busy or self.plan is None:
            waiting = Adw.PreferencesGroup()
            waiting.add(Adw.ActionRow(title="Reading the form...", subtitle=""))
            page.add(waiting)
            self.fill_body.set_child(page)
            return

        filled = self.plan.filled
        results = Adw.PreferencesGroup(
            title=f"{len(filled)} of {len(self.document.fillable())} blanks",
            description="Everything Omaform is confident about. The rest is left "
                        "empty on purpose.")
        for entry in filled:
            spec = BY_KEY[entry.match.key] if entry.match else None
            if entry.image is not None:
                shown = "your drawn signature"
            elif entry.value == "checked":
                shown = f"\u2611 {entry.note}"
            else:
                shown = "•" * len(entry.value) if spec and spec.sensitive \
                    else entry.value
            # Lead with our own name for the field, then the value, then the
            # form's own wording. Government labels wrap and bleed in from
            # neighbouring columns, so the form's wording alone is often barely
            # readable, but you still need it to check the value is going
            # somewhere sane.
            editable = (entry.image is None and entry.value != "checked"
                        and not (spec and spec.sensitive))
            if editable:
                # Adjust a value here and the page follows. The plan entry is
                # changed in place, so the next Fill writes what is shown.
                row = Adw.EntryRow(
                    title=(f"{spec.title}  →  “{self._label_of(entry)}”"
                           if spec else self._label_of(entry)),
                    text=entry.value or "")
                row.set_tooltip_text(entry.blank.label.best())

                def edited(widget, _param, entry=entry) -> None:
                    entry.value = widget.get_text()
                    self.overrides[entry.blank.id] = entry.value
                    self.removed.discard(entry.blank.id)
                    self.preview.area.queue_draw()

                row.connect("notify::text", edited)
            else:
                row = Adw.ActionRow(
                    title=spec.title if spec else self._label_of(entry),
                    subtitle=f"{shown}" + (f"\ninto “{self._label_of(entry)}”"
                                           if spec else ""))
                row.set_subtitle_lines(2)
                row.set_tooltip_text(entry.blank.label.best())
                if spec and spec.sensitive:
                    row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
            row.add_css_class("omaform-filled")
            clear = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Clear this one")
            clear.add_css_class("flat")
            clear.connect("clicked", lambda _b, entry=entry: self._remove(entry))
            row.add_suffix(clear)
            self._follow_on_page(row, entry.blank.id)
            results.add(row)
        if not filled:
            results.add(Adw.ActionRow(
                title="Nothing matched",
                subtitle="This identity has no details that fit this form yet"))
        page.add(results)

        # Things this form asks for that the identity simply has not got. These
        # are the fixable gaps, and burying them in the "left empty" list is how
        # a form goes out missing a tax number without anyone noticing.
        missing = self.plan.missing()
        if missing:
            identity = self.current_identity()
            group = Adw.PreferencesGroup(
                title="Asked for, but not stored yet",
                description=f"Add these to {identity.label if identity else 'this identity'} "
                            f"and they will fill in from now on.")
            for key in missing:
                spec = BY_KEY[key]
                row = Adw.ActionRow(
                    title=spec.title,
                    subtitle="drawn once, stored encrypted" if spec.image
                             else "encrypted in your vault" if spec.sensitive
                             else "kept with this identity's details")
                if spec.sensitive:
                    row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
                button = Gtk.Button(label="Draw" if spec.image else "Add",
                                    valign=Gtk.Align.CENTER)
                button.add_css_class("suggested-action")
                button.connect("clicked", self._add_missing, key)
                row.add_suffix(button)
                group.add(row)
            page.add(group)

        suggested = [e for e in self.plan.suggested if e.blank.id not in self.removed]
        if suggested:
            identity = self.current_identity()
            profile = self.build_profile(identity) if identity else None
            maybe = Adw.PreferencesGroup(
                title="Probably, but not sure",
                description="Close matches the matcher would not write on its own. "
                            "Accept the ones that are right.")
            for entry in suggested[:20]:
                spec = BY_KEY[entry.suggested_key]
                have = profile.has(entry.suggested_key) if profile else False
                row = Adw.ActionRow(title=self._label_of(entry),
                                    subtitle=f"{spec.title}?" if have
                                    else f"{spec.title}? (not stored)")
                if have:
                    accept = Gtk.Button(label="Accept", valign=Gtk.Align.CENTER)
                    accept.connect("clicked", self._accept_suggestion, entry)
                    row.add_suffix(accept)
                self._follow_on_page(row, entry.blank.id)
                maybe.add(row)
            page.add(maybe)

        skipped = [e for e in self.plan.unmatched
                   if e.note != "read only" and not e.suggested_key]
        if skipped:
            group = Adw.PreferencesGroup()
            expander = Adw.ExpanderRow(
                title=f"{len(skipped)} left empty",
                tooltip_text="Checkboxes, another party's section, and anything "
                             "Omaform was not sure about")
            for entry in skipped[:60]:
                kind = entry.blank.kind
                if kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
                    row = Adw.ActionRow(title=self._label_of(entry), subtitle=entry.note)
                    tick = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                    tick.connect("toggled", lambda b, entry=entry: (
                        self._set_override(entry.blank.id, "checked", rerender=False)
                        if b.get_active() else self._remove_quietly(entry)))
                    row.add_suffix(tick)
                elif kind in (BlankKind.TEXT, BlankKind.MULTILINE, BlankKind.DATE):
                    row = Adw.EntryRow(title=self._label_of(entry))
                    row.set_tooltip_text(entry.note)

                    def typed(widget, _param, entry=entry) -> None:
                        self._set_override(entry.blank.id, widget.get_text(), rerender=False)

                    row.connect("notify::text", typed)
                else:
                    row = Adw.ActionRow(title=self._label_of(entry), subtitle=entry.note)
                row.add_css_class("omaform-skipped")
                self._follow_on_page(row, entry.blank.id)
                expander.add_row(row)
            if len(skipped) > 60:
                expander.add_row(Adw.ActionRow(
                    title=f"and {len(skipped) - 60} more"))
            group.add(expander)
            page.add(group)

        for group_keys in self.plan.conflicts:
            keys = sorted(group_keys)
            choice = Adw.PreferencesGroup(
                title="This form wants one or the other",
                description=f"Left blank until you choose between "
                            f"{' and '.join(BY_KEY[k].title for k in keys)}. "
                            f"Filling both is wrong on a form you sign.")
            box = Gtk.Box(spacing=8, margin_top=4, halign=Gtk.Align.START)
            for key in keys:
                button = Gtk.Button(label=f"Use {BY_KEY[key].title}")
                button.connect("clicked", self._prefer, key)
                box.append(button)
            row = Adw.ActionRow()
            row.set_child(box)
            choice.add(row)
            page.add(choice)

        if self.memory_changes:
            last = Adw.PreferencesGroup(
                title="As last time",
                description="Filled the way this form was saved before.")
            for entry in self.memory_changes[:20]:
                row = Adw.ActionRow(title=self._label_of(entry), subtitle=entry.note)
                row.add_css_class("omaform-filled" if entry.filled else "omaform-skipped")
                self._follow_on_page(row, entry.blank.id)
                last.add(row)
            forget = Gtk.Button(label="Forget this form", halign=Gtk.Align.START,
                                margin_top=4)
            forget.connect("clicked", lambda *_: self._forget_form())
            holder = Adw.ActionRow()
            holder.set_child(forget)
            last.add(holder)
            page.add(last)

        if self.model_changes:
            audit = Adw.PreferencesGroup(
                title="The model changed",
                description="Where it disagreed with the simple matcher. Undo one "
                            "by filling it again from the page.")
            for entry in self.model_changes[:20]:
                row = Adw.ActionRow(title=self._label_of(entry), subtitle=entry.note)
                row.set_subtitle_lines(3)
                row.add_css_class("omaform-filled" if entry.filled else "omaform-skipped")
                self._follow_on_page(row, entry.blank.id)
                audit.add(row)
            page.add(audit)

        if self.model_notes:
            asks = Adw.PreferencesGroup(
                title="The model asks",
                description="Things it could not settle from your details alone.")
            for note in self.model_notes[:12]:
                asks.add(Adw.ActionRow(title=note))
            page.add(asks)

        if self.last_saved is not None and self.last_saved.exists():
            page.add(self._saved_group(self.last_saved))

        if self.redactions:
            blacked = Adw.PreferencesGroup(
                title="Blacked out on save",
                description="These pages are re-rendered without what is under the box. "
                            "A blacked-out page can no longer be searched or filled.")
            for region in self.redactions:
                x0, y0, x1, y1 = region.rect
                row = Adw.ActionRow(
                    title=f"Page {region.page}",
                    subtitle=f"{x1 - x0:.0f} by {y1 - y0:.0f} points")
                clear = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                                   tooltip_text="Remove this black-out")
                clear.add_css_class("flat")
                clear.connect("clicked", lambda _b, region=region: self._unredact(region))
                row.add_suffix(clear)
                blacked.add(row)
            page.add(blacked)

        actions = Adw.PreferencesGroup()
        buttons = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=8)
        fill_button = Gtk.Button(label="Fill and save",
                                 sensitive=bool(filled) or bool(self.redactions))
        fill_button.add_css_class("suggested-action")
        fill_button.add_css_class("pill")
        fill_button.connect("clicked", lambda *_: self.do_fill())
        save_as_button = Gtk.Button(label="Save as\u2026",
                                    sensitive=bool(filled) or bool(self.redactions),
                                    tooltip_text="Choose the name and folder")
        save_as_button.add_css_class("pill")
        save_as_button.connect("clicked", lambda *_: self._save_as())
        ask_button = Gtk.Button(
            label=("Reading now\u2026" if self.model_busy else
                   "Model has read it" if self.reading else "Ask a model to read it\u2026"),
            sensitive=self.model_busy is None,
            tooltip_text="For forms with conditions the simple matcher cannot read. "
                         "Only the form's questions and your key names are sent, "
                         "never your values.")
        ask_button.add_css_class("pill")
        ask_button.connect("clicked", lambda *_: self._ask_model())
        buttons.append(fill_button)
        buttons.append(save_as_button)
        actions.add(buttons)
        ask_box = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=8)
        ask_box.append(ask_button)
        actions.add(ask_box)
        if self.document.fmt == "pdf":
            blackout_row = Adw.SwitchRow(
                title="Black out",
                tooltip_text="Drag over anything that must not leave with the file. On "
                             "save the page is re-rendered with it gone, not just covered. "
                             "The \u00d7 on a box removes it.",
                active=self.preview.blackout_on)
            blackout_row.connect("notify::active",
                                 lambda row, _p: self.preview.set_blackout(row.get_active()))
            actions.add(blackout_row)
        lock_row = Adw.SwitchRow(
            title="Lock when saving",
            tooltip_text=("Saves a PDF instead of a Word file, so no one can edit it."
                      if self.document.fmt == "docx" else
                      "Burns the fill into the page so no one can edit it. Leave off "
                      "for a form someone else still has a section to fill, like an I-9."),
            active=self.lock_on_save)
        lock_row.connect("notify::active",
                         lambda row, _p: setattr(self, "lock_on_save", row.get_active()))
        actions.add(lock_row)
        page.add(actions)

        # Rebuilt after every change; put the list back where it was.
        adj = self.fill_scroller.get_vadjustment()
        where = adj.get_value()
        self.fill_body.set_child(page)
        GLib.idle_add(lambda: (adj.set_value(where), False)[1])

    def _prefer(self, _button, key: str) -> None:
        """Take one side of an either/or, and remember it for this identity.

        Remembered rather than asked again, because the answer is a property of
        who is filing, not of the form: a business files under an Employer
        Identification number every time.
        """
        identity = self.current_identity()
        if not identity or not self.document:
            return
        self.library.set_preference(identity.slug, key)
        self.toast(f"{identity.label} will use {BY_KEY[key].title} from now on")
        self.rebuild_plan()

    def _accept_suggestion(self, _button, entry) -> None:
        identity = self.current_identity()
        if identity is None or not entry.suggested_key:
            return
        key, blank_id, doc = entry.suggested_key, entry.blank.id, self.document
        profile = self.build_profile(identity)

        # Fetched off the main loop: the key may be in the vault, and its
        # passphrase dialog is raised on the main loop and waited for.
        def work() -> None:
            value = profile.get(key)

            def done() -> bool:
                if value and doc is self.document:
                    self._set_override(blank_id, value)
                return False

            GLib.idle_add(done)

        threading.Thread(target=work, daemon=True).start()

    def _remove_quietly(self, entry) -> None:
        """Clear without rebuilding the list, for a control you are still on."""
        self.removed.add(entry.blank.id)
        self.overrides.pop(entry.blank.id, None)
        entry.value, entry.image, entry.note, entry.cleared = None, None, "cleared by you", True
        self.preview.set_plan(self.plan)

    def _follow_on_page(self, row, blank_id: str) -> None:
        """Hover a row and the page shows which box it is, turning to it if needed."""
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", lambda *_: self.preview.highlight(blank_id, reveal=True))
        motion.connect("leave", lambda *_: self.preview.highlight(None))
        row.add_controller(motion)

    @staticmethod
    def _label_of(entry) -> str:
        """The form's own words for this blank, tidied onto one line.

        The words the match was made from, when there was a match: the
        shortest scrap of nearby text can be a sidebar's "Enter", which says
        nothing about the box that was filled.
        """
        label = entry.blank.label.best().strip()
        if entry.match and entry.match.source not in ("native", "kind"):
            label = (getattr(entry.blank.label, entry.match.source, "") or label).strip()
        if not label:
            return "unlabelled"
        # Strip the form's own line number: "6City, state" and "1 Name of" and
        # "3a Check the" all lead with one. The sub-item letter is taken only
        # when a space follows it, so that the "A" of "5Address" is kept, and
        # the whole thing only fires when real label text follows. "See " goes
        # too: the W-9's vertical "See ... Specific Instructions" sidebar sits
        # on the same line as the address label and bleeds into it.
        label = re.sub(r"^(?:[Ss]ee\s+)?\d{1,2}(?:[a-z](?=\s))?\s*(?=[A-Z(])",
                       "", label)
        return label if len(label) <= 52 else label[:51].rstrip() + "…"

    def _save_as(self) -> None:
        """Fill and save, to a name and folder of your choosing."""
        if not (self.plan and self.doc_path):
            return
        source = Path(self.doc_path)
        suffix = ".pdf" if (self.lock_on_save and self.document.fmt == "docx") else source.suffix
        dialog = Gtk.FileDialog(title="Save the filled form as")
        dialog.set_initial_folder(Gio.File.new_for_path(str(source.parent)))
        dialog.set_initial_name(f"{source.stem}-filled{suffix}")

        def chosen(dlg, result) -> None:
            try:
                file = dlg.save_finish(result)
            except GLib.Error:
                return  # cancelled
            if file is None:
                return
            target = Path(file.get_path())
            if target.resolve() == source.resolve():
                self.toast("Choose a new name: the original is never overwritten")
                return
            if target.suffix.lower() != source.suffix.lower() and suffix == source.suffix:
                target = target.with_suffix(source.suffix)
            self.do_fill(target)

        dialog.save(self, None, chosen)

    def do_fill(self, out: Path | None = None) -> None:
        if not (self.plan and self.doc_path):
            return
        source = Path(self.doc_path)
        if out is None:
            out = source.with_name(f"{source.stem}-filled{source.suffix}")
        final = out.with_suffix(".pdf") if (self.lock_on_save and self.document.fmt == "docx") \
            else out
        if source.resolve() in (Path(out).resolve(), final.resolve()):
            self.toast("Choose a new name: the original is never overwritten")
            return
        try:
            for_path(self.doc_path).write(self.document, self.plan.values(), str(out),
                                          images=self.plan.images(),
                                          lock_form=self.lock_on_save)
            if self.lock_on_save and self.document.fmt == "docx":
                out = out.with_suffix(".pdf")   # locking a Word file makes a PDF
            if self.redactions and out.suffix.lower() == ".pdf":
                # After the fill, so what is blacked out is the page as saved.
                # Written beside and swapped in; the source is never touched.
                # Staged in a file made just for this, beside the output so the
                # final rename stays on one filesystem; never a name derived
                # from the output, which can be the source's own name.
                import tempfile
                fd, staged = tempfile.mkstemp(prefix=".omaform-", suffix=".pdf",
                                              dir=str(out.parent))
                os.close(fd)
                try:
                    redact.apply(str(out), self.redactions, staged)
                    os.replace(staged, out)
                finally:
                    if os.path.exists(staged):
                        os.unlink(staged)
        except Exception as exc:
            self.toast(f"Could not save: {exc}")
            return
        # Saving is the check. Remember the shape of what was accepted, so the
        # same form fills this way next time, with nothing to press.
        identity = self.current_identity()
        if identity is not None:
            self.memory = memory.remember(self.document, self.plan, identity.slug)

        where = out.parent
        try:
            where = out.parent.relative_to(Path.home())
            shown = f"~/{where}/{out.name}"
        except ValueError:
            shown = str(out)
        if self.plan.sensitive_used:
            names = ", ".join(BY_KEY[k].title for k in sorted(self.plan.sensitive_used))
            self.toast(f"Saved {shown}. It holds your {names} in plain text.",
                       action="Open", on_action=lambda: self._open(out))
        else:
            self.toast(f"Saved {shown}", action="Open",
                       on_action=lambda: self._open(out))
        # The saved copy is what gets arranged and sent, so the Pages tab
        # moves on to it: drop the instructions, add a photo of an ID, merge.
        if out.suffix.lower() == ".pdf":
            self.pages_tab.load(str(out))
        self.last_saved = out
        self._render_fill()

    @staticmethod
    def _open(path: Path) -> None:
        Gio.AppInfo.launch_default_for_uri(f"file://{path}", None)

    def _saved_group(self, out: Path) -> Adw.PreferencesGroup:
        """The saved file, ready to go somewhere: drag it into an email, copy
        it, open it, or start a message with it attached."""
        group = Adw.PreferencesGroup(title="Saved")
        row = Adw.ActionRow(
            title=out.name,
            subtitle="Drag this row into an email or a folder, or copy it and paste it there")
        row.add_prefix(Gtk.Image.new_from_icon_name("document-send-symbolic"))

        def provider():
            return Gdk.ContentProvider.new_for_value(
                Gdk.FileList.new_from_list([Gio.File.new_for_path(str(out))]))

        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.set_content(provider())
        row.add_controller(drag)

        def copy(_b) -> None:
            self.get_clipboard().set_content(provider())
            self.toast(f"Copied {out.name}. Paste it into the message.")

        def email(_b) -> None:
            # xdg-email reaches whatever answers mailto:, which on Omarchy is
            # the shell's mail view. A web mailbox cannot take the attachment
            # from the command line, so the file goes on the clipboard too.
            self.get_clipboard().set_content(provider())
            try:
                subprocess.Popen(["xdg-email", "--attach", str(out)], start_new_session=True)
            except OSError as exc:
                self.toast(f"Could not start a message: {exc}")
                return
            self.toast("Message opened. If the file is not attached, paste or drag it in.")

        for label, handler in (("Open", lambda _b: self._open(out)),
                               ("Copy", copy), ("Email…", email)):
            button = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            button.connect("clicked", handler)
            row.add_suffix(button)
        group.add(row)
        return group

    # -- the identities tab ----------------------------------------------

    def refresh_identities(self) -> None:
        if not self.library.all():
            self.library.create("Me")

    def _build_identities_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        page.add_css_class("omaform-page")
        identities = self.library.all()
        default = self.library.default_slug()

        chooser = Adw.PreferencesGroup(
            title="Identities",
            description="Who a form can be filled in as. Each keeps its own "
                        "details and its own secrets.")
        model = Gtk.StringList()
        for identity in identities:
            model.append(identity.label)
        self.editing_row = Adw.ComboRow(title="Editing", model=model)
        slugs = [i.slug for i in identities]
        # The bug this replaces: taking the selection from the Fill tab's
        # dropdown, so that choosing another identity here rebuilt the page
        # and immediately reverted it. Nothing but the default could ever be
        # edited or deleted.
        if self.editing_slug not in slugs:
            fallback = self.current_identity()
            self.editing_slug = (fallback.slug if fallback and fallback.slug in slugs
                                 else slugs[0])
        self.editing_row.set_selected(slugs.index(self.editing_slug))

        def switched(row, _param) -> None:
            index = row.get_selected()
            if 0 <= index < len(slugs) and slugs[index] != self.editing_slug:
                self.editing_slug = slugs[index]
                self.identities_page.set_child(self._build_identities_page())

        self.editing_row.connect("notify::selected", switched)
        chooser.add(self.editing_row)

        editing = next(i for i in identities if i.slug == self.editing_slug)
        is_default = editing.slug == default

        buttons = Gtk.Box(spacing=8, margin_top=4, halign=Gtk.Align.START)
        for label, handler in (("Add", self._add_identity),
                               ("Rename", self._rename_identity),
                               ("Default" if is_default else "Make default",
                                self._make_default),
                               ("Delete", self._delete_identity)):
            button = Gtk.Button(label=label)
            if label == "Delete":
                button.add_css_class("destructive-action")
                button.set_sensitive(len(identities) > 1)
            elif label == "Default":
                # Already the one used when no identity is named.
                button.set_sensitive(False)
                button.add_css_class("omaform-locked")
            button.connect("clicked", handler)
            buttons.append(button)
        row = Adw.ActionRow()
        row.set_child(buttons)
        chooser.add(row)

        # What an identity is decides which tax number it files under, which is
        # the one either/or that comes up on nearly every form. Asked once here
        # rather than on every W-9.
        kinds = list(KINDS)
        kind_row = Adw.ComboRow(
            title="This is a",
            subtitle="A business files under an EIN; a person under a Social "
                     "Security number",
            model=Gtk.StringList.new([k.title() for k in kinds]))
        kind_row.set_selected(kinds.index(editing.kind)
                              if editing.kind in kinds else 0)
        kind_row.connect("notify::selected", self._kind_changed, editing.slug, kinds)
        chooser.add(kind_row)

        # The W-9's line 3a. Asked here once rather than guessed on every form.
        # The answers forms ask as checkboxes: tax classification on a W-9,
        # filing status on a W-4, citizenship on an I-9. Asked here once.
        stored_secret = set(self.vault.keys_for(editing.slug)) if self.vault.exists else set()
        for key, options in CHOICES.items():
            spec = BY_KEY[key]
            codes = list(options)
            names = ["Not set"] + [options[c][0] for c in codes]
            current = ""
            locked = False
            if spec.sensitive:
                if key in stored_secret:
                    if self.vault.unlocked:
                        current = self.vault.get(editing.slug, key) or ""
                    else:
                        locked = True
                        names.append("Stored (unlock to change)")
            else:
                current = editing.values.get(key, "")
            row = Adw.ComboRow(title=spec.title,
                               subtitle={"tax_classification": "Which box gets ticked on a W-9",
                                         "filing_status": "Step 1(c) on a W-4",
                                         "citizenship": "Section 1 of an I-9, kept in the vault"}
                               .get(key, ""), model=Gtk.StringList.new(names))
            if locked:
                row.set_selected(len(names) - 1)
            else:
                row.set_selected(codes.index(current) + 1 if current in codes else 0)
            row.connect("notify::selected", self._choice_changed, editing.slug, key, codes)
            chooser.add(row)
        page.add(chooser)

        for title, keys in EDITOR_GROUPS:
            group = Adw.PreferencesGroup(title=title)
            for key in keys:
                spec = BY_KEY[key]
                entry = Adw.EntryRow(title=spec.title,
                                     text=editing.values.get(key, ""))
                entry.connect("notify::text", self._value_edited, editing.slug, key)
                group.add(entry)
            page.add(group)

        secrets_group = Adw.PreferencesGroup(
            title="In the vault",
            description="Encrypted, and asked for only when a form needs one.")
        stored = set(self.vault.keys_for(editing.slug)) if self.vault.exists else set()
        secrets_group.add(self._signature_row(editing, "signature" in stored))
        for spec in SCHEMA:
            if not spec.sensitive or spec.image or spec.choice:
                continue
            row = Adw.ActionRow(
                title=spec.title,
                subtitle="stored" if spec.key in stored else "not set")
            if spec.key in stored:
                row.add_css_class("omaform-locked")
                row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
            button = Gtk.Button(label="Change" if spec.key in stored else "Set",
                                valign=Gtk.Align.CENTER)
            button.connect("clicked", self._set_secret, editing, spec.key)
            row.add_suffix(button)
            if spec.key in stored:
                clear = Gtk.Button(icon_name="edit-delete-symbolic",
                                   valign=Gtk.Align.CENTER,
                                   tooltip_text=f"Remove {spec.title}")
                clear.connect("clicked", self._clear_secret, editing, spec.key)
                row.add_suffix(clear)
            secrets_group.add(row)
        page.add(secrets_group)
        return page

    def _choice_changed(self, row, _param, slug: str, key: str, codes: list[str]) -> None:
        index = row.get_selected()
        if index > len(codes):
            return  # the "stored, locked" placeholder: nothing chosen
        value = codes[index - 1] if 1 <= index <= len(codes) else ""
        self._store_choice(slug, key, value)

    def _store_choice(self, slug: str, key: str, value: str) -> None:
        """Keep a choice where the key lives: the vault for a legal status."""
        spec = BY_KEY[key]
        if spec.sensitive:
            identity = self.library.get(slug)
            if identity is None:
                return

            def write() -> None:
                self.vault.set(slug, key, value)
                if self.document:
                    self.rebuild_plan()

            if not self.vault.exists:
                self._create_vault(write)
            else:
                self._with_vault(write)
            return
        try:
            self.library.set_value(slug, key, value)
        except KeyError as exc:
            self.toast(str(exc))
            return
        if self.document:
            self.rebuild_plan()

    def _kind_changed(self, row, _param, slug: str, kinds: list[str]) -> None:
        index = row.get_selected()
        if not (0 <= index < len(kinds)):
            return
        changed = self.library.set_kind(slug, kinds[index])
        if changed is None:
            return
        tax = ", ".join(BY_KEY[k].title for k in sorted(changed.preference()))
        self.toast(f"{changed.label} is a {changed.kind}"
                   + (f", filing under {tax}" if tax else ""))
        if self.document:
            self.rebuild_plan()

    def _value_edited(self, entry, _param, slug: str, key: str) -> None:
        # Debounced, so a saved file is not rewritten on every keystroke.
        if getattr(entry, "_pending", 0):
            GLib.source_remove(entry._pending)

        def save() -> bool:
            entry._pending = 0
            try:
                self.library.set_value(slug, key, entry.get_text().strip())
            except (PermissionError, KeyError) as exc:
                self.toast(str(exc))
            return False

        entry._pending = GLib.timeout_add(400, save)

    def _add_identity(self, *_args) -> None:
        dialog = Adw.AlertDialog(
            heading="New identity",
            body="Give it a name you will recognise in the dropdown, such as "
                 "your own or your company's.")
        entry = Adw.EntryRow(title="Name")
        kinds = list(KINDS)
        kind_row = Adw.ComboRow(
            title="This is a",
            subtitle="A business files under an EIN; a person under a Social "
                     "Security number",
            model=Gtk.StringList.new([k.title() for k in kinds]))
        group = Adw.PreferencesGroup()
        group.add(entry)
        group.add(kind_row)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("add", "Add")
        dialog.set_response_appearance("add", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("add")

        def responded(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) != "add":
                    return
            except GLib.Error:
                return
            label = entry.get_text().strip()
            if not label:
                return
            index = kind_row.get_selected()
            kind = kinds[index] if 0 <= index < len(kinds) else "person"
            created = self.library.create(label, kind=kind)
            self.editing_slug = created.slug
            self.identities_page.set_child(self._build_identities_page())
            self.toast(f"Added {created.label}, a {created.kind}")

        self._choose(dialog, responded)

    def _rename_identity(self, *_args) -> None:
        identity = self._editing()
        if not identity:
            return
        dialog = Adw.AlertDialog(heading=f"Rename {identity.label}", body="")
        entry = Adw.EntryRow(title="Name", text=identity.label)
        group = Adw.PreferencesGroup()
        group.add(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)

        def responded(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) != "rename":
                    return
            except GLib.Error:
                return
            self.library.rename(identity.slug, entry.get_text())
            self.identities_page.set_child(self._build_identities_page())

        self._choose(dialog, responded)

    def _make_default(self, *_args) -> None:
        identity = self._editing()
        if identity:
            self.library.set_default(identity.slug)
            self.identities_page.set_child(self._build_identities_page())
            self.toast(f"{identity.label} is now the default")

    def _delete_identity(self, *_args) -> None:
        identity = self._editing()
        if not identity:
            return
        dialog = Adw.AlertDialog(
            heading=f"Delete {identity.label}?",
            body="Its details go, and so does anything it had in the vault. "
                 "This cannot be undone.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")

        def responded(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) != "delete":
                    return
            except GLib.Error:
                return
            self.library.delete(identity.slug)
            self.editing_slug = None
            if self.vault.exists and self.vault.keys_for(identity.slug):
                if self.vault.unlocked:
                    self.vault.forget_identity(identity.slug)
                else:
                    self.toast(f"{identity.label}'s secrets are still in the vault; "
                               f"unlock it to remove them")
            self.identities_page.set_child(self._build_identities_page())

        self._choose(dialog, responded)

    def _editing(self) -> Identity | None:
        return self.library.get(self.editing_slug) if self.editing_slug else None

    # -- secrets ---------------------------------------------------------

    def _signature_row(self, identity: Identity, stored: bool) -> Gtk.Widget:
        """Signature: drawn rather than typed, previewed on white when open."""
        row = Adw.ActionRow(title="Signature")
        if stored and self.vault.unlocked:
            png = signing.decode(self.vault.get(identity.slug, "signature") or "")
            if png:
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
                picture = Gtk.Picture.new_for_paintable(texture)
                picture.set_content_fit(Gtk.ContentFit.CONTAIN)
                picture.set_size_request(160, 52)
                frame = Gtk.Box(valign=Gtk.Align.CENTER)
                frame.add_css_class("omaform-signature-preview")
                frame.append(picture)
                row.add_suffix(frame)
            row.set_subtitle("stored")
        elif stored:
            row.set_subtitle("stored; unlock to see it")
            row.add_css_class("omaform-locked")
            row.add_prefix(Gtk.Image(icon_name="channel-secure-symbolic"))
            show = Gtk.Button(label="Show", valign=Gtk.Align.CENTER)
            show.connect("clicked", lambda *_: self._with_vault(
                lambda: self.identities_page.set_child(self._build_identities_page())))
            row.add_suffix(show)
        else:
            row.set_subtitle("not drawn yet")
        draw = Gtk.Button(label="Redraw" if stored else "Draw", valign=Gtk.Align.CENTER)
        draw.connect("clicked", lambda *_: self._draw_signature(identity))
        row.add_suffix(draw)
        if stored:
            clear = Gtk.Button(icon_name="edit-delete-symbolic", valign=Gtk.Align.CENTER,
                               tooltip_text="Remove signature")
            clear.connect("clicked", self._clear_secret, identity, "signature")
            row.add_suffix(clear)
        return row

    def _draw_signature(self, identity: Identity) -> None:
        def saved(png: bytes) -> None:
            value = signing.encode(png)
            store = lambda: self._store_secret(identity, "signature", value)  # noqa: E731
            if not self.vault.exists:
                self._create_vault(store)
            else:
                self._with_vault(store)

        SignatureDialog(identity.label, saved).present(self)

    def _add_missing(self, _button, key: str) -> None:
        """Fill a gap the form just pointed out, without leaving the form."""
        identity = self.current_identity()
        if identity is None:
            return
        spec = BY_KEY[key]
        if spec.image:
            self._draw_signature(identity)
            return
        if spec.choice:
            options = CHOICES[key]
            codes = list(options)
            dialog = Adw.AlertDialog(heading=spec.title,
                                     body="Which box gets ticked on this form."
                                     + (" Kept in the vault." if spec.sensitive else ""))
            combo = Adw.ComboRow(title="This identity is",
                                 model=Gtk.StringList.new([options[c][0] for c in codes]))
            group = Adw.PreferencesGroup()
            group.add(combo)
            dialog.set_extra_child(group)
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("save", "Save")
            dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)

            def chose(dlg, result) -> None:
                try:
                    if dlg.choose_finish(result) != "save":
                        return
                except GLib.Error:
                    return
                self._store_choice(identity.slug, key, codes[combo.get_selected()])

            self._choose(dialog, chose)
            return
        if spec.sensitive:
            self._set_secret(None, identity, key)
            return

        dialog = Adw.AlertDialog(heading=f"Add {spec.title}",
                                 body=f"Kept with {identity.label}'s details.")
        entry = Adw.EntryRow(title=spec.title)
        group = Adw.PreferencesGroup()
        group.add(entry)
        dialog.set_extra_child(group)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        def responded(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) != "save":
                    return
            except GLib.Error:
                return
            value = entry.get_text().strip()
            if not value:
                return
            try:
                self.library.set_value(identity.slug, key, value)
            except (PermissionError, KeyError) as exc:
                self.toast(str(exc))
                return
            self.toast(f"{spec.title} added to {identity.label}")
            self.rebuild_plan()

        self._choose(dialog, responded)

    def _set_secret(self, _button, identity: Identity, key: str) -> None:
        spec = BY_KEY[key]

        def store(value: str) -> None:
            if not self.vault.exists:
                self._create_vault(lambda: self._store_secret(identity, key, value))
                return
            self._with_vault(lambda: self._store_secret(identity, key, value))

        dialog = ValueDialog(spec.title)

        def responded(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) != "save":
                    return
            except GLib.Error:
                return
            value = dialog.entry.get_text().strip()
            if value:
                store(value)

        self._choose(dialog, responded)

    def _store_secret(self, identity: Identity, key: str, value: str) -> None:
        try:
            self.vault.set(identity.slug, key, value)
        except vaulting.VaultError as exc:
            self.toast(str(exc))
            return
        self.identities_page.set_child(self._build_identities_page())
        self.toast(f"{BY_KEY[key].title} stored for {identity.label}")
        # A secret added because the open form asked for it should land in that
        # form straight away, not on the next time round.
        if self.document:
            self.rebuild_plan()

    def _clear_secret(self, _button, identity: Identity, key: str) -> None:
        def go() -> None:
            self.vault.unset(identity.slug, key)
            self.identities_page.set_child(self._build_identities_page())
            self.toast(f"{BY_KEY[key].title} removed")

        self._with_vault(go)

    def _with_vault(self, then) -> None:
        """Run `then` with the vault open, asking for the passphrase if needed."""
        if self.vault.unlocked:
            then()
            return
        stored = vaulting.keyring_lookup()
        if stored:
            try:
                self.vault.unlock(stored)
                then()
                return
            except vaulting.VaultError:
                pass

        dialog = PassphraseDialog("Your vault holds the details a form asks for "
                                  "but you would rather not leave lying around.")

        def responded(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) != "unlock":
                    return
            except GLib.Error:
                return
            try:
                self.vault.unlock(dialog.entry.get_text())
            except vaulting.VaultError as exc:
                self.toast(str(exc))
                return
            then()

        self._choose(dialog, responded)

    def _create_vault(self, then) -> None:
        dialog = Adw.AlertDialog(
            heading="Choose a vault passphrase",
            body="It encrypts the details you would not want in a backup. "
                 "There is no way to recover it, so pick something you will "
                 "remember.")
        # placeholder-text is a construct property on Gtk.PasswordEntry; unlike
        # Gtk.Entry it has no set_placeholder_text() method. Calling one here
        # raised AttributeError inside a dialog callback, which GLib prints to
        # stderr and otherwise swallows, so the dialog never appeared and a
        # typed Social Security number went nowhere with no visible error.
        first = Gtk.PasswordEntry(show_peek_icon=True, margin_top=8,
                                  placeholder_text="Passphrase",
                                  activates_default=True)
        second = Gtk.PasswordEntry(show_peek_icon=True, margin_top=4,
                                   placeholder_text="Again",
                                   activates_default=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.append(first)
        box.append(second)
        dialog.set_extra_child(box)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("create")
        dialog.set_close_response("cancel")

        def responded(dlg, result) -> None:
            try:
                if dlg.choose_finish(result) != "create":
                    return
            except GLib.Error:
                return
            if first.get_text() != second.get_text():
                self.toast("Those did not match")
                return
            if not first.get_text():
                self.toast("An empty passphrase protects nothing")
                return
            try:
                self.vault.create(first.get_text())
            except vaulting.VaultError as exc:
                self.toast(str(exc))
                return
            then()

        self._choose(dialog, responded)


class Application(Adw.Application):
    def __init__(self) -> None:
        flags = Gio.ApplicationFlags.HANDLES_OPEN
        if os.environ.get("OMAFORM_HOME"):
            # A separate data folder is a separate instance. Otherwise the
            # running window takes the file and opens it with its own data,
            # which is how real details once ended up in a scratch session.
            flags |= Gio.ApplicationFlags.NON_UNIQUE
        super().__init__(application_id=APP_ID, flags=flags)
        self.theme = Theme()
        # `omaform-ui --setup` opens setup, in the running window if there is one:
        # what the Omarchy bar widget's Setup button runs.
        self.add_main_option("setup", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Open setup: your details, the vault, your Omarchy agent", None)
        setup = Gio.SimpleAction.new("setup", None)
        setup.connect("activate", lambda *_: self._setup())
        self.add_action(setup)

    def do_handle_local_options(self, options) -> int:
        if options.contains("setup"):
            self.register(None)
            self.activate_action("setup", None)
            return 0
        return -1

    def _setup(self) -> None:
        window = self.get_active_window()
        if window is None:
            window = Window(self)
        window.present()
        window.show_onboarding()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gtk.Widget.get_default_direction  # touch gi to keep linters calm
        del display
        from gi.repository import Gdk
        self.theme.attach(Gdk.Display.get_default())

    def do_activate(self) -> None:
        Window(self).present()

    def do_open(self, files, _count, _hint) -> None:
        path = files[0].get_path() if files else None
        Window(self, path).present()


def main(argv: list[str] | None = None) -> int:
    import sys
    return Application().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
