"""Page surgery: merge, split, reorder, rotate, and photos into pages.

A document is described as a sequence of sources, each one page of one file
with an extra rotation, and assembled into a new file. Merge, delete, reorder,
rotate and extract are all the same operation with a different sequence, which
keeps this to one function and one test surface. Nothing is ever changed in
place: the output is always a new file.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from pathlib import Path

import pikepdf

LETTER = (612.0, 792.0)


@dataclass(frozen=True)
class Source:
    path: str
    index: int          # zero-based page in `path`
    rotation: int = 0   # extra quarter turns as degrees: 0, 90, 180, 270

    def rotated(self, delta: int) -> "Source":
        return Source(self.path, self.index, (self.rotation + delta) % 360)


@dataclass(frozen=True)
class PageInfo:
    number: int
    width: float
    height: float
    rotation: int


def info(path: str) -> list[PageInfo]:
    out = []
    with pikepdf.open(path) as pdf:
        for i, page in enumerate(pdf.pages):
            box = page.MediaBox
            out.append(PageInfo(i + 1, float(box[2]) - float(box[0]),
                                float(box[3]) - float(box[1]),
                                int(page.get("/Rotate", 0) or 0) % 360))
    return out


def sequence(path: str) -> list[Source]:
    """Every page of a file, in order, as the starting sequence."""
    return [Source(path, i) for i in range(len(info(path)))]


def assemble(sources: list[Source], out_path: str) -> int:
    """Write the sequence as a new PDF. Returns the page count written."""
    if not sources:
        raise ValueError("nothing to write: the sequence is empty")
    with warnings.catch_warnings():
        return _assemble(sources, out_path)


def _assemble(sources: list[Source], out_path: str) -> int:
    out = pikepdf.Pdf.new()
    opened: dict[str, pikepdf.Pdf] = {}
    try:
        # pikepdf warns that a copied page's widgets are orphaned from any
        # form. They are, until _rebuild_form gives them one below.
        warnings.simplefilter("ignore", getattr(pikepdf, "PageCopyWarning", UserWarning))
        for source in sources:
            src = opened.get(source.path)
            if src is None:
                src = opened[source.path] = pikepdf.open(source.path)
            if not 0 <= source.index < len(src.pages):
                raise ValueError(f"{Path(source.path).name} has no page {source.index + 1}")
            out.pages.append(src.pages[source.index])
            if source.rotation:
                page = out.pages[-1]
                page.Rotate = (int(page.get("/Rotate", 0) or 0) + source.rotation) % 360
        _rebuild_form(out, opened.values())
        out.save(out_path)
    finally:
        for src in opened.values():
            src.close()
    return len(sources)


def _rebuild_form(out: pikepdf.Pdf, sources) -> None:
    """Give the new file a form that reaches every widget on its pages.

    Copying pages copies their widgets but not the document-level form that
    lists them, and a widget the form does not list may not show in Acrobat.
    A merged packet of unfilled forms should still be fillable, so the form
    is rebuilt from the widgets that came along, with the first source's font
    resources.
    """
    fields = []
    seen = set()
    for page in out.pages:
        for annot in page.get("/Annots", []) or []:
            if annot.get("/Subtype") != "/Widget":
                continue
            from .redact import field_root
            root = field_root(annot)
            if root.objgen in seen:
                continue
            seen.add(root.objgen)
            fields.append(root)
    if not fields:
        return
    form = pikepdf.Dictionary(Fields=pikepdf.Array(fields), NeedAppearances=True)
    for src in sources:
        acro = src.Root.get("/AcroForm")
        if acro is None or "/DR" not in acro:
            continue
        # Objects from another file must be copied across, one by one; a
        # dictionary that merely points at them raises ForeignObjectError.
        fonts = pikepdf.Dictionary()
        for name, font in (acro.DR.get("/Font") or pikepdf.Dictionary()).items():
            fonts[name] = (out.copy_foreign(font) if font.is_indirect
                           else out.copy_foreign(src.make_indirect(font)))
        form.DR = pikepdf.Dictionary(Font=fonts)
        if "/DA" in acro:
            form.DA = pikepdf.String(str(acro.DA))
        break
    out.Root.AcroForm = out.make_indirect(form)


def images_to_pdf(image_paths: list[str], out_path: str) -> int:
    """Photographed or scanned pages become a PDF, one image per page.

    Each image is fitted to a letter page at its own aspect, which is what a
    phone photo of a form wants; a scan that is already letter-shaped fills it.
    """
    from PIL import Image

    pages = []
    for path in image_paths:
        image = Image.open(path)
        image = image.convert("RGB")
        w, h = image.size
        # Portrait pages for portrait photos, landscape for landscape.
        page_w, page_h = LETTER if h >= w else (LETTER[1], LETTER[0])
        # PIL writes the image at its pixel size; scale so it fits the page at
        # 150 dpi, which keeps a phone photo sharp without a huge file.
        target_w, target_h = page_w / 72 * 150, page_h / 72 * 150
        scale = min(target_w / w, target_h / h)
        if scale < 1:
            image = image.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        pages.append(image)
    if not pages:
        raise ValueError("no images given")
    first, rest = pages[0], pages[1:]
    first.save(out_path, "PDF", resolution=150.0, save_all=True, append_images=rest)
    return len(pages)


def parse_spec(spec: str) -> list[Source]:
    """`file.pdf`, `file.pdf#1-3,5`, `file.pdf#2@90`: pages and a rotation.

    Page numbers are one-based, as printed on the page. No page list means
    every page.
    """
    path, _, rest = spec.partition("#")
    if not path:
        raise ValueError(f"no file in {spec!r}")
    rest, _, rot = rest.partition("@")
    rotation = int(rot) if rot else 0
    if rotation % 90:
        raise ValueError(f"rotation must be a multiple of 90, not {rotation}")
    count = len(info(path))
    if not rest:
        indexes = list(range(count))
    else:
        indexes = []
        for part in rest.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                lo, hi = int(a), int(b) if b else count
            else:
                lo = hi = int(part)
            if not 1 <= lo <= hi <= count:
                raise ValueError(f"{Path(path).name} has pages 1 to {count}, not {part}")
            indexes.extend(range(lo - 1, hi))
    return [Source(path, i, rotation % 360) for i in indexes]
