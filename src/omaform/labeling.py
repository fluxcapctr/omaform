"""Work out what a blank is asking for, from the text printed around it.

This is needed even for PDFs with proper form fields, because the field names on
real government forms carry no meaning at all: the W-9 calls its business-name
box `f1_02[0]` and supplies no tooltip. The printed label beside the box is the
only description of it that exists, so geometry is not a fallback here, it is the
primary source.

Everything works on text *lines* rather than individual words. Word-level
association pulls body prose out of neighbouring columns; line-level association
with contiguous-run extension does not.
"""

from __future__ import annotations

import re

from .model import LabelContext
from .textmap import Box, Line, PageText, Word

# A horizontal gap this wide is a column break, not a word space. Word spaces in
# these forms run 2 to 4 points at 10pt, so there is plenty of headroom below.
COLUMN_GAP = 12.0
# A left label further away than this belongs to something else.
MAX_LEFT_GAP = 72.0
# How far apart dot leaders may sit and still be one run.
LEADER_GAP = 24.0
# A label line sitting further above than this is not this blank's label.
MAX_ABOVE_GAP = 15.0
# How far a label line may *overlap* the blank it labels, as a fraction of the
# label's own height. Entry fields on IRS forms are drawn over the whole table
# cell, so the small label printed at the cell's top-left is inside the field
# rectangle by a couple of points. Requiring labels to sit wholly above the box
# loses them by a fraction of a point.
MAX_ABOVE_OVERLAP = 0.75
# Slack for float noise and for labels that start a hair left of their field.
PAD = 3.0

# "Part II", "Section 1", and USCIS's "Supplement A": a heading that scopes
# everything beneath it to one party. Supplement A of the I-9 is the preparer's
# and every date in it is the preparer's, however plainly the boxes are labelled.
_SECTION_RE = re.compile(r"^\s*((part|section)\s+([ivxlc]+|\d+)|supplement\s+[a-z])\b", re.I)
# "Employers Only", "For Office Use Only", "Preparer Use Only": a block that
# says on its face that it is somebody else's. Short, and ends in "only".
_ONLY_RE = re.compile(r"^\s*(for\s+)?[a-z'\u2019 ]{3,40}\s+only\b\.?\s*$", re.I)
_LEADER_RE = re.compile(r"^[.…·\s]+$")
_LINE_NO_RE = re.compile(r"^(\d{1,2})(?=[A-Z(])")
_STARTS_LINE_NO_RE = re.compile(r"^\d{1,2}[a-z]? ")
# "(a)", "(b)", "(1)": a sub-item marker, so the label starts here, not above.
_MARKER_RE = re.compile(r"^\(?[a-z0-9]{1,2}\)")
# A wrapped label longer than this is prose that happens to sit above a field.
MAX_LABEL_CHARS = 320


def _clean(words: list[Word]) -> str:
    """Join words into a label, dropping the dot leaders IRS forms are full of."""
    kept = [w.text for w in words if not _LEADER_RE.match(w.text)]
    text = " ".join(kept).strip()
    text = _LINE_NO_RE.sub(r"\1 ", text)          # "5Address" -> "5 Address"
    text = re.sub(r"[\s.:…]+$", "", text)     # trailing colon and leaders
    return re.sub(r"\s{2,}", " ", text).strip()


def _run_leftward(words: list[Word], anchor_x: float) -> list[Word]:
    """Words immediately left of anchor_x, stopping at the first column break."""
    left = [w for w in words if w.x1 <= anchor_x + PAD]
    if not left:
        return []
    left.sort(key=lambda w: w.x0)
    if anchor_x - left[-1].x1 > MAX_LEFT_GAP:
        return []
    run = [left[-1]]
    for w in reversed(left[:-1]):
        # Dot leaders are sparse by design: on the W-9 the jump from the last
        # word of a label onto its first leader is wider than a word space,
        # and treating that as a column break left only the dots, which clean
        # to nothing. A gap onto a leader is allowed to be a leader's width.
        limit = LEADER_GAP if _LEADER_RE.match(run[0].text) else COLUMN_GAP
        if run[0].x0 - w.x1 > limit:
            break
        run.insert(0, w)
    return run


