"""DOCX adapter: the four kinds of blank a Word form has.

A .docx is a zip of XML, and the body is a text stream in reading order, so
the label for a blank is the text just before it. No geometry. The four
flavours, from the most deliberate to the most common:

1. Content controls (`w:sdt`), with an alias or tag that names the blank.
2. Legacy form fields (`FORMTEXT`, `FORMCHECKBOX`) from the old Developer tab.
3. Table cells: an empty cell beside or below a cell with a label in it.
4. Underscore runs in body text: `Name: ________`.

Read and written with the standard library. minidom rather than ElementTree
because it keeps every namespace declaration exactly as Word wrote it; Word
refuses a document whose `mc:Ignorable` names a prefix that is no longer
declared, and ElementTree drops declarations it did not use. Everything else
in the zip is copied through byte for byte.

There is no fixed layout, so nothing is drawn here. The window's page view
shows a PDF made by LibreOffice, read-only; the field list is the interface.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from pathlib import Path
from xml.dom import minidom

from ..model import Blank, BlankKind, Document, LabelContext

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W14 = "http://schemas.microsoft.com/office/word/2010/wordml"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
DOC_XML = "word/document.xml"
RELS_XML = "word/_rels/document.xml.rels"
TYPES_XML = "[Content_Types].xml"

UNDERSCORES = re.compile(r"_{3,}")
UNDERSCORE_PT = 5.5      # about the width of one underscore at 11pt
EMU_PER_PT = 12700
SIGNATURE_HEIGHT_PT = 28.0

CHECK_ON, CHECK_OFF = "☒", "☐"


# -- reading -----------------------------------------------------------------

def _text(node) -> str:
    """All the w:t text under a node, in order, with tabs as spaces."""
    out = []
    for el in node.getElementsByTagNameNS(W, "t"):
        out.append("".join(c.data for c in el.childNodes if c.nodeType == c.TEXT_NODE))
    for _ in node.getElementsByTagNameNS(W, "tab"):
        out.append(" ")
    return "".join(out)


def _children(node, ns: str, name: str):
    return [c for c in node.childNodes if c.nodeType == c.ELEMENT_NODE
            and c.namespaceURI == ns and c.localName == name]


def _is_heading(paragraph) -> bool:
    for ppr in _children(paragraph, W, "pPr"):
        for style in _children(ppr, W, "pStyle"):
            val = style.getAttributeNS(W, "val") or style.getAttribute("w:val")
            if val.lower().startswith(("heading", "title")):
                return True
    text = _text(paragraph).strip()
    return bool(text) and len(text) < 60 and text.upper() == text and any(c.isalpha() for c in text)


class _Walk:
    """The body in reading order, remembering what came before."""

    def __init__(self, dom) -> None:
        self.dom = dom
        self.blanks: list[Blank] = []
        self.targets: dict[str, tuple] = {}     # blank id -> how to write it
        self.section = ""
        self.above = ""                          # the previous paragraph's text
        self.counter = 0

    def run(self) -> None:
        body = self.dom.getElementsByTagNameNS(W, "body")[0]
        self._block(body, in_table=False)

    def _block(self, container, in_table: bool) -> None:
        for child in container.childNodes:
            if child.nodeType != child.ELEMENT_NODE or child.namespaceURI != W:
                continue
            if child.localName == "p":
                self._paragraph(child, in_table)
            elif child.localName == "tbl":
                self._table(child)
            elif child.localName == "sdt":
                self._sdt(child, container_text="")
                # A block-level control may wrap paragraphs of its own.
            elif child.localName in ("sdtContent", "smartTag", "customXml", "ins"):
                self._block(child, in_table)

    def _paragraph(self, paragraph, in_table: bool) -> None:
        text = _text(paragraph)
        # Inline controls and legacy fields first, since they sit in the runs.
        for sdt in paragraph.getElementsByTagNameNS(W, "sdt"):
            self._sdt(sdt, container_text=text)
        self._legacy_fields(paragraph, text)
        if not in_table:
            self._underscores(paragraph, text)
        if _is_heading(paragraph):
            self.section = text.strip()
        if text.strip():
            self.above = text.strip()

    def _next_id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}-{self.counter}"

    def _sdt(self, sdt, container_text: str) -> None:
        props = _children(sdt, W, "sdtPr")
        if not props:
            return
        props = props[0]
        alias = tag = ""
        for el in _children(props, W, "alias"):
            alias = el.getAttribute("w:val")
        for el in _children(props, W, "tag"):
            tag = el.getAttribute("w:val")
        name = alias or tag
        content = _children(sdt, W, "sdtContent")
        current = _text(content[0]).strip() if content else ""
        if props.getElementsByTagNameNS(W14, "checkbox"):
            kind = BlankKind.CHECKBOX
            current = "checked" if current == CHECK_ON else ""
        elif _children(props, W, "date"):
            kind = BlankKind.DATE
        elif _children(props, W, "picture"):
            kind = BlankKind.SIGNATURE
        else:
            kind = BlankKind.TEXT
        # Placeholder text (the grey "Click here to enter text") is not a value.
        if _children(props, W, "showingPlcHdr"):
            current = ""
        own = _text(sdt)
        before, _, after = container_text.partition(own) if own else (container_text, "", "")
        if kind is BlankKind.CHECKBOX:
            # A box's words follow it, the way a printed form reads.
            label = LabelContext(right=after.strip(" :\t"), inside=name if name and
                                 name.lower() not in after.lower() else "",
                                 above=self.above, section=self.section,
                                 native_name=tag, authoritative=bool(name))
            if not label.right and name:
                label.right = name
        else:
            label = LabelContext(left=before.strip(" :\t") if not name else "",
                                 inside=name, above=self.above, section=self.section,
                                 native_name=tag, authoritative=bool(name))
        blank_id = self._next_id("sdt")
        self.blanks.append(Blank(id=blank_id, kind=kind, label=label,
                                 width_pt=200.0, value=current or None))
        self.targets[blank_id] = ("sdt", sdt)

    def _legacy_fields(self, paragraph, text: str) -> None:
        runs = paragraph.getElementsByTagNameNS(W, "r")
        i = 0
        seen_text = ""
        while i < len(runs):
            run = runs[i]
            seen_text += _text(run)
            chars = _children(run, W, "fldChar")
            if not chars or chars[0].getAttribute("w:fldCharType") != "begin":
                i += 1
                continue
            # begin, instr..., separate, result..., end
            instr = ""
            result_runs = []
            j = i + 1
            phase = "instr"
            while j < len(runs):
                r = runs[j]
                fc = _children(r, W, "fldChar")
                if fc:
                    t = fc[0].getAttribute("w:fldCharType")
                    if t == "separate":
                        phase = "result"
                    elif t == "end":
                        break
                elif phase == "instr":
                    for it in _children(r, W, "instrText"):
                        instr += "".join(c.data for c in it.childNodes if c.nodeType == c.TEXT_NODE)
                else:
                    result_runs.append(r)
                j += 1
            ffdata = chars[0].getElementsByTagNameNS(W, "ffData")
            name = ""
            if ffdata:
                for n in _children(ffdata[0], W, "name"):
                    name = n.getAttribute("w:val")
            upper = instr.upper()
            if "FORMTEXT" in upper or "FORMCHECKBOX" in upper:
                is_box = "FORMCHECKBOX" in upper
                current = "".join(_text(r) for r in result_runs).strip()
                if is_box:
                    current = ""
                    if ffdata:
                        for cb in ffdata[0].getElementsByTagNameNS(W, "checkBox"):
                            for d in _children(cb, W, "checked") + _children(cb, W, "default"):
                                if d.getAttribute("w:val") not in ("0", "false"):
                                    current = "checked"
                elif current.replace(" ", "").strip() == "":
                    current = ""   # the five en-spaces of an empty field
                blank_id = self._next_id("fld")
                # A generic Word name (Text1, Check1) says nothing; a chosen one does.
                named = name and not re.fullmatch(r"(Text|Check|Dropdown)\d*", name)
                after = "".join(_text(r) for r in runs[j + 1:]).strip(" :\t")
                self.blanks.append(Blank(
                    id=blank_id,
                    kind=BlankKind.CHECKBOX if is_box else BlankKind.TEXT,
                    label=LabelContext(left="" if is_box else seen_text.strip(" :\t"),
                                       right=after if is_box else "",
                                       inside=name if named else "",
                                       above=self.above, section=self.section,
                                       native_name=name, authoritative=bool(named)),
                    width_pt=150.0, value=current or None))
                self.targets[blank_id] = ("field", chars[0], result_runs, is_box)
            i = j + 1

    def _underscores(self, paragraph, text: str) -> None:
        last_end = 0
        for m in UNDERSCORES.finditer(text):
            before = text[last_end:m.start()].strip(" :\t")
            n = len(m.group())
            blank_id = self._next_id("line")
            label = LabelContext(left=before, above=self.above if not before else "",
                                 section=self.section)
            self.blanks.append(Blank(id=blank_id, kind=BlankKind.TEXT, label=label,
                                     width_pt=n * UNDERSCORE_PT, max_chars=int(n * 1.6)))
            self.targets[blank_id] = ("span", paragraph, m.start(), m.end())
            last_end = m.end()

    def _table(self, table) -> None:
        rows = _children(table, W, "tr")
        grid: list[list] = []
        for row in rows:
            grid.append(_children(row, W, "tc"))
        texts = [[_text(c).strip() for c in row] for row in grid]
        for ri, row in enumerate(grid):
            for ci, cell in enumerate(row):
                if texts[ri][ci]:
                    # Cells with their own controls or lines are read as prose.
                    self._block(cell, in_table=True)
                    self._cell_lines(cell)
                    continue
                left = texts[ri][ci - 1] if ci > 0 else ""
                above = texts[ri - 1][ci] if ri > 0 and ci < len(texts[ri - 1]) else ""
                if not left and not above:
                    continue
                if left and UNDERSCORES.search(left):
                    continue
                blank_id = self._next_id("cell")
                self.blanks.append(Blank(
                    id=blank_id, kind=BlankKind.TEXT,
                    label=LabelContext(left=left, above=above, section=self.section),
                    width_pt=180.0))
                self.targets[blank_id] = ("cell", cell)
            for cell in row:
                if _text(cell).strip():
                    self.above = _text(cell).strip()

    def _cell_lines(self, cell) -> None:
        for paragraph in cell.getElementsByTagNameNS(W, "p"):
            self._underscores(paragraph, _text(paragraph))


# -- writing -----------------------------------------------------------------

def _first_rpr(scope):
    """A copy of the first run properties in scope, so typed text matches."""
    for rpr in scope.getElementsByTagNameNS(W, "rPr"):
        if rpr.parentNode is not None and rpr.parentNode.localName == "r":
            return rpr.cloneNode(True)
    return None


def _make_run(dom, text: str, rpr=None):
    run = dom.createElementNS(W, "w:r")
    if rpr is not None:
        run.appendChild(rpr)
    t = dom.createElementNS(W, "w:t")
    t.setAttribute("xml:space", "preserve")
    t.appendChild(dom.createTextNode(text))
    run.appendChild(t)
    return run


def _set_span(paragraph, start: int, end: int, replacement: str) -> None:
    """Replace characters [start, end) of a paragraph's text across its w:t nodes."""
    pos = 0
    first = True
    for t in paragraph.getElementsByTagNameNS(W, "t"):
        text = "".join(c.data for c in t.childNodes if c.nodeType == c.TEXT_NODE)
        lo, hi = pos, pos + len(text)
        pos = hi
        if hi <= start or lo >= end:
            continue
        a, b = max(start, lo) - lo, min(end, hi) - lo
        new = text[:a] + (replacement if first else "") + text[b:]
        first = False
        for c in list(t.childNodes):
            t.removeChild(c)
        t.appendChild(t.ownerDocument.createTextNode(new))
        t.setAttribute("xml:space", "preserve")


