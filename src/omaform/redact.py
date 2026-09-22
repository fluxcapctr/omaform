"""Black out part of a page for good.

A black rectangle drawn over text is not a redaction: the text is still in
the file, one copy-paste away. The only honest way to remove something from a
PDF page is to remove it, and the only way to be sure of that on an arbitrary
page is to turn the page into a picture with the region already black. That is
what this does. A page with a black-out on it is rendered at 300 dpi, the
region is painted over in the pixels, and the page is rebuilt as that image
alone: no text layer, no fields, no annotations, no hidden layers. Pages with
nothing to black out are left exactly as they were.

The cost is that a redacted page can no longer be searched or filled. That is
the correct cost. A redacted W-9 does not need to be fillable.

Regions are given in PDF space: page number from 1, and a rectangle with the
origin at the bottom left, the way fields are placed. `find_text` turns a
string into regions, one per occurrence, for the case where what needs to go
is a number rather than a place.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pikepdf

from . import textmap

DPI = 300
PAD_PT = 1.5   # a hair past the region, so no edge of a glyph survives


@dataclass(frozen=True)
class Region:
    page: int                                   # from 1
    # The page as it is shown: rotation and crop applied, in points, origin at
    # the bottom left. The same space the page view and Poppler's text use, so
    # a box dragged on screen and a word found by its text agree.
    rect: tuple[float, float, float, float]

    @classmethod
    def parse(cls, spec: str) -> "Region":
        """`PAGE:x0,y0,x1,y1` in points."""
        m = re.fullmatch(r"\s*(\d+)\s*:\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*,\s*([-\d.]+)\s*", spec)
        if not m:
            raise ValueError(f"not a region: {spec!r}; want PAGE:x0,y0,x1,y1")
        page = int(m.group(1))
        x0, y0, x1, y1 = (float(m.group(i)) for i in range(2, 6))
        return cls(page, (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def find_text(path: str, needle: str, pages: list[textmap.PageText] | None = None) -> list[Region]:
    """Every place the words of `needle` appear in a row on any page.

    Matched on letters and digits only, so "123-45-6789" also finds
    "123 45 6789" and "123456789", which is what a person means by it.
    """
    target = _norm(needle)
    if not target:
        return []
    found: list[Region] = []
    for page in pages or textmap.read_pdf(path):
        words = [(w, _norm(w.text)) for w in page.words if _norm(w.text)]
        for start in range(len(words)):
            joined = ""
            for end in range(start, len(words)):
                joined += words[end][1]
                if not target.startswith(joined):
                    break
                if joined == target:
                    run = [w for w, _ in words[start:end + 1]]
                    x0 = min(w.box[0] for w in run)
                    y0 = min(w.box[1] for w in run)
                    x1 = max(w.box[2] for w in run)
                    y1 = max(w.box[3] for w in run)
                    # Top-left page space to PDF space.
                    found.append(Region(page.number,
                                        (x0, page.height - y1, x1, page.height - y0)))
                    break
    return found


def _render(path: str, page_number: int, out_dir: str) -> Path:
    prefix = Path(out_dir) / f"page-{page_number}"
    subprocess.run(["pdftoppm", "-f", str(page_number), "-l", str(page_number),
                    "-r", str(DPI), "-cropbox", "-png", "--", path, str(prefix)],
                   check=True, capture_output=True, timeout=120)
    matches = sorted(Path(out_dir).glob(f"page-{page_number}*.png"))
    if not matches:
        raise RuntimeError(f"could not render page {page_number}")
    return matches[0]


def _display_size(path: str, page_number: int) -> tuple[float, float]:
    """The page's size as shown, from Poppler, which is what renders it."""
    import gi
    gi.require_version("Poppler", "0.18")
    from gi.repository import Poppler

    doc = Poppler.Document.new_from_file("file://" + os.path.abspath(path))
    return doc.get_page(page_number - 1).get_size()


def _picture_page(pdf: pikepdf.Pdf, page: pikepdf.Page, jpeg: bytes,
                  width_px: int, height_px: int, width_pt: float, height_pt: float) -> None:
    """Replace everything on the page with one image of it, as shown.

    The image is already upright and cropped, so the page becomes exactly
    that size with no rotation and no crop of its own; otherwise a rotated
    page's picture would be stretched into its unrotated box.
    """
    image = pikepdf.Stream(pdf, jpeg)
    image.Type = pikepdf.Name.XObject
    image.Subtype = pikepdf.Name.Image
    image.Width, image.Height = width_px, height_px
    image.ColorSpace = pikepdf.Name.DeviceRGB
    image.BitsPerComponent = 8
    image.Filter = pikepdf.Name.DCTDecode
    content = f"q {width_pt:.2f} 0 0 {height_pt:.2f} 0 0 cm /Im0 Do Q".encode()
    page.Contents = pdf.make_indirect(pikepdf.Stream(pdf, content))
    page.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=pdf.make_indirect(image)))
    for key in ("/Annots", "/Rotate", "/Group", "/Thumb", "/PieceInfo", "/AA",
                "/CropBox", "/BleedBox", "/TrimBox", "/ArtBox", "/StructParents"):
        if key in page.obj:
            del page.obj[key]
    page.obj.MediaBox = pikepdf.Array([0, 0, width_pt, height_pt])


