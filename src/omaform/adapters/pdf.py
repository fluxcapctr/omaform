"""PDF adapter: AcroForm fields.

Phase 1 covers PDFs that carry real form fields, which is what the IRS and USCIS
ship. Flat PDFs with no fields at all are Phase 6 and use the same `Blank`
objects, produced from the page's drawing instructions instead of its widgets.

The one surprise worth recording: field *names* on real government forms carry no
information. The W-9 calls its business-name box `f1_02[0]` and supplies no
tooltip, so labels come from the printed text around each widget's rectangle,
exactly as they will for flat PDFs. Geometry is not a fallback here.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import zlib

import pikepdf

from .. import labeling, textmap
from ..model import Blank, BlankKind, Document, LabelContext


def placed_by_hand(page: int, kind: BlankKind, x: float, y: float,
                   width: float = 200.0, height: float = 22.0) -> Blank:
    """A blank the user put on the page themselves, by pointing at it.

    `x`, `y` are PDF user space: the point becomes the bottom-left of the
    rectangle, so a click on a printed line signs on that line. The writer
    treats it like any printed-line blank and draws straight onto the page.
    """
    if kind is BlankKind.DATE:
        height, width = 12.0, min(width, 150.0)
        label = "Date"
    elif kind is BlankKind.TEXT:
        height, width = 12.0, width
        label = "Text"
    elif kind is BlankKind.CHECKBOX:
        height = width = 10.0
        label = "Tick"
    else:
        label = "Signature"
    rect = (x, y, x + width, y + height)
    return Blank(id=f"p{page}:~hand-{kind.value}-{int(x)}-{int(y)}", kind=kind,
                 label=LabelContext(left=label), page=page, width_pt=width,
                 height_pt=height, rect=rect, native={"synthetic": True, "rect": rect})

# Flags from the PDF specification's field flag word (/Ff).
_FF_READONLY = 1 << 0
_FF_MULTILINE = 1 << 12
_FF_RADIO = 1 << 15

# Two widgets belong to the same split run if their tops and bottoms agree this
# closely and they sit this near each other horizontally.
_BAND_TOLERANCE = 2.0
_MAX_GROUP_GAP = 30.0
_PUNCT_ONLY = set("-–—.,/:;|()[] ")


def _inherited(annot, key: str):
    """Widget properties may live on the widget or on its parent field."""
    if key in annot:
        return annot[key]
    parent = annot.get("/Parent")
    seen = 0
    while parent is not None and seen < 8:
        if key in parent:
            return parent[key]
        parent = parent.get("/Parent")
        seen += 1
    return None


def _kind(ft: str, flags: int) -> BlankKind:
    if ft == "/Btn":
        return BlankKind.RADIO if flags & _FF_RADIO else BlankKind.CHECKBOX
    if ft == "/Tx" and flags & _FF_MULTILINE:
        return BlankKind.MULTILINE
    return BlankKind.TEXT


def _options(annot) -> list[str]:
    """On-state names for a button, from its normal appearance dictionary."""
    ap = annot.get("/AP")
    if ap is None or "/N" not in ap:
        return []
    try:
        return [str(k) for k in ap["/N"].keys() if str(k) != "/Off"]
    except Exception:
        return []


def _group_runs(blanks: list[Blank], boxes: dict[str, textmap.Box]) -> None:
    """Tag runs of same-band adjacent boxes that share one label as a group.

    This is what makes the W-9's Social Security row work: three boxes under one
    "Social security number" label holding three digits, two digits and four. The
    run is only a *candidate* here. The planner accepts it once the matched key
    turns out to have a digit grouping of the same length, and dissolves it
    otherwise, so two unrelated side-by-side fields cannot be merged.
    """
    text_blanks = [b for b in blanks if b.kind in (BlankKind.TEXT, BlankKind.MULTILINE)]
    by_page: dict[int, list[Blank]] = {}
    for b in text_blanks:
        by_page.setdefault(b.page, []).append(b)

    run_no = 0
    for page, members in by_page.items():
        members.sort(key=lambda b: (boxes[b.id][1], boxes[b.id][0]))
        run: list[Blank] = []

        def flush() -> None:
            nonlocal run_no, run
            if len(run) >= 2:
                labels = [b.label.best() for b in run]
                real = [l for l in labels if l and not set(l) <= _PUNCT_ONLY]
                # One shared label, with any punctuation-only members (the "-"
                # printed between TIN boxes) going along for the ride.
                if real and len(set(real)) == 1:
                    run_no += 1
                    gid = f"g{run_no}"
                    shared = real[0]
                    for i, b in enumerate(run):
                        b.group, b.group_index = gid, i
                        if not b.label.above and not b.label.left:
                            b.label.above = shared
            run = []

        for b in members:
            if not run:
                run = [b]
                continue
            prev, cur = boxes[run[-1].id], boxes[b.id]
            same_band = (abs(prev[1] - cur[1]) <= _BAND_TOLERANCE
                         and abs(prev[3] - cur[3]) <= _BAND_TOLERANCE)
            near = 0 <= cur[0] - prev[2] <= _MAX_GROUP_GAP
            if same_band and near:
                run.append(b)
            else:
                flush()
                run = [b]
        flush()


def _widgets(pdf: pikepdf.Pdf):
    """Every form widget with its stable id, in document order.

    Ids carry the page and an occurrence counter because widget names repeat
    across pages on multi-page forms. This walk is shared by discover() and
    write(), which is what lets write() work on a freshly opened copy of the
    file and still find the same widget the plan was talking about.
    """
    seen: dict[str, int] = {}
    for index, page in enumerate(pdf.pages):
        for annot in page.get("/Annots", []) or []:
            if annot.get("/Subtype") != "/Widget":
                continue
            ft = _inherited(annot, "/FT")
            if ft is None:
                continue
            name = str(_inherited(annot, "/T") or "")
            seen[name] = seen.get(name, 0) + 1
            yield index, page, annot, str(ft), name, f"p{index + 1}:{name or 'unnamed'}#{seen[name]}"


def _image_xobject(pdf: pikepdf.Pdf, png: bytes) -> pikepdf.Stream:
    """A PNG with transparency as a PDF image with a soft mask."""
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGBA")
    rgb = image.convert("RGB").tobytes()
    alpha = image.getchannel("A").tobytes()
    width, height = image.size

    smask = pikepdf.Stream(pdf, zlib.compress(alpha))
    smask.Type = pikepdf.Name.XObject
    smask.Subtype = pikepdf.Name.Image
    smask.Width, smask.Height = width, height
    smask.ColorSpace = pikepdf.Name.DeviceGray
    smask.BitsPerComponent = 8
    smask.Filter = pikepdf.Name.FlateDecode

    xobj = pikepdf.Stream(pdf, zlib.compress(rgb))
    xobj.Type = pikepdf.Name.XObject
    xobj.Subtype = pikepdf.Name.Image
    xobj.Width, xobj.Height = width, height
    xobj.ColorSpace = pikepdf.Name.DeviceRGB
    xobj.BitsPerComponent = 8
    xobj.Filter = pikepdf.Name.FlateDecode
    xobj.SMask = pdf.make_indirect(smask)
    return pdf.make_indirect(xobj)


# A signature is drawn larger than the line it sits on, the way a hand does
# it. Its height is a multiple of the field's, within a range that keeps a
# 13pt line from producing a 13pt signature and a tall box from producing a
# poster, and it is never wider than the field.
_SIG_HEIGHT_FACTOR = 2.6
_SIG_MIN_PT, _SIG_MAX_PT = 22.0, 46.0
_SIG_INSET_PT = 3.0


def fit_signature(rect, aspect: float, *, exact: bool = False):
    """Where a signature of the given aspect lands over `rect`: (x, y, w, h).

    A form field's rectangle is the thin line a signature sits on, so the
    image is drawn a multiple of its height. A rectangle made for a printed
    line, or placed by hand, already is the intended size, and `exact` uses
    it as is. Shared with the preview, so what is shown is what is saved.
    """
    x0, y0, x1, y1 = (float(v) for v in rect)
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    box_w, box_h = x1 - x0, y1 - y0
    height = box_h if exact else max(_SIG_MIN_PT, min(_SIG_MAX_PT, box_h * _SIG_HEIGHT_FACTOR))
    width = height * aspect
    if width > box_w - 2 * _SIG_INSET_PT:
        width = max(1.0, box_w - 2 * _SIG_INSET_PT)
        height = width / aspect
    return x0 + _SIG_INSET_PT, y0 + 1.0, width, height


def _draw_image(pdf: pikepdf.Pdf, page, rect, png: bytes, *, exact: bool = False) -> None:
    """Draw the image over a rectangle in PDF user space."""
    xobj = _image_xobject(pdf, png)
    x, y, width, height = fit_signature(rect, float(xobj.Width) / float(xobj.Height),
                                        exact=exact)

    name = page.add_resource(xobj, pikepdf.Name.XObject, prefix="OmaSig")
    page.contents_add(pikepdf.Stream(
        pdf, f"q {width:.2f} 0 0 {height:.2f} {x:.2f} {y:.2f} cm {name} Do Q".encode()),
        prepend=False)


def _draw_text(pdf: pikepdf.Pdf, page, rect, text: str, size: float = 10.0) -> None:
    """Write text on the page, for a printed line that has no field behind it."""
    x0, y0, _x1, y1 = (float(v) for v in rect)
    font = pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                              BaseFont=pikepdf.Name.Helvetica,
                              Encoding=pikepdf.Name.WinAnsiEncoding)
    name = page.add_resource(pdf.make_indirect(font), pikepdf.Name.Font, prefix="OmaF")
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    baseline = min(y0, y1) + 2.0
    page.contents_add(pikepdf.Stream(
        pdf, f"BT {name} {size:.1f} Tf {x0 + 2:.2f} {baseline:.2f} Td ({escaped}) Tj ET"
        .encode("latin-1", "replace")), prepend=False)


def _draw_tick(pdf: pikepdf.Pdf, page, rect) -> None:
    """A check mark drawn as a path, for a box the user ticked by hand."""
    x0, y0, x1, y1 = (float(v) for v in rect)
    w, h = x1 - x0, y1 - y0
    path = (f"q 0 0 0 RG {max(1.0, h * 0.16):.2f} w 1 J 1 j "
            f"{x0 + w * 0.22:.2f} {y0 + h * 0.45:.2f} m "
            f"{x0 + w * 0.44:.2f} {y0 + h * 0.22:.2f} l "
            f"{x0 + w * 0.80:.2f} {y0 + h * 0.75:.2f} l S Q")
    page.contents_add(pikepdf.Stream(pdf, path.encode()), prepend=False)


def _place_signature(pdf: pikepdf.Pdf, page, annot, png: bytes,
                     offset: tuple[float, float] = (0.0, 0.0)) -> None:
    """Draw the image over the field's rectangle and retire the widget.

    Retired, not left underneath: USCIS paints its fields with a pale blue
    background, and a widget that stays would sit on top of the ink.
    """
    x0, y0, x1, y1 = (float(v) for v in annot.Rect)
    dx, dy = offset
    _draw_image(pdf, page, (x0 + dx, y0 + dy, x1 + dx, y1 + dy), png)

    # Drop the widget from the page, and the field from the form.
    page.Annots = pikepdf.Array([a for a in page.Annots if a.objgen != annot.objgen])
    acro = pdf.Root.get("/AcroForm")
    field = annot if "/T" in annot else annot.get("/Parent")
    if acro is not None and "/Fields" in acro and field is not None:
        acro.Fields = pikepdf.Array(
            [f for f in acro.Fields if f.objgen != field.objgen])
    if "/Parent" in annot and "/Kids" in annot.Parent:
        annot.Parent.Kids = pikepdf.Array(
            [k for k in annot.Parent.Kids if k.objgen != annot.objgen])


# A signature drawn on a printed line, and the date beside it.
# Shorter than a signature over a field: a printed line has a sentence of
# instructions a few points above it, and a tall signature runs into it.
_PRINTED_SIG_HEIGHT = 22.0
_PRINTED_DATE_HEIGHT = 12.0
_PRINTED_SIG_MAX_W = 260.0
_PRINTED_DATE_MAX_W = 150.0


def _printed_signature_lines(pages: list[textmap.PageText],
                             blanks: list[Blank]) -> list[Blank]:
    """Blanks for signature lines that are printed text with no field behind them.

    The W-9's "Sign Here" block is a label, an arrow and a rule: nothing to fill.
    A page that already has a real signature field is left alone, since that
    field is the right place and a second signature on the page would be worse
    than none. The blank's rectangle lives in `native` as PDF coordinates, and
    the writer draws straight onto the page for these.
    """
    out: list[Blank] = []
    for page_text in pages:
        n = page_text.number
        has_real = any(b.page == n and "signature" in
                       (b.label.native_name + " " + b.label.left).lower()
                       and "date" not in b.label.native_name.lower()
                       for b in blanks)
        if has_real:
            continue
        for i, line in enumerate(page_text.lines):
            sig = next((w for w in line.words if w.text.lower().startswith("signature")), None)
            if sig is None:
                continue
            # The label may wrap onto the next line ("Signature of" / "U.S. person"),
            # and the Date label usually sits on that lower line.
            band = list(line.words)
            bottom = line.box[3]
            if i + 1 < len(page_text.lines):
                nxt = page_text.lines[i + 1]
                # The W-9's two label lines overlap by a couple of points, so
                # the gap here is allowed to be slightly negative.
                if -4 <= nxt.box[1] - line.box[3] <= 6 and abs(nxt.box[0] - line.box[0]) < 60:
                    band += nxt.words
                    bottom = nxt.box[3]
            date = next((w for w in band if w.text.lower().startswith("date")), None)
            # The label starts at the word "Signature", or at a possessive just
            # before it ("Employee's signature"), which the matcher needs to see
            # so that an employer's line is left alone. It takes the words that
            # follow closely, including a wrapped "U.S. person" beneath, and a
            # parenthetical hugging it: the W-4 says "Employee's signature (This
            # form is not valid unless you sign it.)" and all of that is label.
            # The rotated "Sign Here" beside the block sits further left and is
            # not part of it.
            ordered = sorted((w for w in band if w is not date), key=lambda w: w.x0)
            start = sig.x0
            before = [w for w in ordered if w.x1 <= sig.x0 + 1 and sig.x0 - w.x1 <= labeling.COLUMN_GAP
                      and abs(w.y0 - sig.y0) < 4]
            if before:
                start = before[-1].x0
            label_words = [w for w in ordered if start - 1 <= w.x0 <= sig.x1 + 40]
            label_right = max(w.x1 for w in label_words)
            rest = [w for w in ordered if w.x0 > label_right + 1]
            # The window above may already have swallowed the opening bracket
            # of a parenthetical, so look for an unclosed one in the label as
            # well as for one starting just after it.
            opened = (any(w.text.startswith("(") for w in label_words)
                      and not any(w.text.endswith(")") for w in label_words))
            if rest and (opened or rest[0].text.startswith("(")):
                closing = next((k for k, w in enumerate(rest) if w.text.endswith(")")), None)
                if closing is not None and rest[closing].x0 - label_right < 240:
                    label_words += rest[:closing + 1]
                    label_right = max(w.x1 for w in label_words)
                    rest = rest[closing + 1:]
            # A signature line has empty paper after its label. Body text that
            # happens to start with "Signature requirements. The..." does not.
            if rest and rest[0].x0 - label_right < 60:
                continue
            H = page_text.height
            sig_x0 = label_right + 8
            sig_x1 = (date.x0 - 10) if date else min(sig_x0 + _PRINTED_SIG_MAX_W,
                                                     page_text.width - 36)
            if sig_x1 - sig_x0 < 60:
                continue
            base = H - bottom  # PDF y of the line's baseline
            out.append(Blank(
                id=f"p{n}:~signature#{len(out) + 1}", kind=BlankKind.SIGNATURE,
                label=LabelContext(left=" ".join(w.text for w in label_words)),
                page=n, width_pt=sig_x1 - sig_x0, height_pt=_PRINTED_SIG_HEIGHT,
                rect=(sig_x0, base, sig_x1, base + _PRINTED_SIG_HEIGHT),
                native={"synthetic": True,
                        "rect": (sig_x0, base, sig_x1, base + _PRINTED_SIG_HEIGHT)}))
            if date:
                d_x0 = date.x1 + 8
                d_x1 = min(d_x0 + _PRINTED_DATE_MAX_W, page_text.width - 36)
                out.append(Blank(
                    id=f"p{n}:~date#{len(out) + 1}", kind=BlankKind.DATE,
                    label=LabelContext(left="Date"), page=n,
                    width_pt=d_x1 - d_x0, height_pt=_PRINTED_DATE_HEIGHT,
                    rect=(d_x0, base, d_x1, base + _PRINTED_DATE_HEIGHT),
                    native={"synthetic": True,
                            "rect": (d_x0, base, d_x1, base + _PRINTED_DATE_HEIGHT)}))
            break  # one signature block per page
    return out


_FF_COMB = 1 << 24
_DA_RE = __import__("re").compile(r"/([A-Za-z0-9_.+-]+)\s+([\d.]+)\s+Tf")


def _comb_appearance(pdf: pikepdf.Pdf, annot, field) -> None:
    """Draw one character per cell for a comb field, as the form intends.

    qpdf's appearance generator writes the value as one run of text in the
    first cell, so a flattened W-9 showed "3456789" jammed into the EIN's
    third box. This builds the stream by hand: the field's own font and size
    from /DA where the form's /DR has it, each glyph centred in its cell.
    """
    value = str(field.get("/V", ""))
    max_len = int(field.get("/MaxLen", 0) or 0)
    if not value or max_len <= 0:
        return
    x0, y0, x1, y1 = (float(v) for v in annot.Rect)
    w, h = abs(x1 - x0), abs(y1 - y0)
    da = str(_inherited(annot, "/DA") or pdf.Root.AcroForm.get("/DA", "") or "")
    match = _DA_RE.search(da)
    font, size = (match.group(1), float(match.group(2))) if match else ("Helv", 9.0)
    if size <= 0:
        size = min(9.0, h * 0.7)
    resources = pdf.Root.AcroForm.get("/DR", pikepdf.Dictionary())
    fonts = resources.get("/Font", pikepdf.Dictionary())
    if f"/{font}" not in fonts:
        font = "Helv" if "/Helv" in fonts else next((str(k)[1:] for k in fonts.keys()), None)
        if font is None:
            return
    cell = w / max_len
    glyph = size * 0.556  # a Helvetica digit; near enough for letters too
    baseline = (h - size * 0.72) / 2
    parts = [f"/Tx BMC q BT /{font} {size:.2f} Tf 0 g"]
    for i, ch in enumerate(value[:max_len]):
        x = i * cell + (cell - glyph) / 2
        escaped = ch.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        parts.append(f"1 0 0 1 {x:.2f} {baseline:.2f} Tm ({escaped}) Tj")
    parts.append("ET Q EMC")
    stream = pikepdf.Stream(pdf, " ".join(parts).encode("latin-1", "replace"))
    stream.Type = pikepdf.Name.XObject
    stream.Subtype = pikepdf.Name.Form
    stream.BBox = pikepdf.Array([0, 0, w, h])
    stream.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary({f"/{font}": fonts[f"/{font}"]}))
    annot.AP = pikepdf.Dictionary(N=pdf.make_indirect(stream))


def lock(pdf: pikepdf.Pdf) -> None:
    """Burn every field into the page and remove the form.

    What was signed can no longer be edited by whoever receives it. It also
    means nobody else can fill their part, so an I-9, whose employer still has
    a section to complete, should not be locked.
    """
    pdf.generate_appearance_streams()
    for _index, _page, annot, ft, _name, _blank_id in list(_widgets(pdf)):
        field = annot if "/T" in annot else annot.get("/Parent", annot)
        flags = int(_inherited(annot, "/Ff") or 0)
        if ft == "/Tx" and flags & _FF_COMB:
            _comb_appearance(pdf, annot, field)
    pdf.flatten_annotations("all")
    if "/AcroForm" in pdf.Root:
        del pdf.Root["/AcroForm"]


def _flat_blanks(pdf: pikepdf.Pdf, pages: list[textmap.PageText],
                 blanks: list[Blank]) -> list[Blank]:
    """Rule-and-underscore blanks, on pages that have no fields of their own."""
    from .. import flat

    out: list[Blank] = []
    if any(not isinstance(b.native, dict) for b in blanks):
        # A form that made real fields anywhere is a real form; its other
        # pages are instructions, and the rules on them are not blanks.
        return out
    from .. import scan

    for index, page_text in enumerate(pages):
        n = index + 1
        taken = [b.rect for b in blanks if b.page == n and b.rect is not None]
        extra: list = []
        scanned = False
        if scan.looks_scanned(page_text) and scan.available():
            # A picture of a page: words from OCR, rules from the pixels.
            try:
                page_text, extra = scan.read_page(pdf.filename, n, page_text.width,
                                                  page_text.height)
                pages[index] = page_text
                scanned = True
            except (OSError, RuntimeError, subprocess.CalledProcessError,
                    subprocess.TimeoutExpired):
                continue
        out.extend(flat.blanks_for(index, pdf.pages[index], page_text, taken,
                                   extra_rules=extra, scanned=scanned))
    return out


def fingerprint(pdf: pikepdf.Pdf, blanks: list[Blank], names: list[str]) -> str:
    """What makes two files the same form, for remembering how it was filled.

    Field names alone are not enough: every flat PDF and scan has none, and two
    different forms can share names like "f1" with different labels, which let
    one form's memory fill another's employer box. So the fingerprint covers
    the pages (size and drawn content), and every blank's place, kind and
    label. A revised edition, or a different form, is a different fingerprint.
    """
    h = hashlib.sha256()
    h.update(f"pages:{len(pdf.pages)}".encode())
    seen: set = set()

    def stream_digest(stream) -> None:
        try:
            h.update(hashlib.sha256(stream.read_raw_bytes()).digest())
        except pikepdf.PdfError:
            h.update(b"unreadable")

    def resources(res, depth: int) -> None:
        # What the page draws through: a heading can live in a Form XObject
        # that the page content only names ("/Fm0 Do"), so the same page
        # stream can show an employee's section or an employer's.
        if depth > 8 or not isinstance(res, pikepdf.Dictionary):
            return
        xobjects = res.get("/XObject")
        if not isinstance(xobjects, pikepdf.Dictionary):
            return
        for name in sorted(xobjects.keys()):
            xobj = xobjects[name]
            if not isinstance(xobj, pikepdf.Stream) or xobj.objgen in seen:
                continue
            if xobj.objgen != (0, 0):
                seen.add(xobj.objgen)
            h.update(f"|x:{name}".encode())
            stream_digest(xobj)
            if xobj.get("/Subtype") == "/Form":
                resources(xobj.get("/Resources"), depth + 1)

    for page in pdf.pages:
        box = [round(float(v)) for v in page.mediabox]
        h.update(f"|box:{box}|rot:{int(page.obj.get('/Rotate', 0) or 0)}".encode())
        contents = page.obj.get("/Contents")
        streams = contents if isinstance(contents, pikepdf.Array) else [contents]
        for stream in streams:
            if isinstance(stream, pikepdf.Stream):
                stream_digest(stream)
        resources(page.obj.get("/Resources"), 0)
    for name in sorted(names):
        h.update(f"|name:{name}".encode())
    for b in sorted(blanks, key=lambda b: b.id):
        rect = [round(v) for v in b.rect] if b.rect else None
        label = b.label
        # Everything the matcher decides ownership from, not just the label.
        h.update(f"|{b.id}|{b.kind.value}|{rect}|{label.best()}|{label.section}"
                 f"|{label.row}|{label.inside}|{label.native_name}".encode())
    return f"pdf-{h.hexdigest()[:24]}"


class PdfAdapter:
    fmt = "pdf"
    extensions = (".pdf",)

    def discover(self, path: str) -> Document:
        pdf = pikepdf.open(path)
        pages = textmap.read_pdf(path)

        blanks: list[Blank] = []
        boxes: dict[str, textmap.Box] = {}
        names: list[str] = []

        for index, page, annot, ft, name, blank_id in _widgets(pdf):
            page_text = pages[index] if index < len(pages) else None
            height = float(page.MediaBox[3]) - float(page.MediaBox[1])
            if True:
                flags = int(_inherited(annot, "/Ff") or 0)
                tooltip = str(_inherited(annot, "/TU") or "")
                names.append(f"{index + 1}:{name}")

                box = textmap.from_pdf_rect(
                    tuple(float(v) for v in annot.Rect), height)  # type: ignore[arg-type]
                label = (labeling.describe(page_text, box, name)
                         if page_text is not None else None)
                if label is None:
                    label = LabelContext(native_name=name)
                # A tooltip, when a form sets one, is authoritative and
                # *replaces* the geometric guess rather than sitting beside it.
                # USCIS tags its forms for accessibility and so does supply
                # them; the IRS does not. Keeping both was a real bug: the I-9's
                # "Enter Apartment Number" box picked up "City or Town" from the
                # next column and filled the city into it.
                if tooltip:
                    label.left = tooltip
                    label.above = ""
                    label.inside = ""
                    label.authoritative = True

                max_len = _inherited(annot, "/MaxLen")
                rx0, ry0, rx1, ry1 = (float(v) for v in annot.Rect)
                blank = Blank(
                    id=blank_id,
                    kind=_kind(ft, flags),
                    label=label,
                    page=index + 1,
                    width_pt=box[2] - box[0],
                    height_pt=box[3] - box[1],
                    max_chars=int(max_len) if max_len is not None else None,
                    options=_options(annot),
                    readonly=bool(flags & _FF_READONLY),
                    rect=(min(rx0, rx1), min(ry0, ry1), max(rx0, rx1), max(ry0, ry1)),
                    native=annot,
                )
                blanks.append(blank)
                boxes[blank_id] = box

        _group_runs(blanks, boxes)
        blanks.extend(_printed_signature_lines(pages, blanks))
        blanks.extend(_flat_blanks(pdf, pages, blanks))

        return Document(path=path, fmt=self.fmt, page_count=len(pdf.pages),
                        blanks=blanks, fingerprint=fingerprint(pdf, blanks, names),
                        native=pdf)

    def write(self, doc: Document, values: dict[str, str], out_path: str,
              images: dict[str, bytes] | None = None, *, lock_form: bool = False) -> None:
        """Write values and images into a fresh copy of the source document.

        Fresh, not the document discover() opened: writing mutates the PDF, and
        a second press of Fill on the same open document would otherwise stamp
        the signature twice. Widgets are found again by the same id walk that
        named them.

        Text values are set on the field objects with /NeedAppearances raised,
        which tells the viewer to draw them. Images are drawn into the page
        content over the field's rectangle and the field is retired.
        """
        images = images or {}
        pdf = pikepdf.open(doc.path)
        kinds = {b.id: b for b in doc.blanks}

        for blank in doc.blanks:
            native = blank.native
            if not (isinstance(native, dict) and native.get("synthetic")):
                continue
            page = pdf.pages[blank.page - 1]
            rect = blank.shifted(native["rect"])
            if blank.id in images:
                _draw_image(pdf, page, rect, images[blank.id], exact=True)
            elif blank.id in values and blank.kind is BlankKind.CHECKBOX:
                _draw_tick(pdf, page, rect)
            elif blank.id in values:
                _draw_text(pdf, page, rect, values[blank.id])

        for _index, page, annot, _ft, _name, blank_id in list(_widgets(pdf)):
            if blank_id in images:
                owner = kinds.get(blank_id)
                _place_signature(pdf, page, annot, images[blank_id],
                                 owner.offset if owner is not None else (0.0, 0.0))
                continue
            if blank_id not in values:
                continue
            blank = kinds.get(blank_id)
            if blank is None:
                continue
            text = values[blank_id]
            target = annot if "/T" in annot else (annot.get("/Parent") or annot)
            if blank.kind in (BlankKind.TEXT, BlankKind.MULTILINE, BlankKind.DATE):
                target["/V"] = pikepdf.String(text)
                if "/AP" in annot:
                    del annot["/AP"]  # stale appearance would mask the new value
            elif blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
                on = blank.options[0] if blank.options else "/Yes"
                state = pikepdf.Name(on if on.startswith("/") else f"/{on}")
                target["/V"] = state
                annot["/AS"] = state

        acro = pdf.Root.get("/AcroForm")
        if acro is not None:
            acro["/NeedAppearances"] = True
        if lock_form:
            lock(pdf)
        pdf.save(out_path)


def _adapters():
    from .docx import DocxAdapter
    return (PdfAdapter(), DocxAdapter())


ADAPTERS = ()


def for_path(path: str):
    global ADAPTERS
    if not ADAPTERS:
        ADAPTERS = _adapters()
    lower = path.lower()
    for adapter in ADAPTERS:
        if lower.endswith(adapter.extensions):
            return adapter
    raise ValueError(f"no adapter for {path}; PDF and DOCX are supported")
