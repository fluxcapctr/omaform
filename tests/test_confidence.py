"""Only fill what is certain: every wrong fill the audit found, pinned.

The audit that prompted this compared each fill on the three real forms against
what was verified by eye. The wrong ones sat at the same scores as right ones,
so none of this is a threshold: each is a rule about whose box a blank is, or
about what counts as a match at all.
"""

from omaform import plan as planning
from omaform.adapters import for_path
from omaform.matching import match_blank, normalize
from omaform.model import Blank, BlankKind, LabelContext
from omaform.profile import Profile

EVERYTHING = Profile({"full_name": "Alex Rivera", "first_name": "Alex", "middle_initial": "J",
                      "last_name": "Rivera", "business_name": "Rivera Design Co",
                      "address1": "742 Evergreen Terrace", "address2": "B",
                      "city": "Springfield", "state": "OR", "zip": "97403",
                      "phone": "555-0142", "email": "a@example.com",
                      "tax_classification": "s_corp"},
                     {"ssn": "123-45-6789", "ein": "12-3456789", "dob": "03/14/1985"})


def fills(path):
    doc = for_path(path).discover(path)
    result = planning.build(doc, EVERYTHING, prefer={"ssn"})
    return doc, [e for e in result.filled if e.match and e.image is None and e.value != "checked"]


def field_of(entry) -> str:
    return entry.blank.id.split(":", 1)[1].split("#")[0]


# --- the I-9: section 1 is yours, nothing else is ------------------------------

def test_i9_fills_only_page_one(i9):
    _doc, filled = fills(i9)
    assert filled, "section 1 should still fill"
    assert {e.blank.page for e in filled} == {1}, \
        "Supplement A (preparer) and Supplement B (employer rehire) must stay empty"


def test_i9_preparers_signature_date_is_not_todays_date(i9):
    _doc, filled = fills(i9)
    dated = [e for e in filled if e.match.key == "date_today"]
    assert dated and all("Section 1" in e.blank.label.left for e in dated)


# --- the W-4: the employee's block, not the employer's --------------------------

def test_w4_address_line_gets_the_address_not_the_name(w4):
    _doc, filled = fills(w4)
    by_field = {field_of(e): e.match.key for e in filled}
    assert by_field["f1_03[0]"] == "address1"


def test_w4_employers_only_block_stays_empty(w4):
    """Employer's name and address, first date of employment, employer's EIN."""
    _doc, filled = fills(w4)
    touched = {field_of(e) for e in filled} & {"f1_12[0]", "f1_13[0]", "f1_14[0]"}
    assert touched == set(), f"filled the employer's boxes: {touched}"


# --- what counts as a match --------------------------------------------------

def test_a_fuzzy_match_is_offered_not_written():
    blank = Blank(id="b", kind=BlankKind.TEXT, width_pt=200,
                  label=LabelContext(left="Sociall securty nmber"))
    match = match_blank(blank, EVERYTHING)
    assert match is not None and match.key == "ssn" and not match.exact
    doc = type("D", (), {"blanks": [blank], "groups": lambda self: {}})()
    result = planning.build(doc, EVERYTHING, prefer={"ssn"})
    [entry] = result.entries
    assert not entry.filled and entry.suggested_key == "ssn"
    assert result.suggested == [entry]


def test_an_only_block_beats_every_exemption():
    """The EIN key is allowed the word employer; not inside 'Employers Only'."""
    inside = Blank(id="b", kind=BlankKind.TEXT, width_pt=100,
                   label=LabelContext(above="Employer identification number (EIN)",
                                      row="Only Employer\u2019s employment number (EIN)"))
    assert match_blank(inside, EVERYTHING) is None
    yours = Blank(id="c", kind=BlankKind.TEXT, width_pt=100,
                  label=LabelContext(above="Employer identification number",
                                     row="or Employer identification number"))
    assert match_blank(yours, EVERYTHING).key == "ein"


def test_a_glued_rotated_title_is_split_into_words():
    assert normalize("OnlyEmployer\u2019s employment")[:3] == ["only", "employer", "s"]


def test_a_two_line_heading_carries_its_second_line(i9):
    doc = for_path(i9).discover(i9)
    page3 = [b for b in doc.blanks if b.page == 3 and b.label.section]
    assert page3 and all("Preparer" in b.label.section for b in page3)