MAX_FIELD_DEPTH = 64


def field_root(annot):
    """The top of a widget's field tree, refusing cycles and absurd depth."""
    node, seen = annot, set()
    while "/Parent" in node:
        if node.objgen in seen or len(seen) > MAX_FIELD_DEPTH:
            raise ValueError("the form's field tree is malformed (a field is its own ancestor)")
        if node.objgen != (0, 0):
            seen.add(node.objgen)
        node = node.Parent
    return node


def _prune_form(pdf: pikepdf.Pdf) -> None:
    """The form may only reach widgets that are still on a page.

    Pruned node by node, not just at the top: a parent field whose kids sit on
    two pages survives through the page that kept its widget, and the kid on
    the blacked-out page, with its value, has to go from under it.
    """
    acro = pdf.Root.get("/AcroForm")
    if acro is None:
        return
    alive: set = set()        # kept widgets and every ancestor of one
    roots: list = []
    for page in pdf.pages:
        for annot in page.get("/Annots", []) or []:
            if annot.get("/Subtype") != "/Widget":
                continue
            root = field_root(annot)
            node = annot
            alive.add(node.objgen)
            while "/Parent" in node:
                node = node.Parent
                alive.add(node.objgen)
            if root.objgen not in {r.objgen for r in roots}:
                roots.append(root)

    def prune(node, depth: int = 0) -> None:
        if depth > MAX_FIELD_DEPTH or "/Kids" not in node:
            return
        kept = [k for k in node.Kids if k.objgen in alive]
        node.Kids = pikepdf.Array(kept)
        for kid in kept:
            prune(kid, depth + 1)

    for root in roots:
        prune(root)
    if not roots:
        del pdf.Root["/AcroForm"]
        return
    acro.Fields = pikepdf.Array(roots)
    # Other places a form lists its fields: calculation order holds fields
    # directly, so a retired one is still reachable, value and all.
    if "/CO" in acro:
        acro.CO = pikepdf.Array([f for f in acro.CO if f.objgen in alive])
    # An XFA packet is a second copy of the whole form's data, as XML.
    if "/XFA" in acro:
        del acro["/XFA"]


def apply(src: str, regions: list[Region], out: str) -> int:
    """Write a copy of `src` with the regions gone. Returns pages rebuilt."""
    if os.path.abspath(src) == os.path.abspath(out):
        raise ValueError("the output must be a new file; a source is never overwritten")
    if not regions:
        raise ValueError("nothing to black out")
    from PIL import Image, ImageDraw

    by_page: dict[int, list[Region]] = {}
    for region in regions:
        by_page.setdefault(region.page, []).append(region)

    pdf = pikepdf.open(src)
    try:
        for number in by_page:
            if not 1 <= number <= len(pdf.pages):
                raise ValueError(f"no page {number}")
        with tempfile.TemporaryDirectory(prefix="omaform-redact-") as tmp:
            for number, wanted in sorted(by_page.items()):
                page = pdf.pages[number - 1]
                # Rendered from the source file, so what is baked is exactly
                # what a reader shows, field values and all.
                image = Image.open(_render(src, number, tmp)).convert("RGB")
                px_w, px_h = image.size
                # Regions, the render and Poppler's text all share the page as
                # shown, so this is a scale and a flip, nothing more.
                disp_w, disp_h = _display_size(src, number)
                sx, sy = px_w / disp_w, px_h / disp_h
                draw = ImageDraw.Draw(image)
                for region in wanted:
                    x0, y0, x1, y1 = region.rect
                    x0, y0, x1, y1 = x0 - PAD_PT, y0 - PAD_PT, x1 + PAD_PT, y1 + PAD_PT
                    draw.rectangle([x0 * sx, (disp_h - y1) * sy,
                                    x1 * sx, (disp_h - y0) * sy], fill=(0, 0, 0))
                buf = io.BytesIO()
                image.save(buf, "JPEG", quality=88)
                _picture_page(pdf, page, buf.getvalue(), px_w, px_h, disp_w, disp_h)
        _prune_form(pdf)
        # Anything that could remember the old page: no outline targets to
        # dead text, no cached structure tree, no XMP describing fields.
        for key in ("/StructTreeRoot", "/MarkInfo", "/Metadata"):
            if key in pdf.Root:
                del pdf.Root[key]
        pdf.save(out)
    finally:
        pdf.close()
    return len(by_page)
