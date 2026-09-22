"""What a real W-9 needs beyond name and address: the classification box, the
apartment, and a signature and date on a line that has no field behind it."""

import datetime
import subprocess

import pikepdf
import pytest
from PIL import Image

from omaform import plan as planning, signature
from omaform.adapters import for_path
from omaform.model import BlankKind
from omaform.profile import Profile


def field_labels(doc, kinds):
    return {b.id.split(":", 1)[1].split("#")[0]: b.label.best()
            for b in doc.blanks if b.kind in kinds}


# --- checkboxes are labelled to their right ---------------------------------

def test_w9_checkbox_labels_come_from_their_right(w9):
    labels = field_labels(for_path(w9).discover(w9), (BlankKind.CHECKBOX, BlankKind.RADIO))
    assert labels["c1_1[0]"].startswith("Individual/sole proprietor")
    assert labels["c1_1[1]"] == "C corporation"
    assert labels["c1_1[2]"] == "S corporation"
    assert labels["c1_1[3]"] == "Partnership"
    assert labels["c1_1[4]"] == "Trust/estate"
    assert labels["c1_1[5]"].startswith("LLC")


# --- tax classification ticks one box -----------------------------------------

def business(classification: str) -> Profile:
    return Profile({"full_name": "Alex Rivera", "business_name": "Rivera Design Co",
                    "tax_classification": classification}, {"ein": "12-3456789"})


def checked_labels(result):
    return [e.blank.label.best() for e in result.entries if e.value == "checked"]


def test_an_s_corp_ticks_exactly_the_s_corporation_box(w9):
    result = planning.build(for_path(w9).discover(w9), business("s_corp"), prefer={"ein"})
    assert checked_labels(result) == ["S corporation"]


def test_a_c_corp_ticks_exactly_the_c_corporation_box(w9):
    result = planning.build(for_path(w9).discover(w9), business("c_corp"), prefer={"ein"})
    assert checked_labels(result) == ["C corporation"]


def test_an_individual_ticks_the_individual_box(w9):
    result = planning.build(for_path(w9).discover(w9),
                            Profile({"full_name": "Alex Rivera",
                                     "tax_classification": "individual"},
                                    {"ssn": "123-45-6789"}), prefer={"ssn"})
    [label] = checked_labels(result)
    assert label.startswith("Individual/sole proprietor")


def test_an_llc_ticks_the_llc_box_and_writes_its_letter(w9):
    result = planning.build(for_path(w9).discover(w9), business("llc_s"), prefer={"ein"})
    [label] = checked_labels(result)
    assert label.startswith("LLC")
    codes = [e.value for e in result.filled if e.match and e.match.key == "llc_tax_code"]
    assert codes == ["S"]


def test_no_classification_ticks_nothing(w9):
    result = planning.build(for_path(w9).discover(w9),
                            Profile({"full_name": "Alex Rivera"}, {"ssn": "123-45-6789"}),
                            prefer={"ssn"})
    assert checked_labels(result) == []


def test_a_person_starts_as_an_individual_and_a_business_must_choose(library):
    assert library.create("Alex Rivera").values["tax_classification"] == "individual"
    assert "tax_classification" not in library.create("Rivera Design Co",
                                                      kind="business").values


def test_an_unknown_classification_is_refused(library):
    library.create("Alex Rivera")
    with pytest.raises(KeyError):
        library.set_value("alex-rivera", "tax_classification", "megacorp")


def test_the_ticked_box_lands_in_the_pdf(w9, tmp_path):
    doc = for_path(w9).discover(w9)
    result = planning.build(doc, business("s_corp"), prefer={"ein"})
    out = tmp_path / "w9.pdf"
    for_path(w9).write(doc, result.values(), str(out), images=result.images())
    pdf = pikepdf.open(str(out))
    states = {}
    for annot in pdf.pages[0].Annots:
        name = str(annot.get("/T", ""))
        if name.startswith("c1_1"):
            states[name] = str(annot.get("/AS", "/Off"))
    assert states["c1_1[2]"] != "/Off", "the S corporation box is ticked"
    assert all(v == "/Off" for k, v in states.items() if k != "c1_1[2]"), states


