"""Blanks on pages that have no form fields at all.

Most PDFs people are sent are like this: a label, then a rule or a run of
underscores, and nothing behind it a viewer could fill. The page's own drawing
still says where the blanks are. Two sources, both geometric and exact:

* runs of underscore characters in the text, which Word-made forms use;
* thin horizontal rules in the content stream, drawn as stroked lines or as
  hairline-thin filled rectangles.

Each becomes a text blank with a rectangle sitting just above the rule, and is
labelled the same way a real field is, from the words to its left and above.
Only a rule that has a label is kept: a bare line under nothing is a separator.
"""

from __future__ import annotations

from dataclasses import dataclass

import pikepdf

from . import labeling, textmap
from .model import Blank, BlankKind, LabelContext

MIN_RULE_PT = 36.0          # shorter than this is a tick or a dash
MAX_RULE_FRACTION = 0.85    # wider than this share of the page is a separator
MAX_RULE_THICKNESS = 2.5
BLANK_HEIGHT_PT = 12.0
MIN_UNDERSCORES = 4


@dataclass(frozen=True)
class Rule:
    x0: float
    x1: float
    y: float      # PDF user space, the rule's own height


def _apply(m, x, y):
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def _mul(m, n):
    """m then n, as PDF's `cm` composes."""
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + b * c2, a * b2 + b * d2,
            c * a2 + d * c2, c * b2 + d * d2,
            e * a2 + f * c2 + e2, e * b2 + f * d2 + f2)


def rules_in(page: pikepdf.Page) -> list[Rule]:
    """Horizontal rules drawn in the page's content stream."""
    out: list[Rule] = []
    ctm = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    stack: list[tuple] = []
    current: list[tuple[float, float]] = []   # a path being built
    segments: list[tuple[float, float, float, float]] = []
    rects: list[tuple[float, float, float, float]] = []

    def flush(painted: bool) -> None:
        if painted:
            for x0, y0, x1, y1 in segments:
                if abs(y1 - y0) <= 1.0 and abs(x1 - x0) >= MIN_RULE_PT:
                    out.append(Rule(min(x0, x1), max(x0, x1), (y0 + y1) / 2))
            for x, y, w, h in rects:
                if abs(h) <= MAX_RULE_THICKNESS and abs(w) >= MIN_RULE_PT:
                    out.append(Rule(min(x, x + w), max(x, x + w), y + h / 2))
        segments.clear()
        rects.clear()
        current.clear()

    try:
        ops = pikepdf.parse_content_stream(page)
    except Exception:  # noqa: BLE001, a broken stream has no rules to give
        return out
    for operands, operator in ops:
        op = str(operator)
        try:
            if op == "q":
                stack.append(ctm)
            elif op == "Q":
                ctm = stack.pop() if stack else ctm
            elif op == "cm":
                ctm = _mul(tuple(float(v) for v in operands), ctm)
            elif op == "m":
                current.append(_apply(ctm, float(operands[0]), float(operands[1])))
            elif op == "l":
                point = _apply(ctm, float(operands[0]), float(operands[1]))
                if current:
                    segments.append((*current[-1], *point))
                current.append(point)
            elif op == "re":
                x, y, w, h = (float(v) for v in operands)
                p0 = _apply(ctm, x, y)
                p1 = _apply(ctm, x + w, y + h)
                rects.append((p0[0], p0[1], p1[0] - p0[0], p1[1] - p0[1]))
            elif op in ("S", "s", "f", "F", "f*", "B", "B*", "b", "b*"):
                flush(painted=True)
            elif op == "n":
                flush(painted=False)
        except (IndexError, ValueError, TypeError):
            continue
    return out


def underscore_runs(page_text: textmap.PageText) -> list[Rule]:
    """Runs of underscores in the text, as rules in PDF space."""
    out: list[Rule] = []
    height = page_text.height
    for line in page_text.lines:
        run: list[textmap.Word] = []

        def close() -> None:
            if run:
                x0, x1 = run[0].x0, run[-1].x1
                if x1 - x0 >= MIN_RULE_PT:
                    baseline = max(w.y1 for w in run)
                    out.append(Rule(x0, x1, height - baseline + 1.5))
            run.clear()

        for word in line.words:
            if set(word.text) <= {"_"} and len(word.text) >= MIN_UNDERSCORES:
                if run and word.x0 - run[-1].x1 > labeling.COLUMN_GAP:
                    close()
                run.append(word)
            elif "_" in word.text and len(word.text.strip("_")) < len(word.text) // 2:
                # "Name:_______" glued to its label
                run.append(word)
            else:
                close()
        close()
    return out


def blanks_for(page_index: int, page: pikepdf.Page, page_text: textmap.PageText,
               taken: list[tuple[float, float, float, float]],
               extra_rules: list[Rule] = (), scanned: bool = False) -> list[Blank]:
    """Text blanks for every labelled rule on a page with no fields of its own.

    `extra_rules` come from a scan's pixels; `scanned` marks the blanks so the
    planner offers rather than writes, since OCR labels are read, not given.
    """
    width = page_text.width
    rules = rules_in(page) + underscore_runs(page_text) + list(extra_rules)
    seen: set[tuple[int, int, int]] = set()
    out: list[Blank] = []
    for rule in sorted(rules, key=lambda r: (-r.y, r.x0)):
        if rule.x1 - rule.x0 > width * MAX_RULE_FRACTION:
            continue
        key = (round(rule.x0), round(rule.x1), round(rule.y))
        if key in seen:
            continue
        seen.add(key)
        rect = (rule.x0, rule.y, rule.x1, rule.y + BLANK_HEIGHT_PT)
        if any(rect[0] < t[2] and rect[2] > t[0] and rect[1] < t[3] and rect[3] > t[1]
               for t in taken):
            continue  # a field or a signature line already owns this spot
        box = textmap.from_pdf_rect(rect, page_text.height)
        label = labeling.describe(page_text, box)
        if not (label.left or label.above):
            continue
        blank_id = f"p{page_index + 1}:~line-{round(rule.x0)}-{round(rule.y)}"
        out.append(Blank(id=blank_id, kind=BlankKind.TEXT, label=label,
                         page=page_index + 1, width_pt=rule.x1 - rule.x0,
                         height_pt=BLANK_HEIGHT_PT, rect=rect,
                         native={"synthetic": True, "rect": rect, "scanned": scanned}))
    return out