# A blank this small is a checkbox, and a checkbox's label is to its right.
CHECKBOX_MAX_PT = 16.0
MAX_RIGHT_GAP = 24.0
MAX_RIGHT_LABEL_PT = 260.0


def _run_rightward(words: list[Word], anchor_x: float) -> list[Word]:
    """Words immediately right of anchor_x, stopping at a column break or the
    next checkbox's label, which shows up as a gap."""
    right = sorted((w for w in words if w.x0 >= anchor_x - PAD), key=lambda w: w.x0)
    if not right or right[0].x0 - anchor_x > MAX_RIGHT_GAP:
        return []
    run = [right[0]]
    for w in right[1:]:
        if w.x0 - run[-1].x1 > COLUMN_GAP or w.x1 - run[0].x0 > MAX_RIGHT_LABEL_PT:
            break
        run.append(w)
    return run


def _run_around(line: Line, x0: float, x1: float) -> list[Word]:
    """The contiguous run of words on `line` that spans the blank's own column.

    Empty when the line has nothing in the blank's horizontal band. That is the
    point: on a two-column form the nearest line above a right-column field is
    usually left-column prose, and it must be rejected rather than salvaged.
    """
    # A word must *start* within the blank's span to seed the label. Overlap
    # alone let in the first word of the next column, which on the W-4 begins
    # four points past the address field's right edge, so the address line
    # was labelled "name" from "Does your name match...".
    seed = [w for w in line.words_between(x0 - PAD * 2, x1 + PAD * 2) if w.x0 < x1 - 1]
    if not seed:
        return []
    first, last = line.words.index(seed[0]), line.words.index(seed[-1])
    while first > 0 and seed[0].x0 - line.words[first - 1].x1 <= COLUMN_GAP:
        first -= 1
        seed.insert(0, line.words[first])
    # Rightward extension stops at the blank's own right edge. A label above a
    # field may start slightly left of it and may run shorter, but text sitting
    # well to the right belongs to the next column: on the W-9 that is how
    # "Requester's name and address" ends up glued to the address label.
    # Narrow boxes get more room: a 29pt TIN box is labelled "Employer
    # identification number", which is far wider than the box itself.
    right_limit = max(x1, x0 + 140.0)
    while (last < len(line.words) - 1
           and line.words[last + 1].x0 - seed[-1].x1 <= COLUMN_GAP
           and line.words[last + 1].x0 <= right_limit):
        last += 1
        seed.append(line.words[last])
    return seed


def _same_line(page: PageText, box: Box) -> Line | None:
    """The text line the blank itself sits on, if any."""
    cy = (box[1] + box[3]) / 2
    best, best_dist = None, None
    for line in page.lines:
        lb = line.box
        if lb[1] - PAD <= cy <= lb[3] + PAD:
            dist = abs((lb[1] + lb[3]) / 2 - cy)
            if best_dist is None or dist < best_dist:
                best, best_dist = line, dist
    return best


def _is_continuation(text: str) -> bool:
    """Does this line read as the middle of a wrapped label rather than its start?

    A label's own first line opens with a capital or with the form's line number.
    A continuation opens mid-sentence, in lower case. That one test is what keeps
    the upward walk from marching off a short label into the body prose above it.
    """
    if not text or _STARTS_LINE_NO_RE.match(text) or _MARKER_RE.match(text):
        return False
    first = text.split(" ", 1)[0].lstrip("(“\"'")
    return bool(first) and first[0].islower()


def _lines_above(page: PageText, box: Box, limit: int = 4) -> list[Line]:
    """The blank's label lines, nearest first, following a label that wrapped.

    Government forms wrap long labels over several printed lines, so taking only
    the nearest line yields its tail: the W-9's line 1 label ends "...enter the
    business/disregarded entity's name on line 2.)", which is useless on its own.

    Walk upward while lines stay tightly spaced, and stop at the line that opens
    with a line number, since that is where the label itself begins.
    """
    def gap_ok(ln: Line) -> bool:
        height = ln.box[3] - ln.box[1]
        gap = box[1] - ln.box[3]
        return -MAX_ABOVE_OVERLAP * height <= gap <= MAX_ABOVE_GAP

    above = sorted(
        (ln for ln in page.lines if gap_ok(ln) and _run_around(ln, box[0], box[2])),
        key=lambda ln: box[1] - ln.box[3],
    )
    if not above:
        return []

    picked = [above[0]]
    if not _is_continuation(_clean(_run_around(above[0], box[0], box[2]))):
        return picked

    for _ in range(limit - 1):
        top = picked[-1].box[1]
        prev = [ln for ln in page.lines if -PAD <= top - ln.box[3] <= MAX_ABOVE_GAP]
        prev = [ln for ln in prev if _run_around(ln, box[0], box[2])]
        if not prev:
            break
        nxt = min(prev, key=lambda ln: top - ln.box[3])
        run = _clean(_run_around(nxt, box[0], box[2]))
        if not run:
            break
        picked.append(nxt)
        if not _is_continuation(run):
            break
    return picked


