"""The form, as it will be saved, with the fill drawn over it.

All pages in one scroll, the way a document reads. Each page is rendered
through poppler once per zoom and cached; every planned value is painted where
the writer will put it: text in its box, a tick in its checkbox, the signature
image at exactly the size the writer uses, because both take it from the same
function.

Every field the form has is outlined, filled or not, so the fields a viewer
would show as highlighted are visible here too. A left-click on any of them
fills it: a checkbox toggles, a text box asks for its text. A right-click
anywhere offers to sign, date, type or tick at that point, for the lines a form
never made a field for. The point clicked becomes the bottom-left of the mark,
so clicking on a printed line writes on that line.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Poppler", "0.18")
from gi.repository import Gdk, GLib, Gtk, Poppler  # noqa: E402

from ..adapters.pdf import fit_signature  # noqa: E402
from ..model import BlankKind  # noqa: E402

INK = (0.05, 0.10, 0.35)              # what typed values look like on the page
HIGHLIGHT = (1.0, 0.85, 0.20, 0.28)   # a wash behind every mark, so it can be found
FIELD = (0.20, 0.45, 0.85, 0.75)      # the outline of an unfilled field
FOCUS = (0.16, 0.80, 0.95)            # the field the pointer is on, in the list or here
MAX_SCALE = 2.2
MARGIN = 8
CLOSE_PX = 16                         # the × on a black-out, in screen pixels
HINT = "click a field to fill it, drag a signature or placed text to move it, right-click to add"
GAP = 14                              # between pages


@dataclass
class Placed:
    """Where one page sits in the scroll, in screen pixels."""

    number: int
    top: float
    width: float
    height: float
    scale: float
    pdf_w: float
    pdf_h: float

    @property
    def bottom(self) -> float:
        return self.top + self.height


class PagePreview(Gtk.Box):
    """Every page, stacked, fitted to the width available."""

    def __init__(self, on_place, on_activate, on_redact=None, on_unredact=None,
                 on_typed=None, on_toggled=None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_place = on_place          # (page_no, BlankKind, x_pdf, y_pdf)
        self.on_activate = on_activate    # (blank, x_pdf, y_pdf)
        self.on_redact = on_redact        # (page_no, rect_pdf)
        self.on_unredact = on_unredact    # (region)
        self.on_typed = on_typed          # (blank, text, final)
        self.on_toggled = on_toggled      # (blank)
        self.on_moved = None              # (blank), after a drag moved it
        self._moving = None               # (blank, offset at pickup, scale)
        # Keyboard filling: the field the keys go to, and what has been typed.
        self.active: str | None = None
        self.buffer = ""
        self.redactions: list = []        # redact.Region, drawn black
        self._drag: tuple[Placed, float, float, float, float] | None = None
        self.document: Poppler.Document | None = None
        self.blanks: list = []
        self.plan = None
        self.page_no = 1                  # the page at the middle of the view
        self.highlighted: str | None = None
        self._images: dict[str, cairo.ImageSurface] = {}
        self._layout: list[Placed] = []
        self._layout_width = 0
        self._rendered: dict[tuple[int, int], cairo.ImageSurface] = {}

        bar = Gtk.Box(spacing=6, margin_start=8, margin_end=8, margin_top=6,
                      margin_bottom=6)
        self.prev = Gtk.Button(label="‹", tooltip_text="Previous page")
        self.next = Gtk.Button(label="›", tooltip_text="Next page")
        self.prev.connect("clicked", lambda *_: self.go_to_page(self.page_no - 1))
        self.next.connect("clicked", lambda *_: self.go_to_page(self.page_no + 1))
        self.label = Gtk.Label(label="", hexpand=True, xalign=0)
        self.label.add_css_class("dim-label")
        self.hint = Gtk.Label(label=HINT)
        self.hint.add_css_class("dim-label")
        self.blackout_on = False
        for widget in (self.prev, self.next, self.label, self.hint):
            bar.append(widget)
        self.append(bar)

        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True, focusable=True)
        self.area.set_draw_func(self._draw)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.area.add_controller(keys)
        focus = Gtk.EventControllerFocus()
        focus.connect("leave", lambda *_: self._leave_field())
        self.area.add_controller(focus)
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", self._right_click)
        self.area.add_controller(right)
        # On release rather than press: a press may be the start of a drag
        # that moves a signature, and a drag cancels the click.
        left = Gtk.GestureClick(button=1)
        left.connect("released", self._left_click)
        self.area.add_controller(left)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._motion)
        motion.connect("leave", lambda *_: self.highlight(None))
        self.area.add_controller(motion)
        drag = Gtk.GestureDrag(button=1)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.area.add_controller(drag)

        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        # The page area takes keyboard focus, and a viewport by default scrolls
        # to show whatever gains focus. The area is the whole scroll, so every
        # click and every closed dialog scrolled back to page 1.
        viewport = Gtk.Viewport(scroll_to_focus=False)
        viewport.set_child(self.area)
        self.scroller.set_child(viewport)
        self.scroller.get_vadjustment().connect("value-changed", self._scrolled)
        self.append(self.scroller)
        self._popover: Gtk.Popover | None = None

    # -- what to show ------------------------------------------------------

    def show(self, path: str, blanks: list, plan) -> None:
        self.document = Poppler.Document.new_from_file("file://" + os.path.abspath(path))
        self.blanks = blanks
        self.active, self.buffer = None, ""
        self._layout, self._layout_width, self._rendered = [], 0, {}
        self.page_no = 1
        self.scroller.get_vadjustment().set_value(0)
        self.set_plan(plan)

    def set_plan(self, plan) -> None:
        self.plan = plan
        self._images = {}
        if plan is not None:
            for entry in plan.entries:
                if entry.image is not None:
                    try:
                        self._images[entry.blank.id] = cairo.ImageSurface.create_from_png(
                            io.BytesIO(entry.image))
                    except Exception:  # noqa: BLE001, an unreadable image is just not drawn
                        pass
        self._update_bar()
        self.area.queue_draw()

    def go_to_page(self, page_no: int) -> None:
        """Scroll so that a page's top is at the top of the view."""
        if self.document is None or not self._layout:
            return
        page_no = max(1, min(page_no, self.document.get_n_pages()))
        placed = self._layout[page_no - 1]
        self.scroller.get_vadjustment().set_value(max(0.0, placed.top - MARGIN))

    def highlight(self, blank_id: str | None, *, reveal: bool = False) -> None:
        """Mark one blank on the page, and bring it into view when asked."""
        if blank_id == self.highlighted and not reveal:
            return
        self.highlighted = blank_id
        blank = next((b for b in self.blanks if b.id == blank_id), None)
        if reveal and blank is not None and blank.rect is not None and self._layout:
            placed = self._layout[blank.page - 1]
            top = placed.top + (placed.pdf_h - blank.rect[3]) * placed.scale
            adj = self.scroller.get_vadjustment()
            if not (adj.get_value() < top < adj.get_value() + adj.get_page_size() - 60):
                adj.set_value(max(0.0, top - adj.get_page_size() / 3))
        self.area.queue_draw()

    def _scrolled(self, *_args) -> None:
        self._track_page()
        self._render_visible()

    def _render_visible(self) -> None:
        """Render any page now in view that has not been rendered yet.

        GTK keeps a DrawingArea's output as one picture of the whole widget and
        shows a different part of it as you scroll, so the draw function is not
        called again on a scroll. Painting only the pages in view at draw time
        therefore left every other page blank once scrolled to. Pages are
        painted whenever their render exists; this makes the renders as they
        are needed, and asks for a repaint when it has made one.
        """
        if not self._layout:
            return
        adj = self.scroller.get_vadjustment()
        view_top, view_bottom = adj.get_value(), adj.get_value() + adj.get_page_size()
        made = False
        for placed in self._layout:
            if placed.bottom < view_top - 400 or placed.top > view_bottom + 400:
                continue
            key = (placed.number, int(placed.scale * 1000))
            if key not in self._rendered:
                self._rendered_page(placed)
                made = True
        if made:
            self.area.queue_draw()

    def _track_page(self) -> None:
        """Which page is under the middle of the view, for the bar."""
        if not self._layout:
            return
        adj = self.scroller.get_vadjustment()
        middle = adj.get_value() + adj.get_page_size() / 2
        current = next((p.number for p in self._layout if p.top <= middle <= p.bottom + GAP),
                       self._layout[-1].number if middle > self._layout[-1].bottom else 1)
        if current != self.page_no:
            self.page_no = current
            self._update_bar()

    def _update_bar(self) -> None:
        if self.document is None:
            self.label.set_text("")
            return
        total = self.document.get_n_pages()
        marks = sum(1 for e in (self.plan.entries if self.plan else [])
                    if e.filled and e.blank.page == self.page_no)
        self.label.set_text(f"page {self.page_no} of {total}, {marks} filled here")
        self.prev.set_sensitive(self.page_no > 1)
        self.next.set_sensitive(self.page_no < total)

    # -- geometry ----------------------------------------------------------

    def _lay_out(self, width: int) -> None:
        """Stack the pages for this width. Cached until the width changes."""
        if self.document is None or (self._layout and self._layout_width == width):
            return
        self._layout, self._rendered = [], {}
        top = float(MARGIN)
        for i in range(self.document.get_n_pages()):
            pw, ph = self.document.get_page(i).get_size()
            scale = min(MAX_SCALE, max(0.2, (width - 2 * MARGIN) / pw))
            self._layout.append(Placed(i + 1, top, pw * scale, ph * scale, scale, pw, ph))
            top += ph * scale + GAP
        self._layout_width = width
        self.area.set_content_height(int(top))

    def _page_at(self, py: float) -> Placed | None:
        return next((p for p in self._layout if p.top <= py <= p.bottom), None)

    def _to_pdf(self, placed: Placed, px: float, py: float) -> tuple[float, float]:
        return (px - MARGIN) / placed.scale, placed.pdf_h - (py - placed.top) / placed.scale

    def _blank_at(self, px: float, py: float):
        """The blank under a point on the screen, smallest first if they overlap."""
        placed = self._page_at(py)
        if placed is None:
            return None
        x, y = self._to_pdf(placed, px, py)
        hits = []
        for b in self.blanks:
            if b.page != placed.number or b.rect is None:
                continue
            r = self._hit_rect(b)
            if r[0] - 2 <= x <= r[2] + 2 and r[1] - 2 <= y <= r[3] + 2:
                hits.append((b, r))
        if not hits:
            return None
        # Something dragged on top wins over the field it was dragged across.
        return min(hits, key=lambda h: (h[0].offset == (0.0, 0.0),
                                        (h[1][2] - h[1][0]) * (h[1][3] - h[1][1])))[0]

    def _entry(self, blank):
        if self.plan is None:
            return None
        return next((e for e in self.plan.entries if e.blank.id == blank.id), None)

    def _hit_rect(self, blank):
        """Where the blank shows: moved if it was dragged, and for a signature
        the picture itself rather than the whole field."""
        rect = blank.shifted()
        entry = self._entry(blank)
        if entry is not None and not (blank.movable or entry.image is not None):
            entry = None
        if entry is not None and entry.image is not None:
            surface = self._images.get(blank.id)
            if surface is not None:
                aspect = surface.get_width() / max(1, surface.get_height())
                sx, sy, sw, sh = fit_signature(rect, aspect,
                                               exact=isinstance(blank.native, dict))
                return (sx, sy, sx + sw, sy + sh)
        return rect

    def _draggable_at(self, px: float, py: float):
        """A filled thing we drew, under the pointer: it can be picked up."""
        blank = self._blank_at(px, py)
        if blank is None:
            return None
        entry = self._entry(blank)
        if entry is None or not entry.filled:
            return None
        # A signature can sit on a form's plain text field; it is still a
        # picture we draw, so it moves. A typed value in a form field does not.
        return blank if blank.movable or entry.image is not None else None

    # -- drawing -----------------------------------------------------------

    def _rendered_page(self, placed: Placed) -> cairo.ImageSurface:
        """The page itself at this scale, rendered once and kept."""
        key = (placed.number, int(placed.scale * 1000))
        surface = self._rendered.get(key)
        if surface is None:
            surface = cairo.ImageSurface(cairo.FORMAT_RGB24, int(placed.width) + 1,
                                         int(placed.height) + 1)
            ctx = cairo.Context(surface)
            ctx.set_source_rgb(1, 1, 1)
            ctx.paint()
            ctx.scale(placed.scale, placed.scale)
            self.document.get_page(placed.number - 1).render(ctx)
            surface.flush()
            self._rendered[key] = surface
        return surface

    def _draw(self, area, ctx, width, height) -> None:
        if self.document is None:
            return
        if not self._layout or self._layout_width != width:
            self._lay_out(width)
            self._render_visible()
        filled_ids = {e.blank.id for e in (self.plan.entries if self.plan else []) if e.filled}

        for placed in self._layout:
            key = (placed.number, int(placed.scale * 1000))
            surface = self._rendered.get(key)
            ctx.save()
            ctx.translate(MARGIN, placed.top)
            if surface is None:
                # Not rendered yet: a blank sheet, replaced once it scrolls near.
                ctx.set_source_rgb(0.97, 0.97, 0.97)
                ctx.rectangle(0, 0, placed.width, placed.height)
                ctx.fill()
                ctx.restore()
                continue
            ctx.set_source_surface(surface, 0, 0)
            ctx.paint()
            ctx.scale(placed.scale, placed.scale)
            self._draw_marks(ctx, placed, filled_ids)
            ctx.restore()

    def _draw_marks(self, ctx, placed: Placed, filled_ids: set[str]) -> None:
        n, ph, scale = placed.number, placed.pdf_h, placed.scale

        # Every field the form has, so nothing is invisible.
        ctx.save()
        ctx.set_line_width(1.1 / scale)
        ctx.set_dash([3 / scale, 2 / scale])
        ctx.set_source_rgba(*FIELD)
        for blank in self.blanks:
            if blank.page != n or blank.rect is None or blank.id in filled_ids:
                continue
            if isinstance(blank.native, dict):
                continue  # our own printed-line rectangles are not fields
            x0, y0, x1, y1 = blank.rect
            ctx.rectangle(x0, ph - y1, x1 - x0, y1 - y0)
            ctx.stroke()
        ctx.restore()

        if self.plan is not None:
            for entry in self.plan.entries:
                if entry.filled and entry.blank.page == n and entry.blank.rect is not None:
                    self._draw_entry(ctx, entry, ph)

        # Black-outs: solid, since that is what they will be, with a thin red
        # edge so a pending one can still be told from the page.
        for region in self.redactions:
            if region.page != n:
                continue
            x0, y0, x1, y1 = region.rect
            ctx.save()
            ctx.set_source_rgb(0, 0, 0)
            ctx.rectangle(x0, ph - y1, x1 - x0, y1 - y0)
            ctx.fill()
            ctx.set_source_rgb(0.85, 0.15, 0.15)
            ctx.set_line_width(1.0 / scale)
            ctx.rectangle(x0, ph - y1, x1 - x0, y1 - y0)
            ctx.stroke()
            # The remove handle: a red square with a white ×, top right.
            s = CLOSE_PX / scale
            cx, cy = x1 - s, ph - y1
            ctx.rectangle(cx, cy, s, s)
            ctx.fill()
            ctx.set_source_rgb(1, 1, 1)
            ctx.set_line_width(1.6 / scale)
            ctx.move_to(cx + s * 0.28, cy + s * 0.28)
            ctx.line_to(cx + s * 0.72, cy + s * 0.72)
            ctx.move_to(cx + s * 0.72, cy + s * 0.28)
            ctx.line_to(cx + s * 0.28, cy + s * 0.72)
            ctx.stroke()
            ctx.restore()
        if self._drag is not None and self._drag[0].number == n:
            _, x0, y0, x1, y1 = self._drag
            ctx.save()
            ctx.set_source_rgba(0, 0, 0, 0.55)
            ctx.rectangle(min(x0, x1), ph - max(y0, y1), abs(x1 - x0), abs(y1 - y0))
            ctx.fill()
            ctx.restore()

        if self.active:
            blank = next((b for b in self.blanks if b.id == self.active), None)
            if blank is not None and blank.page == n and blank.rect is not None:
                x0, y0, x1, y1 = blank.rect
                ctx.save()
                ctx.set_source_rgb(*FOCUS)
                ctx.set_line_width(2.0 / scale)
                ctx.rectangle(x0 - 1, ph - y1 - 1, x1 - x0 + 2, y1 - y0 + 2)
                ctx.stroke()
                if blank.kind not in (BlankKind.CHECKBOX, BlankKind.RADIO):
                    # A caret after what has been typed, so it reads as a box
                    # with the cursor in it rather than a box that is lit.
                    box_h = y1 - y0
                    size = max(6.0, min(10.0, box_h * 0.72))
                    ctx.select_font_face("Helvetica", cairo.FONT_SLANT_NORMAL,
                                         cairo.FONT_WEIGHT_NORMAL)
                    ctx.set_font_size(size)
                    advance = ctx.text_extents(self.buffer).x_advance if self.buffer else 0
                    cx = x0 + 2 + advance + 0.5
                    top = ph - y1
                    ctx.set_line_width(1.0 / scale)
                    ctx.move_to(cx, top + box_h - (box_h - size) / 2 + size * 0.1)
                    ctx.line_to(cx, top + box_h - (box_h - size) / 2 - size * 0.95)
                    ctx.stroke()
                ctx.restore()

        if self.highlighted:
            blank = next((b for b in self.blanks if b.id == self.highlighted), None)
            if blank is not None and blank.page == n and blank.rect is not None:
                x0, y0, x1, y1 = self._hit_rect(blank)
                ctx.save()
                ctx.set_source_rgb(*FOCUS)
                ctx.set_line_width(2.0 / scale)
                ctx.rectangle(x0 - 2, ph - y1 - 2, x1 - x0 + 4, y1 - y0 + 4)
                ctx.stroke()
                ctx.restore()

    def _draw_entry(self, ctx, entry, ph: float) -> None:
        rect = entry.blank.shifted() if (entry.blank.movable or entry.image is not None) \
            else entry.blank.rect
        x0, y0, x1, y1 = rect
        top = ph - y1
        box_w, box_h = x1 - x0, y1 - y0

        if entry.image is not None:
            surface = self._images.get(entry.blank.id)
            if surface is None:
                return
            exact = isinstance(entry.blank.native, dict)
            aspect = surface.get_width() / max(1, surface.get_height())
            sx, sy, sw, sh = fit_signature(rect, aspect, exact=exact)
            ctx.save()
            ctx.set_source_rgba(*HIGHLIGHT)
            ctx.rectangle(sx, ph - sy - sh, sw, sh)
            ctx.fill()
            ctx.translate(sx, ph - sy - sh)
            ctx.scale(sw / surface.get_width(), sh / surface.get_height())
            ctx.set_source_surface(surface, 0, 0)
            ctx.paint()
            ctx.restore()
            return

        ctx.set_source_rgba(*HIGHLIGHT)
        ctx.rectangle(x0, top, box_w, box_h)
        ctx.fill()
        ctx.set_source_rgb(*INK)
        if entry.blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
            ctx.set_line_width(max(1.2, box_h * 0.16))
            ctx.move_to(x0 + box_w * 0.22, top + box_h * 0.55)
            ctx.line_to(x0 + box_w * 0.44, top + box_h * 0.78)
            ctx.line_to(x0 + box_w * 0.80, top + box_h * 0.25)
            ctx.stroke()
            return
        size = max(6.0, min(10.0, box_h * 0.72))
        ctx.select_font_face("Helvetica", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        ctx.set_font_size(size)
        ctx.move_to(x0 + 2, top + box_h - (box_h - size) / 2 - size * 0.18)
        ctx.show_text(entry.value or "")

    # -- the pointer -------------------------------------------------------

    def _motion(self, _controller, px, py) -> None:
        if self._close_handle_at(px, py) is not None:
            self.area.set_cursor_from_name("pointer")
            return
        if self.blackout_on:
            self.area.set_cursor_from_name("crosshair")
            return
        if self._moving is not None:
            return
        blank = self._blank_at(px, py)
        movable = blank is not None and self._draggable_at(px, py) is not None
        self.area.set_cursor_from_name("move" if movable else "pointer" if blank else "default")
        self.highlight(blank.id if blank else None)

    def _close_handle_at(self, px: float, py: float):
        """The black-out whose × is under the pointer, if any."""
        placed = self._page_at(py)
        if placed is None:
            return None
        x, y = self._to_pdf(placed, px, py)
        s = CLOSE_PX / placed.scale
        return next((r for r in self.redactions
                     if r.page == placed.number
                     and r.rect[2] - s <= x <= r.rect[2] and r.rect[3] - s <= y <= r.rect[3]),
                    None)

    def _left_click(self, gesture, n_press, px, py) -> None:
        self.area.grab_focus()
        if getattr(self, "_dragged", False):
            self._dragged = False
            return
        region = self._close_handle_at(px, py)
        if region is not None:
            gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            if self.on_unredact is not None:
                self.on_unredact(region)
            return
        if self.blackout_on:
            return
        placed = self._page_at(py)
        blank = self._blank_at(px, py)
        if placed is None or blank is None:
            self._leave_field()
            return
        x, y = self._to_pdf(placed, px, py)
        self._leave_field()
        self.on_activate(blank, x, y)

    # -- the keyboard ------------------------------------------------------

    def _typeable(self) -> list:
        """Every field the keys can reach, in reading order: page, then row
        from the top, then left to right. Rows are 6pt bands, so two boxes
        whose tops differ by a hair still read across before down."""
        fields = [b for b in self.blanks
                  if b.rect is not None and not b.readonly
                  and b.kind in (BlankKind.TEXT, BlankKind.MULTILINE, BlankKind.DATE,
                                 BlankKind.CHECKBOX, BlankKind.RADIO)]
        return sorted(fields, key=lambda b: (b.page, -round(b.rect[3] / 6), b.rect[0]))

    def _value_of(self, blank) -> str:
        if self.plan is None:
            return ""
        entry = next((e for e in self.plan.entries if e.blank.id == blank.id), None)
        return (entry.value or "") if entry is not None else ""

    def enter_field(self, blank) -> None:
        """Send the keys to this field, starting from what it already holds."""
        self._commit()
        self.active = blank.id
        self.buffer = self._value_of(blank)
        if self.buffer == "checked":
            self.buffer = ""
        self.highlight(blank.id, reveal=True)
        self.area.grab_focus()
        self.area.queue_draw()

    def _leave_field(self) -> None:
        self._commit()
        self.active, self.buffer = None, ""
        self.area.queue_draw()

    def _active_blank(self):
        return next((b for b in self.blanks if b.id == self.active), None)

    def _commit(self) -> None:
        blank = self._active_blank()
        if blank is None or blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
            return
        if self.on_typed is not None and self.buffer != self._value_of(blank):
            self.on_typed(blank, self.buffer, True)

    def _step(self, direction: int) -> None:
        fields = self._typeable()
        if not fields:
            return
        if self.active is None:
            # Nothing chosen yet: the first field on the page being looked at,
            # or the first there is.
            here = [b for b in fields if b.page >= self.page_no]
            target = (here or fields)[0] if direction > 0 else fields[-1]
        else:
            ids = [b.id for b in fields]
            index = ids.index(self.active) if self.active in ids else -1
            target = fields[(index + direction) % len(fields)]
        self.enter_field(target)

    def _key(self, _controller, keyval, _keycode, state) -> bool:
        if self.document is None or self.blackout_on:
            return False
        if keyval == Gdk.KEY_Tab or keyval == Gdk.KEY_ISO_Left_Tab:
            back = keyval == Gdk.KEY_ISO_Left_Tab or bool(state & Gdk.ModifierType.SHIFT_MASK)
            self._step(-1 if back else 1)
            return True
        blank = self._active_blank()
        if blank is None:
            return False
        if keyval == Gdk.KEY_Escape:
            self._leave_field()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
                if self.on_toggled is not None:
                    self.on_toggled(blank)
            else:
                self._step(1)
            return True
        if blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
            if keyval == Gdk.KEY_space and self.on_toggled is not None:
                self.on_toggled(blank)
                return True
            return False
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK):
            return False
        if keyval == Gdk.KEY_BackSpace:
            self.buffer = self.buffer[:-1]
        else:
            ch = Gdk.keyval_to_unicode(keyval)
            if not ch or ch < 32:
                return False
            self.buffer += chr(ch)
        if self.on_typed is not None:
            self.on_typed(blank, self.buffer, False)
        self.area.queue_draw()
        return True

    # -- black-outs --------------------------------------------------------

    def set_blackout(self, on: bool) -> None:
        self.blackout_on = on
        self.hint.set_text("drag over what must go; the page is re-rendered without it"
                           if on else HINT)
        self.area.set_cursor_from_name("crosshair" if on else "default")
        self.highlight(None)

    def _drag_begin(self, gesture, px, py) -> None:
        self._moving = None
        self._dragged = False
        if self._close_handle_at(px, py) is not None:
            return
        if not self.blackout_on:
            blank = self._draggable_at(px, py)
            placed = self._page_at(py)
            if blank is not None and placed is not None:
                # Picked up: follows the pointer until let go.
                self._moving = (blank, blank.offset, placed.scale)
                self.area.set_cursor_from_name("grabbing")
            return
        placed = self._page_at(py)
        if placed is None:
            return
        x, y = self._to_pdf(placed, px, py)
        self._drag = (placed, x, y, x, y)

    def _drag_update(self, gesture, dx, dy) -> None:
        if abs(dx) >= 3 or abs(dy) >= 3:
            self._dragged = True   # so the release is not also taken as a click
        if self._moving is not None:
            blank, (ox, oy), scale = self._moving
            blank.offset = (ox + dx / scale, oy - dy / scale)
            self.area.queue_draw()
            return
        if self._drag is None:
            return
        placed, x0, y0, _, _ = self._drag
        self._drag = (placed, x0, y0, x0 + dx / placed.scale, y0 - dy / placed.scale)
        self.area.queue_draw()

    def _drag_end(self, gesture, dx, dy) -> None:
        if self._moving is not None:
            blank, start, _scale = self._moving
            self._moving = None
            self.area.set_cursor_from_name("default")
            if abs(dx) < 3 and abs(dy) < 3:
                blank.offset = start   # a click, not a move
            elif self.on_moved is not None:
                self.on_moved(blank)
            self.area.queue_draw()
            return
        if self._drag is None:
            return
        placed, x0, y0, x1, y1 = self._drag
        self._drag = None
        self.area.queue_draw()
        if abs(x1 - x0) < 3 or abs(y1 - y0) < 3:
            return  # a click, not a box
        rect = (max(0.0, min(x0, x1)), max(0.0, min(y0, y1)),
                min(placed.pdf_w, max(x0, x1)), min(placed.pdf_h, max(y0, y1)))
        if self.on_redact is not None:
            self.on_redact(placed.number, rect)

    def _redaction_at(self, placed: Placed, x: float, y: float):
        return next((r for r in self.redactions
                     if r.page == placed.number
                     and r.rect[0] <= x <= r.rect[2] and r.rect[1] <= y <= r.rect[3]), None)

    def _right_click(self, gesture, n_press, px, py) -> None:
        placed = self._page_at(py)
        if placed is None:
            return
        x_pdf, y_pdf = self._to_pdf(placed, px, py)

        region = self._redaction_at(placed, x_pdf, y_pdf)
        if region is not None:
            if self._popover is not None:
                self._popover.unparent()
            popover = Gtk.Popover()
            popover.set_parent(self.area)
            popover.set_pointing_to(Gdk.Rectangle(x=int(px), y=int(py), width=1, height=1))
            button = Gtk.Button(label="Remove black-out", margin_top=6, margin_bottom=6,
                                margin_start=6, margin_end=6)
            button.add_css_class("flat")

            def removed(_b) -> None:
                popover.popdown()
                if self.on_unredact is not None:
                    self.on_unredact(region)

            button.connect("clicked", removed)
            popover.set_child(button)
            self._popover = popover
            popover.popup()
            return

        if self._popover is not None:
            self._popover.unparent()
        popover = Gtk.Popover()
        popover.set_parent(self.area)
        popover.set_pointing_to(Gdk.Rectangle(x=int(px), y=int(py), width=1, height=1))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2,
                      margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        for title, kind in (("Sign here", BlankKind.SIGNATURE),
                            ("Date here", BlankKind.DATE),
                            ("Type here…", BlankKind.TEXT),
                            ("Tick here", BlankKind.CHECKBOX)):
            button = Gtk.Button(label=title, halign=Gtk.Align.FILL)
            button.add_css_class("flat")

            def chosen(_b, kind=kind, page_no=placed.number) -> None:
                popover.popdown()
                self.on_place(page_no, kind, x_pdf, y_pdf)

            button.connect("clicked", chosen)
            box.append(button)
        popover.set_child(box)
        self._popover = popover
        popover.popup()
