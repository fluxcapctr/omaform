"""End to end against the real IRS and USCIS forms.

These assert on actual government PDFs rather than hand-built ones, because
every interesting problem in this program comes from what real forms do:
meaningless field names, no tooltips, two-column layouts, labels that overlap
the boxes they label, and comb fields for tax identification numbers.
"""

import subprocess

import pikepdf
import pytest

from omaform import plan as planning
from omaform.adapters import for_path
from omaform.model import BlankKind


def labels_by_field(doc):
    return {b.id.split(":", 1)[1].split("#")[0]: b.label.best() for b in doc.blanks}


def test_discover_finds_fields_and_pages(w9):
    doc = for_path(w9).discover(w9)
    assert doc.fmt == "pdf"
    assert doc.page_count == 6
    assert len(doc.blanks) > 15
    assert doc.fingerprint.startswith("pdf-")


def test_fingerprint_is_stable(w9):
    assert for_path(w9).discover(w9).fingerprint == for_path(w9).discover(w9).fingerprint


def test_the_w9_carries_no_usable_field_names(w9):
    """The finding that shaped the design: names are opaque, tooltips absent.

    If this ever fails because the IRS started naming fields, the labeler is
    still correct, but the geometry work would no longer be load bearing.
    """
    doc = for_path(w9).discover(w9)
    names = [b.label.native_name for b in doc.blanks]
    assert any(n.startswith("f1_") for n in names)
    assert all(not b.label.native_name.lower().startswith("name") for b in doc.blanks)


@pytest.mark.parametrize("field,expected", [
    ("f1_01[0]", "name of entity"),
    ("f1_02[0]", "business name"),
    ("f1_07[0]", "address"),
    ("f1_08[0]", "city, state, and zip"),
    ("f1_11[0]", "social security number"),
    ("f1_14[0]", "employer identification number"),
])
def test_labels_come_from_the_page(w9, field, expected):
    labels = labels_by_field(for_path(w9).discover(w9))
    assert expected in labels[field].lower()


def test_tin_boxes_group(w9):
    doc = for_path(w9).discover(w9)
    groups = doc.groups()
    sizes = sorted(len(m) for m in groups.values())
    assert sizes == [2, 3], f"expected a 3-box SSN run and a 2-box EIN run, got {sizes}"


def test_first_and_last_name_are_not_grouped(w4):
    """Adjacent same-height boxes with *different* labels must stay separate."""
    doc = for_path(w4).discover(w4)
    for members in doc.groups().values():
        labels = {b.label.best().lower() for b in members if b.label.best()}
        assert not ({"last name"} & labels and len(labels) > 1)


def test_plan_fills_the_expected_w9_fields(w9, sample_profile):
    doc = for_path(w9).discover(w9)
    result = planning.build(doc, sample_profile, prefer={"ssn"})
    keys = {e.match.key for e in result.filled if e.match}
    assert {"full_name", "business_name", "address1", "city_state_zip", "ssn"} <= keys


def test_plan_leaves_the_requester_and_account_boxes_alone(w9, sample_profile):
    """Field ids, not label text: the address label legitimately has the next
    column's "Requester's" bleeding into its tail, and the matcher is right to
    ignore that. What matters is that the requester's own boxes stay empty."""
    doc = for_path(w9).discover(w9)
    result = planning.build(doc, sample_profile, prefer={"ssn"})
    filled = {e.blank.id.split(":", 1)[1].split("#")[0] for e in result.filled}
    assert "f1_09[0]" not in filled, "requester's name and address must stay empty"
    assert "f1_10[0]" not in filled, "the account number list must stay empty"


def test_ssn_is_split_across_the_comb_boxes(w9, sample_profile):
    doc = for_path(w9).discover(w9)
    result = planning.build(doc, sample_profile, prefer={"ssn"})
    parts = [e.value for e in result.filled if e.match and e.match.key == "ssn"]
    assert parts == ["123", "45", "6789"]


def test_sensitive_use_is_reported(w9, sample_profile):
    result = planning.build(for_path(w9).discover(w9), sample_profile,
                            prefer={"ssn"})
    assert "ssn" in result.sensitive_used


def test_fill_writes_values_and_leaves_the_original_alone(w9, sample_profile, tmp_path):
    before = open(w9, "rb").read()
    doc = for_path(w9).discover(w9)
    result = planning.build(doc, sample_profile, prefer={"ssn"})
    out = tmp_path / "filled.pdf"
    for_path(w9).write(doc, result.values(), str(out))

    assert open(w9, "rb").read() == before, "the source document must never be modified"

    written = pikepdf.open(str(out))
    assert written.Root.AcroForm.get("/NeedAppearances") is True
    values = {}
    for page in written.pages:
        for annot in page.get("/Annots", []) or []:
            field = annot if "/T" in annot else annot.get("/Parent")
            if field is not None and "/V" in field:
                values[str(field.get("/T"))] = str(field["/V"])
    assert values["f1_01[0]"] == "Eric Stevens"
    assert values["f1_08[0]"] == "Springfield, OR 97403"
    assert [values["f1_11[0]"], values["f1_12[0]"], values["f1_13[0]"]] == ["123", "45", "6789"]


