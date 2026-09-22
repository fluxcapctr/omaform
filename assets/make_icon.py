"""Draw the Omaform icon on a pixel grid, the way Omarchy draws its own mark.

Square tile, square corners, flat colour, every edge on a 16 by 16 grid, the
page's folded corner and the tick drawn as steps rather than curves. One
drawing makes the SVG and every PNG size, so they cannot drift apart.

    ./.venv/bin/python assets/make_icon.py
"""

from pathlib import Path

import cairo

HERE = Path(__file__).resolve().parent
UNIT = 32          # one grid cell at 512 px
TILE = "#0b0d10"
PAGE = "#e8e4dc"
BLANK = "#5d6570"
ACCENT = "#faa968"

# Cells as (x, y, w, h) in grid units.
PAGE_CELLS = [
    (2, 1, 5, 1),                                  # top edge, up to the fold
    (2, 1, 1, 14),                                 # left edge
    (2, 14, 9, 1),                                 # bottom edge
    (10, 4, 1, 11),                                # right edge, below the fold
    (7, 1, 1, 1), (8, 2, 1, 1), (9, 3, 1, 1),      # the fold's diagonal, stepped
    (7, 1, 1, 4), (7, 4, 4, 1),                    # and its inner corner
]
BLANKS = [(4, 7, 4, 1), (4, 11, 3, 1)]            # left empty
FILLED = [(4, 9, 5, 1)]                            # filled in
CURSOR: list = []
# A heavy stepped tick, cut out of whatever it overlaps.
TICK_ROWS = {9: [14], 10: [13, 14], 11: [12, 13], 12: [9, 11, 12], 13: [9, 10, 11], 14: [10]}
TICK = [(x, y, 1, 1) for y, xs in TICK_ROWS.items() for x in xs]
KNOCKOUT = sorted({(x + dx, y + dy, 1, 1) for x, y, _w, _h in TICK
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                   if 0 <= x + dx < 16 and 0 <= y + dy < 16})


def hex_rgb(value: str):
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))


def draw(ctx: cairo.Context, size: int) -> None:
    s = size / 16
    ctx.set_antialias(cairo.ANTIALIAS_NONE if size >= 64 else cairo.ANTIALIAS_DEFAULT)
    ctx.set_source_rgb(*hex_rgb(TILE))
    ctx.rectangle(0, 0, size, size)
    ctx.fill()
    for cells, colour in ((PAGE_CELLS, PAGE), (BLANKS, BLANK), (FILLED, ACCENT),
                          (KNOCKOUT, TILE), (TICK, ACCENT)):
        ctx.set_source_rgb(*hex_rgb(colour))
        for x, y, w, h in cells:
            ctx.rectangle(round(x * s), round(y * s), round((x + w) * s) - round(x * s),
                          round((y + h) * s) - round(y * s))
        ctx.fill()


def main() -> None:
    svg = cairo.SVGSurface(str(HERE / "omaform.svg"), 512, 512)
    draw(cairo.Context(svg), 512)
    svg.finish()
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        img = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        draw(cairo.Context(img), size)
        img.write_to_png(str(HERE / f"omaform-{size}.png"))
    # The video assembles the icon from these same cells.
    import json
    video = HERE.parent / "video" / "public"
    if video.is_dir():
        (video / "icon-cells.json").write_text(json.dumps({
            "tile": TILE, "groups": [
                {"colour": PAGE, "cells": PAGE_CELLS}, {"colour": BLANK, "cells": BLANKS},
                {"colour": ACCENT, "cells": FILLED}, {"colour": ACCENT, "cells": TICK}]}))
        (video / "omaform.svg").write_bytes((HERE / "omaform.svg").read_bytes())
    print("wrote omaform.svg and PNGs")


if __name__ == "__main__":
    main()