def _section(page: PageText, box: Box) -> str:
    """Nearest 'Part II' style heading above the blank, with its continuation.

    A heading is often split over two lines, and the party is on the second:
    the I-9's page 3 reads "Supplement A, USCIS" and then "Preparer and/or
    Translator Certification". Taking only the first line hands the whole
    preparer's page to the employee. The next line is appended when it sits
    directly beneath and starts at the same margin.
    """
    best, best_raw = "", ""
    lines = page.lines
    for i, line in enumerate(lines):
        if line.box[3] > box[1] + PAD:
            continue
        text = raw = line.text.strip()
        if _SECTION_RE.match(text) or _ONLY_RE.match(text):
            # A page header repeats the heading in short: the I-9's page 3 has
            # "Supplement A" alone in a corner box beneath the real heading
            # "Supplement A, USCIS / Preparer and/or Translator Certification".
            # A heading line that is merely a prefix of one already found is
            # that repeat, and must not replace the heading naming the party.
            # Judged on the line itself, before any continuation is joined.
            if best_raw and best_raw.lower().startswith(raw.lower()) and len(raw) < len(best_raw):
                continue
            if i + 1 < len(lines):
                nxt = lines[i + 1]
                # Adjacent below and overlapping across: headings are often
                # centred, so a shared left margin is the wrong test, and a
                # logo merged into the heading line can make the gap slightly
                # negative.
                if (-4 <= nxt.box[1] - line.box[3] <= 6
                        and nxt.box[2] > line.box[0] and nxt.box[0] < line.box[2]
                        and nxt.box[3] <= box[1] + PAD):
                    text = f"{text} {nxt.text.strip()}"
            best, best_raw = text, raw
    return best


def _inside(page: PageText, box: Box) -> str:
    """A caption printed within the blank's own rectangle."""
    x0, y0, x1, y1 = box
    words = [w for w in page.words
             if w.x0 >= x0 - 1 and w.x1 <= x1 + 1 and w.y0 >= y0 - 1 and w.y1 <= y1 + 1]
    if not words:
        return ""
    words.sort(key=lambda w: (round(w.y0), w.x0))
    return _clean(words)


def describe(page: PageText, box: Box, native_name: str = "") -> LabelContext:
    """Everything printed around `box` that might describe it."""
    ctx = LabelContext(native_name=native_name, section=_section(page, box))
    ctx.inside = _inside(page, box)

    line = _same_line(page, box)
    if line is not None:
        ctx.left = _clean(_run_leftward(line.words, box[0]))
        if (box[2] - box[0] <= CHECKBOX_MAX_PT and box[3] - box[1] <= CHECKBOX_MAX_PT):
            ctx.right = _clean(_run_rightward(line.words, box[2]))

    picked = [ln for ln in _lines_above(page, box) if ln is not line]
    if picked:
        parts = [_clean(_run_around(ln, box[0], box[2])) for ln in reversed(picked)]
        text = " ".join(p for p in parts if p)
        ctx.above = text if len(text) <= MAX_LABEL_CHARS else parts[0][:MAX_LABEL_CHARS]
        ctx.row = " ".join(w.text for w in picked[0].words[:5])
    elif line is not None and ctx.left:
        ctx.row = " ".join(w.text for w in line.words[:5])

    # A label found above is usually the real one on government forms, where
    # labels head their entry line. A short left label wins only when there is
    # nothing above, or when the above text is clearly prose.
    return ctx
