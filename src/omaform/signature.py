"""A drawn signature: strokes in, a trimmed transparent PNG out.

The same stroke renderer draws the live canvas and the exported image, so what
you see while signing is exactly what gets stored. Rendered at 3x so it stays
crisp when it is eventually placed on a page, and trimmed to the ink with a
small margin so placement can size it by its content rather than its canvas.

Ink is black on transparent, because forms are paper-white and a signature
that carried its own background would sit on a page as a grey box.
"""

from __future__ import annotations

import base64
import io

import cairo

Point = tuple[float, float]
Stroke = list[Point]

INK_WIDTH = 2.2       # canvas pixels; scaled with everything else on export
EXPORT_SCALE = 3      # export resolution multiplier
MARGIN = 10           # canvas pixels of clear space around the ink
INK = (0.0, 0.0, 0.0)


def draw_strokes(ctx: "cairo.Context", strokes: list[Stroke],
                 width: float = INK_WIDTH) -> None:
    """Draw strokes as smooth curves with round ends.

    Straight segments between sampled points look jagged at the speed a hand
    moves; a quadratic through each midpoint reads as a pen line instead.
    """
    ctx.set_source_rgb(*INK)
    ctx.set_line_width(width)
    ctx.set_line_cap(cairo.LINE_CAP_ROUND)
    ctx.set_line_join(cairo.LINE_JOIN_ROUND)
    for stroke in strokes:
        if not stroke:
            continue
        if len(stroke) == 1:
            x, y = stroke[0]
            ctx.arc(x, y, width / 2, 0, 6.2832)
            ctx.fill()
            continue
        ctx.move_to(*stroke[0])
        if len(stroke) == 2:
            ctx.line_to(*stroke[1])
        else:
            for i in range(1, len(stroke) - 1):
                cx, cy = stroke[i]
                nx, ny = stroke[i + 1]
                mx, my = (cx + nx) / 2, (cy + ny) / 2
                # cairo has only cubics; this is the quadratic through (cx, cy).
                px, py = ctx.get_current_point()
                ctx.curve_to(px + 2 / 3 * (cx - px), py + 2 / 3 * (cy - py),
                             mx + 2 / 3 * (cx - mx), my + 2 / 3 * (cy - my),
                             mx, my)
            ctx.line_to(*stroke[-1])
        ctx.stroke()


def render_png(strokes: list[Stroke], *, scale: int = EXPORT_SCALE,
               margin: float = MARGIN) -> bytes | None:
    """The signature as PNG bytes, trimmed to its ink. None if nothing was drawn."""
    points = [p for s in strokes for p in s]
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, y0 = min(xs) - margin, min(ys) - margin
    w = max(1.0, max(xs) - min(xs) + 2 * margin)
    h = max(1.0, max(ys) - min(ys) + 2 * margin)

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                 max(1, int(w * scale)), max(1, int(h * scale)))
    ctx = cairo.Context(surface)
    ctx.scale(scale, scale)
    ctx.translate(-x0, -y0)
    draw_strokes(ctx, strokes)
    surface.flush()
    out = io.BytesIO()
    surface.write_to_png(out)
    return out.getvalue()


def encode(png: bytes) -> str:
    """How a signature is kept in the vault: base64 text under the key."""
    return base64.b64encode(png).decode("ascii")


def decode(text: str) -> bytes | None:
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        return None
    return raw if raw.startswith(b"\x89PNG") else None
