"""The format-agnostic document model.

Adapters produce these objects; nothing above this layer knows or cares whether a
blank came from a PDF widget, a Word content control or a spreadsheet cell. Keeping
that boundary honest is what lets the profile, the matcher, the template store and
the review UI be written exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BlankKind(str, Enum):
    TEXT = "text"
    MULTILINE = "multiline"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    SIGNATURE = "signature"
    DATE = "date"


@dataclass
class LabelContext:
    """Everything we know about what a blank is asking for, in text form.

    Deliberately not collapsed to a single string: the matcher weighs these
    differently, and the LLM layer in Phase 8 is handed all of them separately.
    """

    left: str = ""
    above: str = ""
    # Only set for checkbox-sized blanks, whose label sits to their right.
    right: str = ""
    # A caption printed inside the box itself, as the W-4 does ("Address" in
    # the top-left corner of the address field). The surest label there is.
    inside: str = ""
    section: str = ""
    native_name: str = ""
    # How the printed row the label came from opens. A caption row that begins
    # "Employers name and address | First date of employment | ..." belongs to
    # the employer all the way along, even where a clipped label does not say so.
    row: str = ""
    # True when the form supplied the label itself (a tooltip), rather than it
    # being inferred from the page. USCIS does; the IRS does not.
    authoritative: bool = False

    def best(self) -> str:
        """The single most label-like string available, for display and grouping.

        Shorter wins. Labels are terse and the prose that bleeds in from a
        neighbouring column is not, so word count is a decent proxy for which
        slot actually holds the label. Ties go to the text on the same line.
        """
        if self.inside:
            return self.inside
        if self.right:
            return self.right
        options = [t for t in (self.left, self.above) if t]
        if options:
            return min(options, key=lambda t: (len(t.split()), options.index(t)))
        return self.section or self.native_name

    def all_text(self) -> str:
        parts = [p for p in (self.left, self.above, self.section) if p]
        return " | ".join(parts)


# A text character is about 0.5 em wide on average across the fonts these forms
# use, and form fields are typically set at 10pt or smaller.
_AVG_CHAR_PT = 5.0


@dataclass
class Blank:
    """One thing a human would have to type, click or sign."""

    id: str
    kind: BlankKind
    label: LabelContext = field(default_factory=LabelContext)
    page: int = 1
    width_pt: float = 0.0
    height_pt: float = 0.0
    # Where it is on the page, in PDF user space (origin bottom-left), so a
    # preview can draw the value where the writer will put it.
    rect: tuple[float, float, float, float] | None = None
    max_chars: int | None = None
    options: list[str] = field(default_factory=list)
    readonly: bool = False

    # Split fields: the run of single-character boxes that IRS forms use for a
    # Social Security or Employer Identification number. Members of a group share
    # a group id and one label, and a single profile value is distributed across
    # them in group_index order.
    group: str | None = None
    group_index: int = 0

    native: Any = None
    value: str | None = None
    # Where the person dragged what we drew for this blank, in points from
    # where it would otherwise go. Only for things drawn onto the page (a
    # signature, or a blank on a printed line); a form's own text field stays
    # where the form put it.
    offset: tuple[float, float] = (0.0, 0.0)

    def shifted(self, rect=None):
        """A rectangle (the blank's own by default) moved by the offset."""
        rect = self.rect if rect is None else rect
        if rect is None:
            return None
        dx, dy = self.offset
        x0, y0, x1, y1 = (float(v) for v in rect)
        return (x0 + dx, y0 + dy, x1 + dx, y1 + dy)

    @property
    def movable(self) -> bool:
        """Drawn by us, so it can be dragged: synthetic blanks and signatures."""
        return (isinstance(self.native, dict) and bool(self.native.get("synthetic"))) \
            or self.kind is BlankKind.SIGNATURE

    @property
    def capacity(self) -> int:
        """Best guess at how many characters fit, for picking between candidates.

        A two-character-wide box is asking for a state abbreviation, not a street
        address, and that is often the only signal available.
        """
        if self.max_chars:
            return self.max_chars
        if self.width_pt <= 0:
            return 0
        rows = max(1, int(self.height_pt // 12)) if self.kind is BlankKind.MULTILINE else 1
        return max(1, int(self.width_pt / _AVG_CHAR_PT) * rows)

    def __repr__(self) -> str:  # keeps CLI output and test failures readable
        return f"<Blank {self.id} {self.kind.value} {self.width_pt:.0f}pt {self.label.best()[:32]!r}>"


@dataclass
class Document:
    """A document plus every blank an adapter could find in it."""

    path: str
    fmt: str
    page_count: int = 1
    blanks: list[Blank] = field(default_factory=list)
    fingerprint: str = ""
    native: Any = None

    def by_id(self, blank_id: str) -> Blank | None:
        return next((b for b in self.blanks if b.id == blank_id), None)

    def groups(self) -> dict[str, list[Blank]]:
        """Split-field groups, each ordered by group_index."""
        out: dict[str, list[Blank]] = {}
        for b in self.blanks:
            if b.group:
                out.setdefault(b.group, []).append(b)
        for members in out.values():
            members.sort(key=lambda b: b.group_index)
        return out

    def fillable(self) -> list[Blank]:
        return [b for b in self.blanks if not b.readonly]
