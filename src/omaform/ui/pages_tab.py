"""The Pages tab: a grid of the sequence, dragged into order, rotated, removed.

Works on a sequence of sources rather than on any file: add whole files or
photographs to it, shuffle it, and Save as writes a new PDF. Nothing is
changed in place.
"""

from __future__ import annotations

import os
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Poppler", "0.18")
from gi.repository import Adw, Gdk, GLib, GObject, Gio, Gtk, Poppler  # noqa: E402

from .. import pages  # noqa: E402

THUMB_W = 240            # the default; the slider in the bar changes it
THUMB_MIN, THUMB_MAX = 140, 520


class PagesTab(Gtk.Box):
    """Pages as a grid of large thumbnails, in reading order, left to right.

    Drag a page onto another to put it there. Each card has two small buttons,
    rotate and remove; the slider in the bar makes every page bigger, for the
    times it matters which page is which.
    """

    def __init__(self, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self.sequence: list[pages.Source] = []
        self.thumb_w = THUMB_W
        self._docs: dict[str, Poppler.Document] = {}
        self._thumbs: dict[tuple[str, int, int, int], Gdk.Texture] = {}

        bar = Gtk.Box(spacing=8, margin_start=8, margin_end=8, margin_top=6, margin_bottom=6)
        for label, handler in (("Add PDF…", self._add_pdf),
                               ("Add photos…", self._add_images),
                               ("Save as…", self._save_as)):
            button = Gtk.Button(label=label)
            button.connect("clicked", handler)
            bar.append(button)
        self.summary = Gtk.Label(label="", hexpand=True, xalign=0.5)
        self.summary.add_css_class("dim-label")
        bar.append(self.summary)
        size_icon = Gtk.Image.new_from_icon_name("zoom-in-symbolic")
        size_icon.add_css_class("dim-label")
        self.size = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL,
                                             THUMB_MIN, THUMB_MAX, 20)
        self.size.set_value(self.thumb_w)
        self.size.set_draw_value(False)
        self.size.set_size_request(160, -1)
        self.size.set_tooltip_text("Page size")
        self.size.connect("value-changed", self._resized)
        bar.append(size_icon)
        bar.append(self.size)
        self.append(bar)

        self.grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=False,
                                halign=Gtk.Align.START,
                                min_children_per_line=1, max_children_per_line=30,
                                row_spacing=14, column_spacing=14,
                                valign=Gtk.Align.START,
                                margin_start=14, margin_end=14, margin_top=8,
                                margin_bottom=14)
        self.scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        self.scroller.set_child(self.grid)
        self.append(self.scroller)
        self.empty = Adw.StatusPage(
            icon_name="view-paged-symbolic", title="No pages yet",
            description="Add a PDF or photos, or open a form on the Fill tab.")
        self.empty.set_vexpand(True)
        self.append(self.empty)
        self._refresh()

    # -- the sequence ------------------------------------------------------

    def load(self, path: str) -> None:
        """Start from every page of one file."""
        self.sequence = pages.sequence(path)
        self._refresh()

    def append_file(self, path: str) -> None:
        self.sequence += pages.sequence(path)
        self._refresh()

    def move(self, src: int, dst: int) -> None:
        """Put page `src` where page `dst` is, shifting the rest along."""
        if src == dst or not (0 <= src < len(self.sequence)) \
                or not (0 <= dst < len(self.sequence)):
            return
        page = self.sequence.pop(src)
        self.sequence.insert(dst, page)
        self._refresh()

    def _doc(self, path: str) -> Poppler.Document:
        doc = self._docs.get(path)
        if doc is None:
            doc = self._docs[path] = Poppler.Document.new_from_file(
                "file://" + os.path.abspath(path))
        return doc

    def _thumb(self, source: pages.Source) -> Gdk.Texture:
        # Rendered at twice the card width, so a HiDPI screen and the slider
        # both stay sharp without a re-render per notch.
        width = self.thumb_w * 2
        key = (source.path, source.index, source.rotation, width)
        texture = self._thumbs.get(key)
        if texture is not None:
            return texture
        page = self._doc(source.path).get_page(source.index)
        pw, ph = page.get_size()
        turned = source.rotation % 180 == 90
        out_w, out_h = (ph, pw) if turned else (pw, ph)
        scale = width / out_w
        surface = cairo.ImageSurface(cairo.FORMAT_RGB24, int(out_w * scale), int(out_h * scale))
        ctx = cairo.Context(surface)
        ctx.set_source_rgb(1, 1, 1)
        ctx.paint()
        ctx.scale(scale, scale)
        ctx.translate(out_w / 2, out_h / 2)
        ctx.rotate(source.rotation * 3.14159265 / 180)
        ctx.translate(-pw / 2, -ph / 2)
        page.render(ctx)
        surface.flush()
        data = GLib.Bytes.new(surface.get_data().tobytes())
        texture = Gdk.MemoryTexture.new(surface.get_width(), surface.get_height(),
                                        Gdk.MemoryFormat.B8G8R8A8, data, surface.get_stride())
        self._thumbs[key] = texture
        return texture

    def _resized(self, scale) -> None:
        width = int(scale.get_value())
        if width != self.thumb_w:
            self.thumb_w = width
            self._refresh()

    def _refresh(self) -> None:
        self.grid.remove_all()
        self.empty.set_visible(not self.sequence)
        self.scroller.set_visible(bool(self.sequence))
        files = {s.path for s in self.sequence}
        self.summary.set_text(
            f"{len(self.sequence)} pages from {len(files)} file{'s' if len(files) != 1 else ''}"
            "  \u00b7  drag a page to move it" if self.sequence else "")
        for position, source in enumerate(self.sequence):
            self.grid.append(self._card(position, source))

    def _card(self, position: int, source: pages.Source) -> Gtk.Widget:
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       halign=Gtk.Align.CENTER, valign=Gtk.Align.START)
        card.add_css_class("omaform-page-card")
        card.set_size_request(self.thumb_w, -1)

        texture = self._thumb(source)
        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        picture.set_can_shrink(True)
        height = int(self.thumb_w * texture.get_height() / max(1, texture.get_width()))
        picture.set_size_request(self.thumb_w, height)
        picture.add_css_class("omaform-page-thumb")
        picture.set_cursor_from_name("grab")
        # The texture is twice the card for sharpness, so its natural width is
        # too; the clamp holds the card to the slider's width.
        clamp = Adw.Clamp(maximum_size=self.thumb_w, tightening_threshold=self.thumb_w)
        clamp.set_child(picture)
        card.append(clamp)

        foot = Gtk.Box(spacing=2)
        name = Path(source.path).name
        label = Gtk.Label(label=f"{position + 1}", xalign=0)
        label.add_css_class("heading")
        foot.append(label)
        detail = Gtk.Label(label=f"{name}, p.{source.index + 1}"
                           + (f", {source.rotation}\u00b0" if source.rotation else ""),
                           xalign=0, hexpand=True, ellipsize=3, margin_start=6)
        detail.set_tooltip_text(f"{name}, page {source.index + 1}")
        detail.add_css_class("dim-label")
        detail.add_css_class("caption")
        foot.append(detail)
        for icon, tip, handler in (("object-rotate-right-symbolic", "Rotate", self._rotate),
                                   ("user-trash-symbolic", "Remove", self._delete)):
            button = Gtk.Button(icon_name=icon, tooltip_text=tip, valign=Gtk.Align.CENTER)
            button.add_css_class("flat")
            button.add_css_class("circular")
            button.add_css_class("omaform-small")
            button.connect("clicked", handler, position)
            foot.append(button)
        card.append(foot)

        # Drag the page by its picture; drop it on another page to take its place.
        drag = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        drag.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(str(position)))
        drag.connect("drag-begin", lambda src, _d: (
            src.set_icon(Gtk.WidgetPaintable.new(picture), self.thumb_w // 2, 20),
            card.add_css_class("omaform-dragging")))
        drag.connect("drag-end", lambda *_: card.remove_css_class("omaform-dragging"))
        picture.add_controller(drag)

        drop = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        drop.connect("enter", lambda *_: (card.add_css_class("omaform-drop"),
                                          Gdk.DragAction.MOVE)[1])
        drop.connect("leave", lambda *_: card.remove_css_class("omaform-drop"))

        def dropped(_target, value, _x, _y) -> bool:
            card.remove_css_class("omaform-drop")
            try:
                src = int(value)
            except (TypeError, ValueError):
                return False
            # Deferred: the card being dropped on is about to be rebuilt.
            GLib.idle_add(lambda: (self.move(src, position), False)[1])
            return True

        drop.connect("drop", dropped)
        card.add_controller(drop)
        return card

    def _rotate(self, _b, i: int) -> None:
        self.sequence[i] = self.sequence[i].rotated(90)
        self._refresh()

    def _delete(self, _b, i: int) -> None:
        del self.sequence[i]
        self._refresh()

    # -- files in and out --------------------------------------------------

    def _add_pdf(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Add pages from")
        pdfs = Gtk.FileFilter(name="PDF")
        pdfs.add_pattern("*.pdf")
        pdfs.add_pattern("*.PDF")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(pdfs)
        dialog.set_filters(filters)

        def chosen(dlg, result) -> None:
            try:
                files = dlg.open_multiple_finish(result)
            except GLib.Error:
                return
            for i in range(files.get_n_items()):
                self.append_file(files.get_item(i).get_path())

        dialog.open_multiple(self.window, None, chosen)

    def _add_images(self, *_args) -> None:
        dialog = Gtk.FileDialog(title="Add photos or scans")
        images = Gtk.FileFilter(name="Images")
        for pattern in ("*.png", "*.jpg", "*.jpeg", "*.JPG", "*.PNG", "*.webp", "*.tif", "*.tiff"):
            images.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(images)
        dialog.set_filters(filters)

        def chosen(dlg, result) -> None:
            try:
                files = dlg.open_multiple_finish(result)
            except GLib.Error:
                return
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            if not paths:
                return
            # Photos become a PDF of their own first, then join the sequence.
            out = Path(paths[0]).with_suffix(".pdf")
            n = 1
            while out.exists():
                out = Path(paths[0]).with_name(f"{Path(paths[0]).stem}-{n}.pdf")
                n += 1
            try:
                pages.images_to_pdf(paths, str(out))
            except Exception as exc:  # noqa: BLE001
                self.window.toast(f"Could not read those images: {exc}")
                return
            self.append_file(str(out))
            self.window.toast(f"{len(paths)} photos became {out.name}")

        dialog.open_multiple(self.window, None, chosen)

    def _save_as(self, *_args) -> None:
        if not self.sequence:
            self.window.toast("Nothing to save yet")
            return
        dialog = Gtk.FileDialog(title="Save pages as")
        first = Path(self.sequence[0].path)
        dialog.set_initial_folder(Gio.File.new_for_path(str(first.parent)))
        dialog.set_initial_name(f"{first.stem}-pages.pdf")

        def chosen(dlg, result) -> None:
            try:
                file = dlg.save_finish(result)
            except GLib.Error:
                return
            if file is None:
                return
            target = file.get_path()
            if any(os.path.abspath(s.path) == os.path.abspath(target) for s in self.sequence):
                self.window.toast("Choose a new name: a source file is never overwritten")
                return
            try:
                count = pages.assemble(self.sequence, target)
            except Exception as exc:  # noqa: BLE001
                self.window.toast(f"Could not save: {exc}")
                return
            self.window.toast(f"Saved {count} pages to {Path(target).name}", action="Open",
                              on_action=lambda: Gio.AppInfo.launch_default_for_uri(
                                  f"file://{target}", None))

        dialog.save(self.window, None, chosen)
