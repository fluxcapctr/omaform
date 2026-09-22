"""Scanned pages: words from OCR, rules from the pixels.

A scan is one picture per page with no text layer and no drawing
instructions, so neither of the other readers has anything to work with.
tesseract, already installed, gives words with boxes; the rules come from the
image itself, as long thin runs of dark pixels. Both are turned into the same
page text and rule objects the flat-PDF reader uses, so from there on a scan
is just a flat form. Confidence is lower, and the plan says so.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import textmap
from .flat import MIN_RULE_PT, Rule

DPI = 150
SCALE = DPI / 72.0
MIN_WORD_CONFIDENCE = 40


def available() -> bool:
    return shutil.which("tesseract") is not None and shutil.which("pdftoppm") is not None


def looks_scanned(page_text: textmap.PageText) -> bool:
    """A page with almost no words in its text layer is a picture of a page."""
    return len(page_text.words) < 5


def render(path: str, page_number: int, out_dir: str) -> Path:
    prefix = Path(out_dir) / f"scan-{page_number}"
    subprocess.run(["pdftoppm", "-f", str(page_number), "-l", str(page_number), "-r", str(DPI),
                    "-cropbox", "-png", "--", path, str(prefix)], check=True, capture_output=True,
                   timeout=120)
    matches = sorted(Path(out_dir).glob(f"scan-{page_number}*.png"))
    if not matches:
        raise RuntimeError(f"pdftoppm produced nothing for page {page_number}")
    return matches[0]


def ocr(image: Path, page_number: int, width_pt: float, height_pt: float) -> textmap.PageText:
    """tesseract's TSV, as words and lines in top-left points."""
    done = subprocess.run(["tesseract", str(image), "-", "--psm", "6", "tsv"],
                          capture_output=True, text=True, check=True, timeout=180)
    words: list[textmap.Word] = []
    for row in done.stdout.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12 or not parts[11].strip():
            continue
        try:
            conf = float(parts[10])
            left, top, w, h = (int(parts[i]) for i in (6, 7, 8, 9))
        except ValueError:
            continue
        if conf < MIN_WORD_CONFIDENCE:
            continue
        words.append(textmap.Word(parts[11].strip(),
                                  (left / SCALE, top / SCALE, (left + w) / SCALE,
                                   (top + h) / SCALE)))
    return textmap.PageText(page_number, width_pt, height_pt, words,
                            textmap._lines_from_words(words))


def raster_rules(image: Path, height_pt: float) -> list[Rule]:
    """Long thin runs of dark pixels, as rules in PDF space."""
    from PIL import Image

    im = Image.open(image).convert("L")
    w, h = im.size
    px = im.load()
    min_run = int(MIN_RULE_PT * SCALE)
    rows: list[tuple[int, int, int]] = []   # (y, x0, x1)
    for y in range(h):
        x = 0
        while x < w:
            if px[x, y] < 128:
                start = x
                while x < w and px[x, y] < 128:
                    x += 1
                if x - start >= min_run:
                    rows.append((y, start, x))
            else:
                x += 1
    # Merge adjacent rows into one rule; a rule thicker than 4px is a bar.
    rules: list[Rule] = []
    rows.sort()
    i = 0
    while i < len(rows):
        y, x0, x1 = rows[i]
        j = i + 1
        while j < len(rows) and rows[j][0] == rows[j - 1][0] + 1 \
                and abs(rows[j][1] - x0) < 8 and abs(rows[j][2] - x1) < 8:
            j += 1
        thickness = j - i
        if thickness <= 4:
            mid = (rows[i][0] + rows[j - 1][0]) / 2
            rules.append(Rule(x0 / SCALE, x1 / SCALE, height_pt - mid / SCALE))
        i = j
    return rules


def read_page(path: str, page_number: int, width_pt: float,
              height_pt: float) -> tuple[textmap.PageText, list[Rule]]:
    """Everything a scanned page can tell us."""
    with tempfile.TemporaryDirectory(prefix="omaform-scan-") as tmp:
        image = render(path, page_number, tmp)
        return ocr(image, page_number, width_pt, height_pt), raster_rules(image, height_pt)