def test_filled_values_actually_render(w9, sample_profile, tmp_path):
    """A stored value a viewer will not draw is not a filled form.

    Poppler regenerates appearances from /NeedAppearances, so extracting text
    from the output proves a real renderer shows the values, not merely that
    they were stored in the field dictionaries.
    """
    doc = for_path(w9).discover(w9)
    result = planning.build(doc, sample_profile, prefer={"ssn"})
    out = tmp_path / "filled.pdf"
    for_path(w9).write(doc, result.values(), str(out))

    # pdftotext, not our own extractor: page text and widget appearances are
    # separate layers, and only an independent renderer proves the appearance
    # was generated from /NeedAppearances at all.
    rendered = subprocess.run(["pdftotext", "-f", "1", "-l", "1", str(out), "-"],
                              capture_output=True, text=True, check=True).stdout
    assert "Eric Stevens" in rendered
    assert "1200 Maple Avenue Apt 4" in rendered
    assert "Springfield, OR 97403" in rendered


def test_checkbox_kinds_are_recognised(w9):
    doc = for_path(w9).discover(w9)
    assert any(b.kind in (BlankKind.CHECKBOX, BlankKind.RADIO) for b in doc.blanks)


def test_unknown_extension_is_refused(tmp_path):
    target = tmp_path / "thing.xyz"
    target.write_text("")
    with pytest.raises(ValueError, match="no adapter"):
        for_path(str(target))


def test_a_form_offering_ssn_or_ein_fills_neither_without_a_preference(w9, sample_profile):
    """The W-9 prints a literal "or" between the two identification rows."""
    result = planning.build(for_path(w9).discover(w9), sample_profile)
    keys = {e.match.key for e in result.filled if e.match}
    assert "ssn" not in keys and "ein" not in keys
    assert any(group == frozenset({"ssn", "ein"}) for group in result.conflicts)


def test_a_preference_resolves_the_either_or(w9, sample_profile):
    result = planning.build(for_path(w9).discover(w9), sample_profile, prefer={"ein"})
    keys = {e.match.key for e in result.filled if e.match}
    assert "ein" in keys and "ssn" not in keys


# --- whose tax number goes in the box ---------------------------------------

def _profile_with_both():
    from omaform.profile import Profile
    return Profile({"full_name": "Eric Stevens"},
                   {"ssn": "123-45-6789", "ein": "12-3456789"})


def test_a_person_gets_their_social_security_number(w9):
    """No prompt, no conflict: the W-9's either/or is answered by who is filing."""
    from omaform.profile import Identity

    identity = Identity("me", "Eric Stevens", {}, kind="person")
    result = planning.build(for_path(w9).discover(w9), _profile_with_both(),
                            identity.preference())
    keys = {e.match.key for e in result.filled if e.match}
    assert "ssn" in keys and "ein" not in keys
    assert not result.conflicts


def test_a_business_gets_its_employer_identification_number(w9):
    from omaform.profile import Identity

    identity = Identity("biz", "Eric Stevens Design", {}, kind="business")
    result = planning.build(for_path(w9).discover(w9), _profile_with_both(),
                            identity.preference())
    keys = {e.match.key for e in result.filled if e.match}
    assert "ein" in keys and "ssn" not in keys
    assert not result.conflicts


# --- gaps the form points out -----------------------------------------------

def test_missing_reports_what_the_form_wants_and_you_have_not_got(w9):
    """The whole reason a W-9 went out without a tax number: nothing said so."""
    from omaform.profile import Profile

    result = planning.build(for_path(w9).discover(w9),
                            Profile({"full_name": "Eric Stevens"}), prefer={"ssn"})
    assert "ssn" in result.missing()
    assert "business_name" in result.missing()


def test_missing_does_not_include_the_side_that_was_ruled_out(w9):
    """You are not missing an EIN if you file under a Social Security number."""
    from omaform.profile import Profile

    result = planning.build(for_path(w9).discover(w9),
                            Profile({}, {"ssn": "123-45-6789"}), prefer={"ssn"})
    assert "ein" not in result.missing()
    assert "ssn" not in result.missing(), "it is stored, so it is not missing"


def test_nothing_is_missing_once_everything_is_stored(w9, sample_profile):
    result = planning.build(for_path(w9).discover(w9), sample_profile,
                            prefer={"ssn"})
    assert "ssn" not in result.missing()
