"""Locking: the fill burned into the page, comb boxes one digit per cell."""

import subprocess

import pikepdf
from PIL import Image

from omaform import plan as planning, signature
from omaform.adapters import for_path
from omaform.profile import Profile


def signed_w9(w9, tmp_path, lock):
    doc = for_path(w9).discover(w9)
    png = signature.render_png([[(0, 0), (60, 25), (120, 5)]])
    result = planning.build(doc, Profile(
        {"full_name": "Alex Rivera", "business_name": "Rivera Design Co",
         "address1": "742 Evergreen Terrace", "city": "Springfield", "state": "OR",
         "zip": "97403", "tax_classification": "s_corp"},
        {"ein": "12-3456789", "signature": signature.encode(png)}), prefer={"ein"})
    out = tmp_path / ("locked.pdf" if lock else "open.pdf")
    for_path(w9).write(doc, result.values(), str(out), images=result.images(), lock_form=lock)
    return doc, out


def test_a_locked_form_has_no_fields_left(w9, tmp_path):
    _doc, out = signed_w9(w9, tmp_path, lock=True)
    pdf = pikepdf.open(str(out))
    assert "/AcroForm" not in pdf.Root
    assert all(not (p.get("/Annots") or []) for p in pdf.pages)


def test_a_locked_form_still_shows_everything(w9, tmp_path):
    _doc, out = signed_w9(w9, tmp_path, lock=True)
    text = subprocess.run(["pdftotext", "-layout", "-f", "1", "-l", "1", str(out), "-"],
                          capture_output=True, text=True, check=True).stdout
    for value in ("Alex Rivera", "Rivera Design Co", "742 Evergreen Terrace",
                  "Springfield, OR 97403"):
        assert value in text


def test_comb_digits_land_one_per_cell(w9, tmp_path):
    """qpdf writes a comb value as one run in the first cell. Each cell must
    carry its own digit, or the EIN row reads as nonsense."""
    doc, out = signed_w9(w9, tmp_path, lock=True)
    cells = [b for b in doc.blanks if b.id.startswith("p1:f1_15")]
    [ein_tail] = cells  # 7 digits of the EIN
    x0, y0, x1, y1 = ein_tail.rect
    page_h = float(doc.native.pages[0].MediaBox[3])
    subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-r", "144", "-png", str(out),
                    str(tmp_path / "pg")], check=True)
    [png] = list(tmp_path.glob("pg*.png"))
    image = Image.open(png).convert("L")
    s = 2.0
    inked = []
    for i in range(7):
        cx0 = x0 + (x1 - x0) * i / 7
        cx1 = x0 + (x1 - x0) * (i + 1) / 7
        region = image.crop((int(cx0 * s) + 3, int((page_h - y1) * s) + 3,
                             int(cx1 * s) - 3, int((page_h - y0) * s) - 3))
        inked.append(sum(1 for v in region.get_flattened_data() if v < 100) > 5)
    assert all(inked), f"cells with ink: {inked}"


def test_an_unlocked_form_keeps_its_fields(w9, tmp_path):
    _doc, out = signed_w9(w9, tmp_path, lock=False)
    pdf = pikepdf.open(str(out))
    assert "/AcroForm" in pdf.Root