def _clear_content(node) -> None:
    """Drop every run under a content node, keeping paragraph structure."""
    for r in list(node.getElementsByTagNameNS(W, "r")):
        if r.parentNode is not None:
            r.parentNode.removeChild(r)


def _content_paragraph(node):
    ps = node.getElementsByTagNameNS(W, "p")
    return ps[0] if ps else node


def _insert_run_at(paragraph, offset: int, new_run) -> None:
    """Put a run at a character offset in a paragraph, so a picture lands
    where its line was rather than at the end of the paragraph.

    The run holding the offset is split there: a text node is cut in two if
    the offset falls inside it, and then everything after the cut moves to a
    new run with the same formatting. Children are moved, not copied, so a
    tab or break is kept exactly once, on whichever side it was.
    """
    pos = 0
    for t in list(paragraph.getElementsByTagNameNS(W, "t")):
        text = "".join(c.data for c in t.childNodes if c.nodeType == c.TEXT_NODE)
        lo, hi = pos, pos + len(text)
        pos = hi
        # At a boundary between two text nodes, the later one is chosen, so
        # the picture goes before the text that followed the line.
        if not (lo <= offset < hi or (offset == hi and t is _last_t(paragraph))):
            continue
        run = t.parentNode
        if run is None or run.localName != "r":
            continue
        cut = offset - lo
        if 0 < cut < len(text):
            rest = t.cloneNode(False)
            _replace_text(rest, text[cut:])
            _replace_text(t, text[:cut])
            run.insertBefore(rest, t.nextSibling)
            split_before = rest
        elif cut == 0:
            split_before = t
        else:
            split_before = t.nextSibling
        _split_run(run, split_before, new_run)
        return
    paragraph.appendChild(new_run)


