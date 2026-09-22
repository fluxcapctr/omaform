"""Capture the screenshots the video is made from, from the real app.

Runs Omaform against a throwaway data folder filled with made-up details
(Alex Rivera, Rivera Design Co), drives the window through each feature, and
has GTK render the window to PNG at twice its size, so zooms stay sharp. Also
writes targets.json: where the interesting things are in each shot, so the
video's zooms land on them. Nothing here touches real data.

    ./.venv/bin/python video/capture.py
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path(tempfile.mkdtemp(prefix="omaform-demo-"))
os.environ["OMAFORM_HOME"] = str(HOME)   # also makes the app its own instance

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gio, GLib, Graphene, Gtk  # noqa: E402

import cairo  # noqa: E402

from omaform import signature as signing  # noqa: E402
from omaform import vault as vaulting  # noqa: E402
from omaform.profile import Library  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "video" / "public" / "shots"
FIX = REPO / "tests" / "fixtures"
WORK = HOME / "forms"
PASS = "demo passphrase"
W, H = 1600, 960
SCALE = 2

targets: dict[str, dict] = {}


# -- made-up data -------------------------------------------------------------

def signature_strokes():
    """A flowing made-up signature: two words of loops, each with a capital."""
    strokes = []
    for word, (x0, length, loops) in enumerate(((10, 150, 6), (190, 190, 7))):
        cap = [(x0 + 4, 70), (x0 + 16, 12), (x0 + 26, 64)]
        strokes.append([(x + (5 if i else 0), y) for i, (x, y) in enumerate(cap)])
        pts = []
        steps = 90
        for i in range(steps + 1):
            t = i / steps * loops * 2 * math.pi
            x = x0 + 22 + i / steps * length + 9 * math.cos(t + math.pi)
            y = 52 - 14 * math.sin(t) * (0.6 + 0.4 * math.sin(i / 11 + word))
            pts.append((x, y))
        strokes.append(pts)
    strokes.append([(20, 84), (160, 78), (360, 82)])   # the underline flourish
    return strokes


def setup() -> None:
    lib = Library(HOME)
    lib.create("Alex Rivera", {
        "full_name": "Alex Rivera", "first_name": "Alex", "last_name": "Rivera",
        "middle_initial": "J", "address1": "1200 Maple Avenue", "address2": "Apt B",
        "city": "Springfield", "state": "OR", "zip": "97403",
        "email": "alex.rivera@example.com", "phone": "(555) 010-2030",
        "filing_status": "single"})
    lib.create("Rivera Design Co", {
        "full_name": "Alex Rivera", "business_name": "Rivera Design Co",
        "address1": "40 Market Street", "address2": "Suite 3", "city": "Springfield",
        "state": "OR", "zip": "97403", "email": "hello@riveradesign.example",
        "phone": "(555) 010-4455", "tax_classification": "s_corp"}, kind="business")
    lib.create("Sam Rivera", {"full_name": "Sam Rivera", "first_name": "Sam",
                              "last_name": "Rivera", "address1": "1200 Maple Avenue",
                              "address2": "Apt B", "city": "Springfield", "state": "OR",
                              "zip": "97403", "filing_status": "married_jointly"})
    png = signing.render_png(signature_strokes())
    from omaform.ui.app import vault_path
    kdf = vaulting.Kdf(salt=os.urandom(16), time_cost=1, memory_cost=1024, parallelism=1)
    vaulting.Vault(vault_path()).create(PASS, {
        "alex-rivera": {"ssn": "123-45-6789", "dob": "04/12/1990",
                        "citizenship": "citizen", "signature": signing.encode(png)},
        "rivera-design-co": {"ein": "12-3456789", "signature": signing.encode(png)},
    }, kdf=kdf)
    WORK.mkdir(parents=True, exist_ok=True)
    for name in ("fw9.pdf", "fw4.pdf", "i-9.pdf"):
        shutil.copy(FIX / name, WORK / name)
    rental_form(WORK / "rental-application.pdf")
    sys.path.insert(0, str(REPO / "tests"))
    from conftest import make_docx
    make_docx(WORK / "volunteer-form.docx")


def rental_form(path: Path) -> None:
    """A flat PDF: printed lines, no form fields, like most forms people email."""
    s = cairo.PDFSurface(str(path), 612, 792)
    c = cairo.Context(s)
    c.select_font_face("Helvetica", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
    c.set_font_size(20)
    c.move_to(72, 90)
    c.show_text("Maple Court Apartments")
    c.set_font_size(13)
    c.move_to(72, 112)
    c.show_text("Rental Application")
    c.select_font_face("Helvetica", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
    c.set_font_size(11)
    y = 170
    for label in ("Full name", "Current address", "City, state, ZIP", "Phone",
                  "Email", "Employer", "Monthly income"):
        c.move_to(72, y)
        c.show_text(label)
        c.move_to(190, y + 2)
        c.line_to(540, y + 2)
        c.set_line_width(0.8)
        c.stroke()
        y += 40
    y += 20
    c.move_to(72, y)
    c.show_text("I certify the information above is true and complete.")
    y += 50
    c.move_to(72, y)
    c.show_text("Signature")
    c.move_to(140, y + 2)
    c.line_to(360, y + 2)
    c.stroke()
    c.move_to(380, y)
    c.show_text("Date")
    c.move_to(412, y + 2)
    c.line_to(540, y + 2)
    c.stroke()
    s.finish()


# -- capture --------------------------------------------------------------------

def shot(win, name: str, extra: dict | None = None) -> None:
    w, h = win.get_width(), win.get_height()
    paintable = Gtk.WidgetPaintable.new(win)
    snap = Gtk.Snapshot()
    snap.scale(SCALE, SCALE)
    paintable.snapshot(snap, w, h)
    node = snap.to_node()
    rect = Graphene.Rect().init(0, 0, w * SCALE, h * SCALE)
    texture = win.get_renderer().render_texture(node, rect)
    texture.save_to_png(str(OUT / f"{name}.png"))
    data = {"width": w, "height": h}
    data.update(extra or {})
    targets[name] = data
    print("shot", name, w, h, flush=True)


def bounds(win, widget) -> list[float] | None:
    ok, r = widget.compute_bounds(win)
    if not ok:
        return None
    return [r.origin.x, r.origin.y, r.size.width, r.size.height]


def blank_box(win, blank_id: str) -> list[float] | None:
    """Where a blank is in the window, in window pixels."""
    preview = win.preview
    blank = next((b for b in preview.blanks if b.id == blank_id), None)
    if blank is None or not preview._layout:
        return None
    ok, origin = preview.area.compute_point(win, Graphene.Point().init(0, 0))
    placed = preview._layout[blank.page - 1]
    x0, y0, x1, y1 = preview._hit_rect(blank)
    left = origin.x + 8 + x0 * placed.scale
    top = origin.y + placed.top + (placed.pdf_h - y1) * placed.scale
    return [left, top, (x1 - x0) * placed.scale, (y1 - y0) * placed.scale]


def find(win, pred):
    return next((e for e in win.plan.entries if pred(e)), None)


def main() -> None:
    setup()
    from omaform.ui import app as ui
    # The demo vault opens the way a saved keyring passphrase would open a real
    # one, so no passphrase dialog sits over the shots.
    ui.vaulting.keyring_lookup = lambda: PASS

    steps = []

    class Harness(ui.Application):
        # The real application class, so the real theme is applied on startup:
        # Omarchy colours, JetBrains Mono, square corners.
        def do_activate(self):
            self.win = ui.Window(self, str(WORK / "fw9.pdf"))
            self.win.set_default_size(W, H)
            self.win.present()
            GLib.timeout_add(900, self.place)

        def place(self):
            pid = os.getpid()
            for cmd in (f"setfloating pid:{pid}",
                        f"resizewindowpixel exact {W} {H},pid:{pid}",
                        f"movewindowpixel exact 120 120,pid:{pid}"):
                subprocess.run(["hyprctl", "dispatch", *cmd.split(" ", 1)],
                               capture_output=True)
            GLib.timeout_add(1200, self.next)
            return False

        def next(self):
            if not steps:
                (OUT / "targets.json").write_text(json.dumps(targets, indent=2))
                self.quit()
                return False
            delay = steps.pop(0)(self.win)
            GLib.timeout_add(delay or 1200, self.next)
            return False

    def unlock(win):
        if not win.vault.unlocked:
            win.vault.unlock(PASS)
        win.rebuild_plan()
        return 3000

    def w9(win):
        win.preview.scroller.get_vadjustment().set_value(0)
        name = find(win, lambda e: e.match and e.match.key == "full_name")
        addr = find(win, lambda e: e.match and e.match.key == "address1")
        sig = find(win, lambda e: e.image is not None)
        tick = find(win, lambda e: e.value == "checked")
        ssn = [e for e in win.plan.entries if e.match and e.match.key == "ssn" and e.filled]
        shot(win, "w9_fill", {
            "preview": bounds(win, win.preview), "list": bounds(win, win.fill_scroller),
            "name": blank_box(win, name.blank.id) if name else None,
            "address": blank_box(win, addr.blank.id) if addr else None,
            "signature": blank_box(win, sig.blank.id) if sig else None,
            "tick": blank_box(win, tick.blank.id) if tick else None,
            "ssn": [blank_box(win, e.blank.id) for e in ssn]})
        win.preview.highlight(name.blank.id)
        return 600

    def hover(win):
        shot(win, "w9_hover")
        win.preview.highlight(None)
        return 300

    def vault(win):
        win.preview.scroller.get_vadjustment().set_value(0)
        dialog = ui.PassphraseDialog("This form needs your Social Security number and "
                                     "signature for Alex Rivera.")
        dialog.entry.set_text("correct horse battery staple")
        dialog.present(win)
        win._vault_dialog = dialog
        return 900

    def vault_shot(win):
        shot(win, "vault", {"dialog": bounds(win, win._vault_dialog)})
        win._vault_dialog.force_close()
        return 500

    def busy(win):
        import time
        win.model_busy = ("Omarchy's default agent (claude)", time.monotonic() - 7)
        win._render_fill()
        return 900

    def busy_shot(win):
        shot(win, "model_busy", {"list": bounds(win, win.fill_scroller)})
        win.model_busy = None
        win._render_fill()
        return 500

    def ask(win):
        win._ask_model()
        return 900

    def ask_shot(win):
        shot(win, "ask_model")
        dialog = win.get_visible_dialog()
        if dialog:
            dialog.force_close()
        return 600

    def save(win):
        win.do_fill(WORK / "fw9-filled.pdf")
        return 1500

    def saved_shot(win):
        adj = win.fill_scroller.get_vadjustment()
        adj.set_value(adj.get_upper())
        return 600

    def saved_shot2(win):
        shot(win, "saved", {"list": bounds(win, win.fill_scroller)})
        win.toasts.dismiss_all()
        return 300

    def scroll_to(win, entry):
        preview = win.preview
        placed = preview._layout[entry.blank.page - 1]
        top = placed.top + (placed.pdf_h - entry.blank.rect[3]) * placed.scale
        preview.scroller.get_vadjustment().set_value(max(0, top - 380))

    def w9_sign(win):
        sig = find(win, lambda e: e.image is not None)
        scroll_to(win, sig)
        return 800

    def w9_sign_shot(win):
        sig = find(win, lambda e: e.image is not None)
        date = find(win, lambda e: e.match and e.match.key == "date_today" and e.filled)
        ssn = [e for e in win.plan.entries if e.match and e.match.key == "ssn" and e.filled]
        shot(win, "w9_sign", {"signature": blank_box(win, sig.blank.id),
                              "ssn": [blank_box(win, e.blank.id) for e in ssn],
                              "list": bounds(win, win.fill_scroller),
                              "date": blank_box(win, date.blank.id) if date else None,
                              "preview": bounds(win, win.preview)})
        return 300

    def identities(win):
        win.stack.set_visible_child_name("identities")
        return 1200

    def identities_shot(win):
        shot(win, "identities")
        return 300

    def pages(win):
        win.stack.set_visible_child_name("pages")
        win.pages_tab.load(str(WORK / "fw9-filled.pdf"))
        win.pages_tab.append_file(str(WORK / "i-9.pdf"))
        win.pages_tab.size.set_value(260)
        return 1500

    def pages_shot(win):
        shot(win, "pages")
        win.stack.set_visible_child_name("fill")
        return 300

    def i9(win):
        win.load(str(WORK / "i-9.pdf"))
        return 3500

    def i9_shot(win):
        win.preview.scroller.get_vadjustment().set_value(0)
        ssn = [e for e in win.plan.entries if e.match and e.match.key == "ssn" and e.filled]
        tick = find(win, lambda e: e.value == "checked")
        shot(win, "i9_fill", {
            "preview": bounds(win, win.preview), "list": bounds(win, win.fill_scroller),
            "ssn": [blank_box(win, e.blank.id) for e in ssn],
            "tick": blank_box(win, tick.blank.id) if tick else None})
        return 300

    def blackout(win):
        ssn = [e for e in win.plan.entries if e.match and e.match.key == "ssn" and e.filled]
        if ssn:
            rects = [e.blank.rect for e in ssn]
            x0 = min(r[0] for r in rects) - 3
            y0 = min(r[1] for r in rects) - 3
            x1 = max(r[2] for r in rects) + 3
            y1 = max(r[3] for r in rects) + 3
            win.preview.set_blackout(True)
            win.preview.on_redact(ssn[0].blank.page, (x0, y0, x1, y1))
        return 900

    def blackout_shot(win):
        from omaform import redact  # noqa: F401
        region = win.redactions[0] if win.redactions else None
        box = None
        if region:
            preview = win.preview
            ok, origin = preview.area.compute_point(win, Graphene.Point().init(0, 0))
            placed = preview._layout[region.page - 1]
            x0, y0, x1, y1 = region.rect
            box = [origin.x + 8 + x0 * placed.scale,
                   origin.y + placed.top + (placed.pdf_h - y1) * placed.scale,
                   (x1 - x0) * placed.scale, (y1 - y0) * placed.scale]
        shot(win, "blackout", {"box": box, "preview": bounds(win, win.preview)})
        win.preview.set_blackout(False)
        return 300

    def flat(win):
        win.load(str(WORK / "rental-application.pdf"))
        return 3000

    def flat_shot(win):
        name = find(win, lambda e: e.match and e.match.key == "full_name")
        sig = find(win, lambda e: e.image is not None)
        shot(win, "flat", {"preview": bounds(win, win.preview),
                           "name": blank_box(win, name.blank.id) if name else None,
                           "signature": blank_box(win, sig.blank.id) if sig else None})
        return 300

    def sigdialog(win):
        from omaform.ui.signature_dialog import SignatureDialog
        d = SignatureDialog("Alex Rivera", lambda png: None)
        s = signature_strokes()
        d.strokes = [[(x * 1.1 + 20, y * 1.1 + 40) for x, y in st] for st in s]
        d.present(win)
        d.canvas.queue_draw()
        win._sig_dialog = d
        return 1200

    def sig_shot(win):
        shot(win, "signature_dialog")
        win._sig_dialog.force_close()
        return 400

    def docx(win):
        win.load(str(WORK / "volunteer-form.docx"))
        return 12000

    def docx_shot(win):
        shot(win, "docx", {"preview": bounds(win, win.preview),
                           "list": bounds(win, win.fill_scroller)})
        return 300

    def flat_sign(win):
        sig = find(win, lambda e: e.image is not None)
        if sig:
            scroll_to(win, sig)
        return 800

    def flat_sign_shot(win):
        sig = find(win, lambda e: e.image is not None)
        shot(win, "flat_sign", {"signature": blank_box(win, sig.blank.id) if sig else None,
                                "preview": bounds(win, win.preview)})
        return 300

    steps.extend([unlock, w9, hover, w9_sign, w9_sign_shot, vault, vault_shot, busy, busy_shot, ask, ask_shot,
                  save, saved_shot, saved_shot2, identities, identities_shot, pages, pages_shot, i9, i9_shot,
                  blackout, blackout_shot, flat, flat_shot, flat_sign, flat_sign_shot, sigdialog, sig_shot, docx,
                  docx_shot])

    harness = Harness()
    harness.run([])
    shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    main()
