"""First run: who you are, where private data goes, and your Omarchy agent.

Five short pages in one dialog. Shown the first time the window opens with
nothing stored, and again from the Setup button whenever you like. Every
page can be skipped; nothing here is needed to fill a form, only to fill
more of it.
"""

from __future__ import annotations

import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import llm  # noqa: E402
from .. import vault as vaulting  # noqa: E402

ADDRESS_FIELDS = [("full_name", "Full name"), ("address1", "Street address"),
                  ("address2", "Apartment or suite"), ("city", "City"), ("state", "State"),
                  ("zip", "ZIP code"), ("email", "Email"), ("phone", "Phone")]

BACKENDS = [("agent", "Your Omarchy agent"), ("ollama", "A local model (Ollama)"),
            ("off", "Neither, never ask")]


def _page(title: str, body: str, *widgets: Gtk.Widget) -> Adw.NavigationPage:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                  margin_top=18, margin_bottom=18, margin_start=24, margin_end=24)
    heading = Gtk.Label(label=title, xalign=0, wrap=True)
    heading.add_css_class("title-2")
    box.append(heading)
    if body:
        text = Gtk.Label(label=body, xalign=0, wrap=True)
        text.add_css_class("dim-label")
        box.append(text)
    for widget in widgets:
        box.append(widget)
    scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroller.set_child(box)
    view = Adw.ToolbarView()
    view.add_top_bar(Adw.HeaderBar(show_title=False))
    view.set_content(scroller)
    return Adw.NavigationPage(title=title, child=view)


def _buttons(*pairs) -> Gtk.Box:
    row = Gtk.Box(spacing=8, halign=Gtk.Align.END, margin_top=8)
    for label, handler, primary in pairs:
        button = Gtk.Button(label=label)
        if primary:
            button.add_css_class("suggested-action")
        button.connect("clicked", lambda _b, h=handler: h())
        row.append(button)
    return row