def _last_t(paragraph):
    texts = paragraph.getElementsByTagNameNS(W, "t")
    return texts[len(texts) - 1] if texts.length else None


def _split_run(run, split_before, new_run) -> None:
    """Move `split_before` and everything after it to a new run after `run`,
    and put `new_run` between the two. Formatting (w:rPr) goes to both."""
    parent = run.parentNode
    content = [c for c in run.childNodes
               if not (c.nodeType == c.ELEMENT_NODE and c.localName == "rPr")]
    if split_before is None or split_before not in content:
        parent.insertBefore(new_run, run.nextSibling)
        return
    index = content.index(split_before)
    if index == 0:
        parent.insertBefore(new_run, run)
        return
    tail = run.cloneNode(False)
    for rpr in _children(run, W, "rPr"):
        tail.appendChild(rpr.cloneNode(True))
    for child in content[index:]:
        tail.appendChild(run.removeChild(child))
    parent.insertBefore(tail, run.nextSibling)
    parent.insertBefore(new_run, tail)


def _replace_text(t, text: str) -> None:
    for c in list(t.childNodes):
        t.removeChild(c)
    t.appendChild(t.ownerDocument.createTextNode(text))
    t.setAttribute("xml:space", "preserve")


class _Writer:
    def __init__(self, dom, rels: str, types: str, existing=frozenset()) -> None:
        self.dom = dom
        # Edited as XML, not as strings: a legal empty part is written as
        # <Relationships .../>, which has no closing tag to splice before.
        self.rels_dom = minidom.parseString(rels.encode("utf-8"))
        self.types_dom = minidom.parseString(types.encode("utf-8"))
        self.existing = set(existing)       # part names already in the package
        self.media: dict[str, bytes] = {}
        self.image_count = 0

    @property
    def rels(self) -> str:
        return self.rels_dom.toxml()

    @property
    def types(self) -> str:
        return self.types_dom.toxml()

    def _new_image_part(self) -> tuple[str, str]:
        """An unused relationship id and media name, even in a document that
        Omaform signed before."""
        root = self.rels_dom.documentElement
        ids = {el.getAttribute("Id") for el in root.getElementsByTagName("*")}
        while True:
            self.image_count += 1
            rid = f"rIdOmaform{self.image_count}"
            part = f"word/media/omaform{self.image_count}.png"
            if rid not in ids and part not in self.existing and part not in self.media:
                return rid, part

    def text(self, target, value: str) -> None:
        kind = target[0]
        if kind == "sdt":
            sdt = target[1]
            content = _children(sdt, W, "sdtContent")
            props = _children(sdt, W, "sdtPr")[0]
            for p in _children(props, W, "showingPlcHdr"):
                props.removeChild(p)
            if not content:
                return
            box = content[0]
            rpr = _first_rpr(box)
            holder = _content_paragraph(box)
            _clear_content(box)
            holder.appendChild(_make_run(self.dom, value, rpr))
        elif kind == "field":
            _, begin, result_runs, is_box = target
            if is_box:
                self.tick(target, value == "checked")
                return
            if not result_runs:
                return
            _set_span_runs(result_runs, value)
        elif kind == "span":
            _, paragraph, start, end = target
            _set_span(paragraph, start, end, value)
        elif kind == "cell":
            cell = target[1]
            holder = _content_paragraph(cell)
            rpr = _first_rpr(cell.parentNode) if cell.parentNode else None
            holder.appendChild(_make_run(self.dom, value, rpr))

    def tick(self, target, on: bool) -> None:
        kind = target[0]
        if kind == "sdt":
            sdt = target[1]
            props = _children(sdt, W, "sdtPr")[0]
            for cb in props.getElementsByTagNameNS(W14, "checkbox"):
                for c in cb.getElementsByTagNameNS(W14, "checked"):
                    c.setAttribute("w14:val", "1" if on else "0")
            content = _children(sdt, W, "sdtContent")
            if content:
                for t in content[0].getElementsByTagNameNS(W, "t"):
                    for c in list(t.childNodes):
                        t.removeChild(c)
                    t.appendChild(self.dom.createTextNode(CHECK_ON if on else CHECK_OFF))
        elif kind == "field":
            begin = target[1]
            for cb in begin.getElementsByTagNameNS(W, "checkBox"):
                for old in _children(cb, W, "checked") + _children(cb, W, "default"):
                    cb.removeChild(old)
                checked = self.dom.createElementNS(W, "w:checked")
                checked.setAttribute("w:val", "1" if on else "0")
                cb.appendChild(checked)

    def image(self, target, png: bytes, width_pt: float) -> None:
        """A picture where the blank was: a signature on its line."""
        w_px, h_px = _png_size(png)
        aspect = w_px / max(1, h_px)
        height = SIGNATURE_HEIGHT_PT
        width = min(width_pt or height * aspect, height * aspect)
        height = width / aspect
        rid, part = self._new_image_part()
        self.media[part] = png
        root = self.rels_dom.documentElement
        rel = self.rels_dom.createElementNS(
            root.namespaceURI, root.tagName.replace("Relationships", "Relationship"))
        rel.setAttribute("Id", rid)
        rel.setAttribute("Type", f"{R}/image")
        rel.setAttribute("Target", part.removeprefix("word/"))
        root.appendChild(rel)
        types_root = self.types_dom.documentElement
        if not any(el.getAttribute("Extension").lower() == "png"
                   for el in types_root.getElementsByTagName("*")):
            default = self.types_dom.createElementNS(
                types_root.namespaceURI, types_root.tagName.replace("Types", "Default"))
            default.setAttribute("Extension", "png")
            default.setAttribute("ContentType", "image/png")
            types_root.appendChild(default)
        drawing = minidom.parseString(_inline_drawing_xml(rid, self.image_count, width, height))
        run = self.dom.createElementNS(W, "w:r")
        run.appendChild(self.dom.importNode(drawing.documentElement, True))

        kind = target[0]
        if kind == "span":
            _, paragraph, start, end = target
            _set_span(paragraph, start, end, "")
            _insert_run_at(paragraph, start, run)
        elif kind == "cell":
            _content_paragraph(target[1]).appendChild(run)
        elif kind == "sdt":
            content = _children(target[1], W, "sdtContent")
            if content:
                _clear_content(content[0])
                _content_paragraph(content[0]).appendChild(run)
        elif kind == "field":
            runs = target[2]
            if runs:
                _set_span_runs(runs, "")
                runs[-1].parentNode.insertBefore(run, runs[-1].nextSibling)


