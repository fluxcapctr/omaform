"""A scanned form: no text layer, no drawing, just a picture of a page."""

import subprocess

import pytest

from omaform import pages, plan as planning, scan
from omaform.adapters import for_path
from omaform.profile import Profile

pytestmark = pytest.mark.skipif(not scan.available(), reason="needs tesseract and pdftoppm")


@pytest.fixture
def scanned_form(flat_form, tmp_path):
    """The cairo form rendered to pixels and wrapped back into a PDF."""
    subprocess.run(["pdftoppm", "-r", "150", "-png", flat_form, str(tmp_path / "shot")],
                   check=True)
    [png] = list(tmp_path.glob("shot*.png"))
    out = tmp_path / "scanned.pdf"
    pages.images_to_pdf([str(png)], str(out))
    return str(out)


def test_a_scanned_page_is_recognised_as_one(scanned_form):
    from omaform import textmap
    [page] = textmap.read_pdf(scanned_form)
    assert scan.looks_scanned(page)


def test_ocr_finds_the_labels_and_the_rules(scanned_form):
    doc = for_path(scanned_form).discover(scanned_form)
    labels = " | ".join(b.label.best() for b in doc.blanks)
    assert "Name" in labels and "Email" in labels
    assert all(b.native.get("scanned") for b in doc.blanks)


def test_scanned_blanks_are_offered_not_written(scanned_form):
    doc = for_path(scanned_form).discover(scanned_form)
    result = planning.build(doc, Profile({"full_name": "Alex Rivera", "email": "a@example.com"}))
    assert result.filled == []
    keys = {e.suggested_key for e in result.suggested}
    assert "full_name" in keys and "email" in keys
    assert all("scan" in e.note for e in result.suggested)


def test_an_accepted_scan_blank_is_written_onto_the_picture(scanned_form, tmp_path):
    doc = for_path(scanned_form).discover(scanned_form)
    result = planning.build(doc, Profile({"full_name": "Alex Rivera"}))
    name = next(e for e in result.suggested if e.suggested_key == "full_name")
    name.value = "Alex Rivera"  # what Accept does in the window
    out = tmp_path / "filled.pdf"
    for_path(scanned_form).write(doc, result.values(), str(out))
    text = subprocess.run(["pdftotext", "-layout", str(out), "-"],
                          capture_output=True, text=True, check=True).stdout
    assert "Alex Rivera" in text