class Onboarding(Adw.Dialog):
    def __init__(self, window) -> None:
        super().__init__(title="Set up Omaform", content_width=600, content_height=640)
        self.window = window
        self.library = window.library
        self.nav = Adw.NavigationView()
        self.set_child(self.nav)
        self.nav.add(self._welcome())

    # -- 1 ---------------------------------------------------------------

    def _welcome(self) -> Adw.NavigationPage:
        icon = Gtk.Image.new_from_icon_name("omaform")
        icon.set_pixel_size(96)
        icon.set_halign(Gtk.Align.START)
        return _page(
            "Fill it out once and for all",
            "Store your details once, and every form after that opens already filled. "
            "PDF forms, flat PDFs, scans and Word documents. Everything stays on this "
            "machine.",
            icon,
            _buttons(("Get started", lambda: self.nav.push(self._you()), True)))

    # -- 2 ---------------------------------------------------------------

    def _you(self) -> Adw.NavigationPage:
        identity = self.window.current_identity() or self.library.resolve(None)
        values = identity.values if identity else {}
        group = Adw.PreferencesGroup(
            description="The basics most forms ask for. Add the rest later, on the "
                        "Identities tab, along with a business or family member.")
        self.entries: dict[str, Adw.EntryRow] = {}
        for key, title in ADDRESS_FIELDS:
            row = Adw.EntryRow(title=title, text=values.get(key, ""))
            self.entries[key] = row
            group.add(row)

        def save() -> None:
            name = self.entries["full_name"].get_text().strip()
            if not name:
                self.window.toast("Your name first: every form asks for it")
                return
            new = {k: e.get_text().strip() for k, e in self.entries.items()
                   if e.get_text().strip()}
            target = self.window.current_identity() or self.library.resolve(None)
            if target is None:
                target = self.library.create(name, new)
            else:
                if target.label in ("Me", "") or target.label != name and not target.values:
                    target = self.library.rename(target.slug, name) or target
                target.values.update(new)
                self.library.save(target)
                self.library.set_default(target.slug)
            self.window.filling_slug = target.slug
            self.nav.push(self._private())

        return _page("You", "", group,
                     _buttons(("Skip", lambda: self.nav.push(self._private()), False),
                              ("Continue", save, True)))

    # -- 3 ---------------------------------------------------------------

    def _private(self) -> Adw.NavigationPage:
        vault = self.window.vault
        body = ("Your Social Security number, EIN, date of birth and signature are "
                "kept in an encrypted vault, apart from everything else. Only a "
                "passphrase you choose opens it, and Omaform asks for it only when a "
                "form needs one of them. There is no way to recover a lost passphrase.")
        if vault.exists:
            return _page("Your private data", body + "\n\nYour vault is already set up.",
                         _buttons(("Continue", lambda: self.nav.push(self._agent()), True)))
        group = Adw.PreferencesGroup()
        first = Adw.PasswordEntryRow(title="Passphrase")
        again = Adw.PasswordEntryRow(title="Same again")
        group.add(first)
        group.add(again)
        remember = Adw.SwitchRow(
            title="Remember it in the keyring",
            subtitle="Then the vault opens without asking while you are logged in.",
            active=False, sensitive=vaulting.keyring_available())
        group.add(remember)

        def create() -> None:
            phrase = first.get_text()
            if len(phrase) < 8:
                self.window.toast("At least eight characters")
                return
            if phrase != again.get_text():
                self.window.toast("The two passphrases differ")
                return
            try:
                vault.create(phrase)
            except vaulting.VaultError as exc:
                self.window.toast(str(exc))
                return
            if remember.get_active():
                vaulting.keyring_store(phrase)
            self.window.toast("Vault created")
            self.nav.push(self._agent())

        return _page("Your private data", body, group,
                     _buttons(("Later", lambda: self.nav.push(self._agent()), False),
                              ("Create vault", create, True)))

    # -- 4 ---------------------------------------------------------------

    def _agent(self) -> Adw.NavigationPage:
        group = Adw.PreferencesGroup()
        self.agent_row = Adw.ActionRow()
        self.local_row = Adw.ActionRow()
        group.add(self.agent_row)
        group.add(self.local_row)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
                             tooltip_text="Look again")
        refresh.add_css_class("flat")
        refresh.connect("clicked", lambda *_: self._detect())
        self.agent_row.add_suffix(refresh)

        choice = Adw.PreferencesGroup()
        self.backend_row = Adw.ComboRow(title="When I ask for help, use",
                                        model=Gtk.StringList.new([b[1] for b in BACKENDS]))
        choice.add(self.backend_row)
        self.test_row = Adw.ActionRow(title="Test the connection",
                                      subtitle="Sends one word, nothing about you.")
        test = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        test.connect("clicked", lambda *_: self._test())
        self.test_row.add_suffix(test)
        choice.add(self.test_row)
        self._detect()

        def next_page() -> None:
            backend = BACKENDS[self.backend_row.get_selected()][0]
            self.library.set_setting("llm_backend", backend)
            self.window.llm_backend = backend
            self.nav.push(self._forms_in())

        return _page(
            "Your Omarchy agent",
            "Some forms are confusing: “complete this part only if…”. "
            "For those, Omaform can hand the questions to the agent you already use "
            "with Omarchy, or to a model on this machine. Only when you press Ask, and "
            "it sees the questions and the names of your details, never your answers.",
            group, choice, _buttons(("Continue", next_page, True)))

    def _detect(self) -> None:
        status = llm.agent_status()
        if status["usable"]:
            self.agent_row.set_title(f"✓  {status['label']}")
            self.agent_row.set_subtitle(
                "Your Omarchy agent. " + ("A cloud service: the form's questions go to it."
                                          if status.get("remote") else "Runs as configured."))
        else:
            self.agent_row.set_title("✗  No Omarchy agent Omaform can use")
            self.agent_row.set_subtitle(status["why"])
        local = llm.pick_local_model()
        self.local_row.set_title(f"✓  Local model: {local}" if local
                                 else "·  No local model")
        self.local_row.set_subtitle("Nothing leaves the computer." if local
                                    else "Optional. Install Ollama and pull a model.")
        stored = self.library.setting("llm_backend")
        default = stored or ("agent" if status["usable"] else "ollama" if local else "off")
        keys = [b[0] for b in BACKENDS]
        self.backend_row.set_selected(keys.index(default) if default in keys else 0)

    def _test(self) -> None:
        backend = BACKENDS[self.backend_row.get_selected()][0]
        if backend == "off":
            self.test_row.set_subtitle("Nothing to test: asking is turned off.")
            return
        self.test_row.set_subtitle("Asking…")

        def work() -> None:
            try:
                answer = llm.test_backend(backend)
                text = f"✓ Connected. It said: {answer}"
            except llm.ModelError as exc:
                text = f"✗ {exc}"
            GLib.idle_add(self.test_row.set_subtitle, text)

        threading.Thread(target=work, daemon=True).start()

    # -- 5 ---------------------------------------------------------------

    def _forms_in(self) -> Adw.NavigationPage:
        group = Adw.PreferencesGroup()
        watch = Adw.SwitchRow(
            title="Offer to fill forms that land in Downloads",
            subtitle="One notification with a Fill button, only for files with blanks.",
            active=self._watching())
        default = Adw.SwitchRow(
            title="Open PDFs with Omaform",
            subtitle="Otherwise it is one choice in Open With.",
            active=self._default_pdf())
        group.add(watch)
        group.add(default)

        def finish() -> None:
            self._set_watching(watch.get_active())
            if default.get_active() and not self._default_pdf():
                subprocess.run(["xdg-mime", "default", "omaform.desktop", "application/pdf"],
                               capture_output=True)
            self.library.set_setting("onboarded", True)
            self.window.refresh_identities()
            self.window.identities_page.set_child(self.window._build_identities_page())
            if self.window.document is not None:
                self.window.rebuild_plan()
            self.close()
            self.window.toast("All set. Open a form to fill it.")

        return _page("Forms in", "", group, _buttons(("Finish", finish, True)))

    @staticmethod
    def _watching() -> bool:
        done = subprocess.run(["systemctl", "--user", "is-enabled", "omaform-watch"],
                              capture_output=True, text=True)
        return done.stdout.strip() == "enabled"

    @staticmethod
    def _set_watching(on: bool) -> None:
        if on == Onboarding._watching():
            return
        subprocess.run(["systemctl", "--user", "enable" if on else "disable", "--now",
                        "omaform-watch"], capture_output=True)

    @staticmethod
    def _default_pdf() -> bool:
        done = subprocess.run(["xdg-mime", "query", "default", "application/pdf"],
                              capture_output=True, text=True)
        return done.stdout.strip() == "omaform.desktop"