def _set_span_runs(runs, value: str) -> None:
    first = True
    for r in runs:
        for t in r.getElementsByTagNameNS(W, "t"):
            for c in list(t.childNodes):
                t.removeChild(c)
            t.appendChild(t.ownerDocument.createTextNode(value if first else ""))
            t.setAttribute("xml:space", "preserve")
            first = False
    if first and runs:
        t = runs[0].ownerDocument.createElementNS(W, "w:t")
        t.setAttribute("xml:space", "preserve")
        t.appendChild(runs[0].ownerDocument.createTextNode(value))
        runs[0].appendChild(t)


def _png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    w, h = struct.unpack(">II", png[16:24])
    return w, h


def _inline_drawing_xml(rid: str, n: int, width_pt: float, height_pt: float) -> str:
    cx, cy = int(width_pt * EMU_PER_PT), int(height_pt * EMU_PER_PT)
    return (
        f'<w:drawing xmlns:w="{W}" '
        'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
        f'xmlns:r="{R}">'
        f'<wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
        f'<wp:docPr id="{9000 + n}" name="Signature {n}"/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{9000 + n}" name="signature{n}.png"/><pic:cNvPicPr/></pic:nvPicPr>'
        f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
        f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        '</a:graphicData></a:graphic></wp:inline></w:drawing>')