# --- the apartment --------------------------------------------------------------

def address_values(result):
    return {e.match.key: e.value for e in result.filled
            if e.match and e.match.key in ("address1", "address2")}


def test_the_apartment_folds_into_the_street_when_the_form_has_no_apartment_box(w9):
    profile = Profile({"full_name": "Alex Rivera", "address1": "2450 Chelsea Pl",
                       "address2": "B"}, {"ssn": "123-45-6789"})
    result = planning.build(for_path(w9).discover(w9), profile, prefer={"ssn"})
    assert address_values(result)["address1"] == "2450 Chelsea Pl, Apt B"


def test_the_apartment_takes_its_own_box_when_the_form_has_one(i9):
    profile = Profile({"full_name": "Alex Rivera", "address1": "2450 Chelsea Pl",
                       "address2": "B"}, {"ssn": "123-45-6789"})
    result = planning.build(for_path(i9).discover(i9), profile, prefer={"ssn"})
    values = address_values(result)
    assert values["address1"] == "2450 Chelsea Pl"
    assert values["address2"] == "B"


def test_a_unit_that_already_says_what_it_is_is_not_prefixed():
    assert Profile({"address1": "1 Main St", "address2": "Suite 300"}).street_line() \
        == "1 Main St, Suite 300"
    assert Profile({"address1": "1 Main St", "address2": "#4"}).street_line() \
        == "1 Main St, #4"
    assert Profile({"address1": "1 Main St"}).street_line() == "1 Main St"


# --- a signature line with no field behind it ---------------------------------

def printed(doc):
    return [b for b in doc.blanks if isinstance(b.native, dict) and b.native.get("synthetic")]


def test_the_w9_gets_a_printed_signature_line_and_a_date_beside_it(w9):
    doc = for_path(w9).discover(w9)
    found = printed(doc)
    assert [(b.page, b.kind) for b in found] == [(1, BlankKind.SIGNATURE), (1, BlankKind.DATE)]
    sig, date = found
    sx0, _, sx1, _ = sig.native["rect"]
    dx0, _, _, _ = date.native["rect"]
    assert 115 < sx0 < 140, "starts just after the 'Signature of U.S. person' label"
    assert sx1 < 386 < dx0, "ends before the printed 'Date', which the date follows"


def test_instructions_that_mention_signatures_do_not_grow_a_line(w9):
    """Page 5 says 'Signature requirements. The...' in running text."""
    assert all(b.page == 1 for b in printed(for_path(w9).discover(w9)))


def test_a_form_with_a_real_signature_field_gains_no_printed_one(i9):
    assert printed(for_path(i9).discover(i9)) == []


def test_signing_a_w9_draws_the_signature_and_todays_date(w9, tmp_path):
    doc = for_path(w9).discover(w9)
    png = signature.render_png([[(0, 0), (60, 25), (120, 5), (180, 30)]])
    result = planning.build(doc, Profile({"full_name": "Alex Rivera"},
                                         {"ssn": "123-45-6789",
                                          "signature": signature.encode(png)}),
                            prefer={"ssn"})
    sig, date = printed(doc)
    assert result.images() == {sig.id: png}
    today = datetime.date.today().strftime("%m/%d/%Y")
    assert result.values()[date.id] == today

    out = tmp_path / "w9.pdf"
    for_path(w9).write(doc, result.values(), str(out), images=result.images())

    text = subprocess.run(["pdftotext", "-f", "1", "-l", "1", str(out), "-"],
                          capture_output=True, text=True, check=True).stdout
    assert today in text, "the date is drawn as real text, not an image"

    subprocess.run(["pdftoppm", "-f", "1", "-l", "1", "-r", "72", "-png", str(out),
                    str(tmp_path / "page")], check=True)
    [page_png] = list(tmp_path.glob("page*.png"))
    image = Image.open(page_png).convert("L")
    page_h = float(doc.native.pages[0].MediaBox[3])
    x0, y0, x1, y1 = sig.native["rect"]
    region = image.crop((int(x0), int(page_h - y1), int(x1), int(page_h - y0)))
    assert sum(1 for v in region.getdata() if v < 90) > 40, "no ink on the signature line"
