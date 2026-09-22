"""Merge, split, reorder, rotate and photograph, all as one sequence."""

import pikepdf
import pytest
from PIL import Image

from omaform import pages
from omaform.pages import Source


def test_info_counts_pages(w9):
    got = pages.info(w9)
    assert len(got) == 6 and got[0].number == 1 and got[0].rotation == 0


def test_extract_one_page(w9, tmp_path):
    out = tmp_path / "one.pdf"
    assert pages.assemble([Source(w9, 0)], str(out)) == 1
    assert len(pikepdf.open(str(out)).pages) == 1


def test_merge_two_forms_and_reorder(w9, w4, tmp_path):
    out = tmp_path / "packet.pdf"
    pages.assemble([Source(w4, 0), Source(w9, 0), Source(w4, 1)], str(out))
    got = pikepdf.open(str(out))
    assert len(got.pages) == 3
    import subprocess
    first = subprocess.run(["pdftotext", "-f", "1", "-l", "1", str(out), "-"],
                           capture_output=True, text=True).stdout
    second = subprocess.run(["pdftotext", "-f", "2", "-l", "2", str(out), "-"],
                            capture_output=True, text=True).stdout
    assert "W-4" in first and "W-9" in second


def test_delete_is_leaving_a_page_out(w9, tmp_path):
    seq = [s for s in pages.sequence(w9) if s.index != 1]
    out = tmp_path / "less.pdf"
    assert pages.assemble(seq, str(out)) == 5


def test_rotate_quarter_turns(w9, tmp_path):
    out = tmp_path / "rot.pdf"
    pages.assemble([Source(w9, 0).rotated(90), Source(w9, 1).rotated(90).rotated(90)], str(out))
    got = pikepdf.open(str(out))
    assert int(got.pages[0].Rotate) == 90 and int(got.pages[1].Rotate) == 180


def test_an_empty_sequence_is_refused(tmp_path):
    with pytest.raises(ValueError):
        pages.assemble([], str(tmp_path / "x.pdf"))


def test_photos_become_pages(tmp_path):
    tall = tmp_path / "tall.png"
    wide = tmp_path / "wide.png"
    Image.new("RGB", (900, 1400), "white").save(tall)
    Image.new("RGB", (1400, 900), "white").save(wide)
    out = tmp_path / "photos.pdf"
    assert pages.images_to_pdf([str(tall), str(wide)], str(out)) == 2
    got = pikepdf.open(str(out))
    w0, h0 = float(got.pages[0].MediaBox[2]), float(got.pages[0].MediaBox[3])
    w1, h1 = float(got.pages[1].MediaBox[2]), float(got.pages[1].MediaBox[3])
    assert h0 > w0 and w1 > h1


def test_spec_parsing(w9):
    assert [s.index for s in pages.parse_spec(w9)] == [0, 1, 2, 3, 4, 5]
    assert [s.index for s in pages.parse_spec(f"{w9}#1-2,5")] == [0, 1, 4]
    assert pages.parse_spec(f"{w9}#3@90")[0] == Source(w9, 2, 90)
    with pytest.raises(ValueError):
        pages.parse_spec(f"{w9}#9")
    with pytest.raises(ValueError):
        pages.parse_spec(f"{w9}#1@45")


def test_a_merged_packet_of_forms_is_still_fillable(w9, w4, tmp_path):
    """Copied pages carry their widgets but not the form that lists them."""
    import warnings
    out = tmp_path / "packet.pdf"
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # pikepdf warns when widgets are orphaned
        pages.assemble([Source(w9, 0), Source(w4, 0)], str(out))
    got = pikepdf.open(str(out))
    assert "/AcroForm" in got.Root
    widgets = sum(1 for p in got.pages for a in (p.get("/Annots") or [])
                  if a.get("/Subtype") == "/Widget")
    assert widgets > 0 and len(got.Root.AcroForm.Fields) > 0
    assert "/DR" in got.Root.AcroForm