# -- the adapter -------------------------------------------------------------

MAX_BODY_BYTES = 64 * 1024 * 1024


def _read_body(path: str):
    """The document part, parsed, refusing what no Word file contains.

    Every .docx is someone else's file. The body is capped in size before it
    is inflated, and a DOCTYPE is refused outright: Word never writes one, and
    it is the only way to declare the entities an expansion bomb is made of.
    """
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo(DOC_XML)
            if info.file_size > MAX_BODY_BYTES:
                raise ValueError("this Word document's text is too large to read")
            with z.open(info) as fh:
                xml = fh.read(MAX_BODY_BYTES + 1)
    except KeyError as exc:
        raise ValueError("not a Word document: it has no word/document.xml") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError(f"not a Word document: {exc}") from exc
    if len(xml) > MAX_BODY_BYTES:
        raise ValueError("this Word document's text is too large to read")
    # Looked for in the text, not the bytes: a UTF-16 body hides "<!ENTITY"
    # between zero bytes. Decoded the way the XML says it is encoded.
    head = xml[:200]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in head:
        encoding = "utf-16"
    else:
        encoding = "utf-8"
    try:
        decoded = xml.decode(encoding, errors="replace").upper()
    except LookupError:
        decoded = xml.decode("latin-1").upper()
    if "<!DOCTYPE" in decoded or "<!ENTITY" in decoded:
        raise ValueError("this Word document declares entities, which Word never does")
    try:
        dom = minidom.parseString(xml)
    except Exception as exc:  # expat's errors are not one class
        raise ValueError(f"this Word document's text is not valid XML: {exc}") from exc
    if not dom.getElementsByTagNameNS(W, "body"):
        raise ValueError("this Word document has no body")
    return dom


