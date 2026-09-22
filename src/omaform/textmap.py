"""Words and lines with boxes, in one coordinate space.

Everything in Omaform works in **top-left points**: x grows right, y grows down,
origin at the top-left of the page. Poppler already reports text that way. PDF
widget rectangles do not, so the adapter converts them on the way in and this
module is the only place that convention is written down.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import gi

gi.require_version("Poppler", "0.18")
from gi.repository import Poppler  # noqa: E402

Box = tuple[float, float, float, float]  # x0, y0, x1, y1, top-left origin


def union(a: Box, b: Box) -> Box:
    return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))


def from_pdf_rect(rect: Box, page_height: float) -> Box:
    """Convert a PDF bottom-left rectangle to our top-left space."""
    x0, y0, x1, y1 = rect
    return (min(x0, x1), page_height - max(y0, y1), max(x0, x1), page_height - min(y0, y1))


@dataclass
class Word:
    text: str
    box: Box

    @property
    def x0(self) -> float: return self.box[0]
    @property
    def y0(self) -> float: return self.box[1]
    @property
    def x1(self) -> float: return self.box[2]
    @property
    def y1(self) -> float: return self.box[3]
    @property
    def height(self) -> float: return self.box[3] - self.box[1]


@dataclass
class Line:
    """Words sharing a baseline, left to right."""

    words: list[Word] = field(default_factory=list)

    @property
    def box(self) -> Box:
        b = self.words[0].box
        for w in self.words[1:]:
            b = union(b, w.box)
        return b

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def words_between(self, x0: float, x1: float) -> list[Word]:
        return [w for w in self.words if w.x1 > x0 and w.x0 < x1]


@dataclass
class PageText:
    number: int
    width: float
    height: float
    words: list[Word] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)


# How far a word's vertical centre may sit from its line's centre, as a fraction
# of glyph height. Compared against the line's anchor word rather than against a
# growing union box: a union drifts, and drift chains two columns of a two-column
# form into one bogus line.
_LINE_TOLERANCE = 0.45


def _words_from_page(page: Poppler.Page) -> list[Word]:
    """Poppler gives us a character string and a matching rectangle per character."""
    text = page.get_text()
    ok, rects = page.get_text_layout()
    if not ok or not text:
        return []

    words: list[Word] = []
    buf = ""
    box: Box | None = None
    for ch, r in zip(text, rects):
        if ch.isspace() or ch == " ":
            if buf and box:
                words.append(Word(buf, box))
            buf, box = "", None
            continue
        buf += ch
        cb: Box = (r.x1, r.y1, r.x2, r.y2)
        box = cb if box is None else union(box, cb)
    if buf and box:
        words.append(Word(buf, box))
    return words


def _centre(w: Word) -> float:
    return (w.y0 + w.y1) / 2


def _lines_from_words(words: list[Word]) -> list[Line]:
    """Cluster words into lines by vertical centre, then sort each left to right.

    Centre distance against a fixed anchor word, not overlap against the line's
    bounding box. Overlap-against-the-union is the obvious implementation and it
    is wrong: on a two-column form the box grows a little with each word added,
    and after enough words it overlaps the next column's lines too, merging
    unrelated text into one line. Anchoring kills the drift.

    Words on the same visual line in different columns *do* still land in one
    Line, which is correct. Separating the columns is the labeler's job, and it
    does that by refusing to cross a COLUMN_GAP.
    """
    lines: list[Line] = []
    anchors: list[Word] = []
    for w in sorted(words, key=lambda w: (_centre(w), w.x0)):
        placed = False
        for line, anchor in zip(reversed(lines), reversed(anchors)):
            tol = _LINE_TOLERANCE * min(anchor.height, w.height)
            if abs(_centre(w) - _centre(anchor)) <= tol:
                line.words.append(w)
                placed = True
                break
        if not placed:
            lines.append(Line([w]))
            anchors.append(w)
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    lines.sort(key=lambda ln: ln.box[1])
    return lines


def read_pdf(path: str) -> list[PageText]:
    uri = "file://" + os.path.abspath(path)
    doc = Poppler.Document.new_from_file(uri)
    pages = []
    for i in range(doc.get_n_pages()):
        page = doc.get_page(i)
        w, h = page.get_size()
        words = _words_from_page(page)
        pages.append(PageText(i + 1, w, h, words, _lines_from_words(words)))
    return pages
