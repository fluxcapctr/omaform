"""The parts of the window that are logic rather than widgets.

Importing the UI modules pulls in GTK, which works headless; nothing here opens
a window. Anything needing a display belongs in a manual pass, not in here.
"""

import pytest

pytest.importorskip("gi")

from omaform.ui import theme  # noqa: E402
from omaform.ui.app import Window  # noqa: E402


class FakeEntry:
    """Just enough of a plan Entry for the label formatter."""

    def __init__(self, label: str) -> None:
        outer = self

        class blank:
            class label:
                @staticmethod
                def best() -> str:
                    return outer.text

        self.text = label
        self.blank = blank
        self.match = None  # the helper prefers the matched slot when there is one


@pytest.mark.parametrize("raw,expected", [
    # The form's line number leads, glued or spaced, with or without a sub-item.
    ("6City, state, and ZIP code", "City, state, and ZIP code"),
    ("1 Name of entity/individual.", "Name of entity/individual."),
    ("3a Check the appropriate box", "Check the appropriate box"),
    ("7 List account number(s)", "List account number(s)"),
    # "5Address": the A belongs to the label, not to the line number.
    ("See 5Address (number, street)", "Address (number, street)"),
    # Nothing to strip.
    ("Social security number", "Social security number"),
    ("Employer identification number", "Employer identification number"),
])
def test_label_tidying(raw, expected):
    assert Window._label_of(FakeEntry(raw)) == expected


def test_an_empty_label_says_so_rather_than_showing_nothing():
    assert Window._label_of(FakeEntry("   ")) == "unlabelled"


def test_a_long_label_is_cut_with_an_ellipsis():
    out = Window._label_of(FakeEntry("A " + "very " * 40 + "long label"))
    assert len(out) <= 52 and out.endswith("…")


# --- theme ------------------------------------------------------------------

SAMPLE = '''
mode = "dark"
accent = "#2ad4f0"
background = "#08131c"
foreground = "#c2d8e6"
selection = "#173247"
muted = "#5a7385"
red = "#e05a3c"
green = "#4fc99e"
'''


def test_palette_parses_omarchy_colors():
    palette = theme.parse(SAMPLE)
    assert palette is not None
    assert palette.dark is True
    assert palette.accent == "#2ad4f0"
    # Absent keys fall back rather than leaving a hole in the stylesheet.
    assert palette.dark_background == "#08131c"


def test_light_themes_are_recognised():
    palette = theme.parse('mode = "light"\nbackground = "#ffffff"\nforeground = "#111111"')
    assert palette is not None and palette.dark is False


def test_mode_is_inferred_from_brightness_when_absent():
    dark = theme.parse('background = "#08131c"\nforeground = "#c2d8e6"')
    light = theme.parse('background = "#fafafa"\nforeground = "#202020"')
    assert dark.dark is True and light.dark is False


def test_a_file_without_colours_yields_nothing():
    """Better the stock libadwaita look than a palette we invented."""
    assert theme.parse('mode = "dark"\nnot_a_colour = "hello"') is None
    assert theme.parse("") is None


def test_comments_and_bare_values_are_tolerated():
    palette = theme.parse('background = "#08131c" # the page\nforeground = #c2d8e6\n')
    assert palette is not None and palette.foreground == "#c2d8e6"


def test_css_mentions_every_colour_it_was_given():
    palette = theme.parse(SAMPLE)
    css = theme.css(palette)
    for colour in (palette.background, palette.foreground, palette.accent):
        assert colour in css


def test_css_does_not_paint_values_in_the_accent():
    """A loud accent must not make a filled value look like a confirmed one."""
    palette = theme.parse(SAMPLE)
    for line in theme.css(palette).splitlines():
        if ".omaform-filled" in line:
            assert palette.accent not in line


def test_every_corner_is_square():
    """omarchy.org's radius tokens are all 0px. The stylesheet must not quietly
    reintroduce a curve somewhere, because that is exactly how the first pass
    ended up looking like libadwaita with a different font."""
    import re
    for sheet in (theme.base_css(), theme.css(theme.parse(SAMPLE))):
        curved = re.findall(r"border-radius:\s*([1-9][0-9]*[a-z%]*)", sheet)
        assert not curved, f"rounded corners crept back in: {curved}"
    assert "border-radius: 0" in theme.base_css()


def test_headings_are_not_uppercase():
    """The site sets its headings in normal case; uppercase is for four tiny
    eyebrow labels, and the first pass shouted every section title."""
    assert "uppercase" not in theme.base_css()
