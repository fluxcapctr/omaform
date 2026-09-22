"""Draw a signature with the mouse, trackpad, pen or finger."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import signature  # noqa: E402

CANVAS_W, CANVAS_H = 620, 220


class SignatureDialog(Adw.Dialog):
    """A white pad to sign on. Calls `on_save(png_bytes)` when saved."""

    def __init__(self, identity_label: str, on_save) -> None:
        super().__init__(title="Draw your signature", content_width=CANVAS_W + 48)
        self.on_save = on_save
        self.strokes: list[signature.Stroke] = []

        header = Adw.HeaderBar(show_end_title_buttons=False,
                               show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        header.pack_start(cancel)
        self.save = Gtk.Button(label="Save", sensitive=False)
        self.save.add_css_class("suggested-action")
        self.save.connect("clicked", self._save)
        header.pack_end(self.save)

        hint = Gtk.Label(
            label=f"Sign as {identity_label}. Stored encrypted with the rest of "
                  f"the vault; placed on forms that ask for it.",
            wrap=True, xalign=0, margin_start=16, margin_end=16, margin_top=12)
        hint.add_css_class("dim-label")

        self.canvas = Gtk.DrawingArea(content_width=CANVAS_W, content_height=CANVAS_H,
                                      margin_start=16, margin_end=16, margin_top=10,
                                      margin_bottom=6)
        self.canvas.add_css_class("omaform-signature-pad")
        self.canvas.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        self.canvas.add_controller(drag)

        clear = Gtk.Button(label="Clear", halign=Gtk.Align.START,
                           margin_start=16, margin_bottom=14)
        clear.connect("clicked", self._clear)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        body.append(hint)
        body.append(self.canvas)
        body.append(clear)
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.set_content(body)
        self.set_child(view)

    # -- drawing ---------------------------------------------------------

    def _begin(self, gesture, x, y) -> None:
        self.strokes.append([(x, y)])
        self.save.set_sensitive(True)
        self.canvas.queue_draw()

    def _update(self, gesture, dx, dy) -> None:
        ok, sx, sy = gesture.get_start_point()
        if ok and self.strokes:
            self.strokes[-1].append((sx + dx, sy + dy))
            self.canvas.queue_draw()

    def _draw(self, area, ctx, w, h) -> None:
        # Paper. The pad is white regardless of theme, because that is what a
        # form is, and a signature judged against a dark canvas lies about
        # its weight.
        ctx.set_source_rgb(1, 1, 1)
        ctx.paint()
        ctx.set_source_rgb(0.80, 0.80, 0.80)
        ctx.set_line_width(1)
        ctx.move_to(24, h - 48)
        ctx.line_to(w - 24, h - 48)
        ctx.stroke()
        signature.draw_strokes(ctx, self.strokes)

    def _clear(self, *_args) -> None:
        self.strokes = []
        self.save.set_sensitive(False)
        self.canvas.queue_draw()

    def _save(self, *_args) -> None:
        png = signature.render_png(self.strokes)
        if png:
            self.on_save(png)
        self.close()