class DocxAdapter:
    fmt = "docx"
    extensions = (".docx",)

    def discover(self, path: str) -> Document:
        dom = _read_body(path)
        walk = _Walk(dom)
        try:
            walk.run()
        except RecursionError as exc:
            raise ValueError("this Word document is nested too deeply to read") from exc
        digest = hashlib.sha256()
        for b in walk.blanks:
            digest.update(f"{b.id}|{b.kind.value}|{b.label.best()}|{b.label.section}"
                          f"|{b.label.inside}|{b.label.native_name}".encode())
        return Document(path=path, fmt=self.fmt, page_count=1, blanks=walk.blanks,
                        fingerprint=f"docx-{digest.hexdigest()[:24]}",
                        native={"targets": walk.targets, "dom": dom})

    def write(self, doc: Document, values: dict[str, str], out_path: str,
              images: dict[str, bytes] | None = None, *, lock_form: bool = False) -> None:
        # Every file this will write, decided and checked before anything is
        # opened for writing. Locking makes a PDF; the Word file it is made
        # from is an intermediate in a temporary folder, never a sibling that
        # could turn out to be the source itself.
        as_pdf = lock_form or out_path.lower().endswith(".pdf")
        final = str(Path(out_path).with_suffix(".pdf")) if as_pdf else out_path
        if os.path.abspath(doc.path) in (os.path.abspath(out_path), os.path.abspath(final)):
            raise ValueError("the output must be a new file; a source is never overwritten")
        # Re-read rather than reuse the discovery DOM, so writing twice from
        # one discovery starts clean both times.
        dom = _read_body(doc.path)
        with zipfile.ZipFile(doc.path) as z:
            names = set(z.namelist())
            rels = (z.read(RELS_XML).decode("utf-8") if RELS_XML in names else
                    '<Relationships xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/relationships"/>')
            types = z.read(TYPES_XML).decode("utf-8")
        walk = _Walk(dom)
        walk.run()
        writer = _Writer(dom, rels, types, names)
        widths = {b.id: b.width_pt for b in walk.blanks}
        kinds = {b.id: b.kind for b in walk.blanks}

        # Edits inside one paragraph's text are made right to left: each
        # target is an offset into the paragraph as discovered, and an edit
        # on the left would shift every offset to its right.
        edits = []
        for blank_id, value in values.items():
            target = walk.targets.get(blank_id)
            if target is None or value is None:
                continue
            if kinds.get(blank_id) is BlankKind.CHECKBOX:
                edits.append(((1, 0, 0), lambda t=target, v=value: writer.tick(t, v == "checked")))
            else:
                edits.append((_order(target), lambda t=target, v=value: writer.text(t, str(v))))
        for blank_id, png in (images or {}).items():
            target = walk.targets.get(blank_id)
            if target is not None:
                edits.append((_order(target), lambda t=target, p=png, b=blank_id:
                               writer.image(t, p, widths.get(b, 0.0))))
        for _key, edit in sorted(edits, key=lambda e: e[0]):
            edit()

        tmp = tempfile.TemporaryDirectory(prefix="omaform-docx-") if as_pdf else None
        docx_out = str(Path(tmp.name) / (Path(doc.path).stem + ".docx")) if tmp else out_path
        try:
            _package(doc.path, docx_out, dom, writer)
            if as_pdf:
                # Locking a Word document means it stops being one.
                to_pdf(docx_out, final)
        finally:
            if tmp is not None:
                tmp.cleanup()


