"""A form with no fields: labels, rules and underscores, and nothing behind them."""

import subprocess

import pytest

from omaform import plan as planning
from omaform.adapters import for_path
from omaform.profile import Profile

def test_labelled_rules_become_blanks(flat_form):
    doc = for_path(flat_form).discover(flat_form)
    labels = sorted(b.label.best() for b in doc.blanks)
    assert any("Name" in l for l in labels)
    assert any("Email" in l for l in labels)
    assert any("Company" in l for l in labels)
    assert any("Phone" in l for l in labels), "an underscore run is a blank too"
    assert not any("Notes" in l for l in labels), "a separator across the page is not"


def test_flat_blanks_fill_and_save(flat_form, tmp_path):
    doc = for_path(flat_form).discover(flat_form)
    profile = Profile({"full_name": "Alex Rivera", "email": "alex@example.com",
                       "phone": "555-0142", "business_name": "Rivera Design Co"})
    result = planning.build(doc, profile)
    keys = {e.match.key for e in result.filled if e.match}
    assert {"full_name", "email", "phone", "business_name"} <= keys
    out = tmp_path / "filled.pdf"
    for_path(flat_form).write(doc, result.values(), str(out), images=result.images())
    text = subprocess.run(["pdftotext", "-layout", str(out), "-"],
                          capture_output=True, text=True, check=True).stdout
    for value in ("Alex Rivera", "alex@example.com", "555-0142", "Rivera Design Co"):
        assert value in text


def test_a_page_with_real_fields_is_left_to_them(w9):
    doc = for_path(w9).discover(w9)
    assert not any(b.id.split(":", 1)[1].startswith("~line") for b in doc.blanks if b.page == 1)