def _order(target) -> tuple:
    """Sort key for an edit: span edits grouped by paragraph, right to left."""
    if target[0] == "span":
        return (0, id(target[1]), -target[2])
    return (1, 0, 0)


def _package(source: str, docx_out: str, dom, writer) -> None:
    """The source package with the edited parts swapped in, byte for byte otherwise."""
    with zipfile.ZipFile(source) as src, \
            zipfile.ZipFile(docx_out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename == DOC_XML:
                dst.writestr(item, dom.toxml(encoding="UTF-8"))
            elif item.filename == RELS_XML:
                dst.writestr(item, writer.rels.encode("utf-8"))
            elif item.filename == TYPES_XML:
                dst.writestr(item, writer.types.encode("utf-8"))
            else:
                dst.writestr(item, src.read(item.filename))
        if RELS_XML not in src.namelist():
            dst.writestr(RELS_XML, writer.rels.encode("utf-8"))
        for name, data in writer.media.items():
            dst.writestr(name, data)


def soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def to_pdf(docx_path: str, out_pdf: str) -> str:
    """Render through LibreOffice, headless. Returns the PDF path."""
    exe = soffice()
    if exe is None:
        raise RuntimeError("LibreOffice is needed to render a Word document")
    with tempfile.TemporaryDirectory(prefix="omaform-docx-") as tmp:
        subprocess.run([exe, "--headless", "--convert-to", "pdf", "--outdir", tmp,
                        docx_path], check=True, capture_output=True, timeout=120,
                       env={**os.environ, "HOME": os.environ.get("HOME", tmp)})
        made = Path(tmp) / (Path(docx_path).stem + ".pdf")
        if not made.exists():
            raise RuntimeError("LibreOffice produced no PDF")
        shutil.move(str(made), out_pdf)
    return out_pdf
