"""The matcher's job is to be right, and to shut up when it is not sure."""

import pytest

from omaform.matching import THRESHOLD, normalize, rank
from omaform.model import Blank, BlankKind, LabelContext
from omaform.matching import distribute, match_blank
from omaform.profile import BY_KEY


def top(text: str) -> str | None:
    """Pure ranking of one label string, with no blank context."""
    hits = rank(text)
    return hits[0].key if hits and hits[0].score >= THRESHOLD else None


def decides(text: str, width: float = 200.0, section: str = "") -> str | None:
    """The real decision path: ranking plus the blank-level vetoes.

    Whose-blank-is-this questions are settled in match_blank, not in rank, so a
    test that calls rank alone is testing the wrong layer.
    """
    from omaform.profile import Profile

    blank = Blank(id="b", kind=BlankKind.TEXT, width_pt=width,
                  label=LabelContext(left=text, section=section))
    everything = Profile({f.key: "x" for f in __import__(
        "omaform.profile", fromlist=["SCHEMA"]).SCHEMA})
    match = match_blank(blank, everything)
    return match.key if match else None


@pytest.mark.parametrize("label,key", [
    # Real W-9 and W-4 label text, as the labeler actually extracts it.
    ("1 Name of entity/individual. An entry is required.", "full_name"),
    ("2 Business name/disregarded entity name, if different from above", "business_name"),
    ("6 City, state, and ZIP code", "city_state_zip"),
    ("Social security number", "ssn"),
    ("Employer identification number", "ein"),
    ("See 5Address (number, street, and apt. or suite no.). See instructions.", "address1"),
    ("City or town, state, and ZIP code", "city_state_zip"),
    ("Last name", "last_name"),
    ("Home address", "address1"),
    ("Daytime phone number", "phone"),
    ("Email address", "email"),
])
def test_matches(label, key):
    assert top(label) == key


@pytest.mark.parametrize("label", [
    # Things that look fillable and are not. Filling any of these is worse than
    # leaving it blank, because it is wrong on a document you signed.
    "7 List account number(s) here (optional)",
    "Requester’s name and address (optional)",
    "4 Exemptions (codes apply only to certain entities)",
    "Individual/sole proprietor C corporation S corporation Partnership",
    "Exemption from Foreign Account Tax Compliance Act (FATCA) reporting",
    "Enter the amount from line 7",
])
def test_declines(label):
    assert top(label) is None


def test_leading_qualifier_vetoes_but_trailing_does_not():
    # "Requester's" leads, so it disqualifies the name beside it.
    assert top("Requester’s name and address") is None
    # The same word trailing far behind is the next column bleeding in, and
    # must not disqualify the label that actually starts the line.
    assert top("5Address (number, street, and apt. or suite no.). "
               "See instructions. Requester’s") == "address1"


def test_business_name_is_not_the_name_key():
    assert top("Business name/disregarded entity name") == "business_name"


def test_glued_line_numbers_are_split():
    assert "address" in normalize("5Address (number, street)")


def test_more_specific_alias_wins_a_tie():
    blank = Blank(id="b", kind=BlankKind.TEXT, width_pt=180,
                  label=LabelContext(above="(a) First name and middle initial"))
    from omaform.profile import Profile
    match = match_blank(blank, Profile({"first_name": "Eric", "middle_initial": "J"}))
    assert match is not None and match.key == "first_middle"


def test_capacity_penalises_a_value_that_cannot_fit():
    from omaform.profile import Profile
    profile = Profile({"state": "OR", "address1": "1200 Maple Avenue Apt 4"})
    narrow = Blank(id="n", kind=BlankKind.TEXT, width_pt=14,
                   label=LabelContext(left="State"))
    assert match_blank(narrow, profile).key == "state"


def test_split_uses_the_canonical_grouping_not_pixel_widths():
    members = [Blank(id=f"b{i}", kind=BlankKind.TEXT, width_pt=w, group="g", group_index=i)
               for i, w in enumerate((43, 29, 58))]
    parts = distribute("123-45-6789", members, BY_KEY["ssn"])
    assert [parts["b0"], parts["b1"], parts["b2"]] == ["123", "45", "6789"]


def test_split_falls_back_to_width_when_the_box_count_disagrees():
    members = [Blank(id=f"b{i}", kind=BlankKind.TEXT, width_pt=50, group="g", group_index=i)
               for i in range(2)]
    parts = distribute("123456", members, BY_KEY["ssn"])
    assert "".join(parts[f"b{i}"] for i in range(2)) == "123456"


# --- whose blank is this ---------------------------------------------------
# Every case below is a wrong answer on a signed document, not a cosmetic miss.

@pytest.mark.parametrize("label", [
    "Preparer or Translator ZIP Code",
    "Preparer or Translator Last Name (Family Name)",
    "Employer's Business or Organization Address",
    "Employer's Telephone Number",
    "Spouse's Social Security Number",
    "Witness Name",
    "Notary Public Address",
])
def test_declines_other_parties_fields(label):
    assert decides(label) is None


def test_ein_is_exempt_from_the_employer_veto():
    """An EIN is "employer identification number" about you, so this one key
    has to survive a veto that every other key needs."""
    assert decides("Employer identification number") == "ein"
    assert decides("Enter your EIN") == "ein"


@pytest.mark.parametrize("label", [
    "Date of Rehire (if applicable)",
    "Expiration Date",
    "Document Exp. Date",
    "Date of Birth",
])
def test_declines_dates_that_are_not_today(label):
    assert decides(label) != "date_today"


def test_other_last_names_used_is_not_your_last_name():
    """A maiden-name box is not your last name, and the I-9 has one."""
    assert decides("Enter Other Last Names Used (if any)") is None


def test_single_word_alias_survives_a_preamble():
    """Regression: a steeper dilution curve pushed this under THRESHOLD."""
    assert top("Section 1., Enter Address (Street Number and Name).") == "address1"
    assert top("Section 1., Enter City or Town.") == "city"


def test_section_heading_naming_another_party_blocks_the_whole_blank():
    from omaform.profile import Profile
    blank = Blank(id="b", kind=BlankKind.TEXT, width_pt=200,
                  label=LabelContext(left="Date (mm/dd/yyyy)",
                                     section="Section 2. Employer Review and Verification"))
    assert match_blank(blank, Profile({})) is None


def test_the_employees_own_section_is_not_blocked_by_its_own_prose():
    """The I-9's section 1 heading continues "...Employers must ensure...", and
    a looser section test handed the employee's whole section to the employer."""
    from omaform.profile import Profile
    blank = Blank(id="b", kind=BlankKind.TEXT, width_pt=200,
                  label=LabelContext(
                      left="Section 1., Enter City or Town.",
                      section="Section 1. Employee Information and Attestation: "
                              "Employees must complete and sign"))
    match = match_blank(blank, Profile({"city": "Springfield"}))
    assert match is not None and match.key == "city"


# --- a signature is not its date, and a supplement is somebody's section ------

def test_a_signature_date_is_not_a_signature():
    assert decides("Signature Date mm/dd/yyyy") != "signature"
    assert decides("Enter Today's date of signature") != "signature"


def test_the_party_can_be_named_in_the_sentence_not_just_the_opening():
    """The I-9's Supplement B line opens with a long section name and only
    says whose signature it is in its second sentence."""
    label = ("Supplement B, Reverification and Rehire (formerly Section 3). "
             "Enter Signature of Employer or Authorized Representative.")
    assert decides(label) is None


def test_an_aside_in_a_later_sentence_does_not_veto_your_own_line():
    """The employee's own line mentions the preparer in a third sentence."""
    label = ("Section 1. Employee Information and Attestation. Enter Signature "
             "of Employee. If a preparer and/or translator assisted you, they "
             "must complete Supplement A.")
    assert decides(label) == "signature"


def test_a_supplement_heading_scopes_its_blanks_to_that_party():
    from omaform.profile import Profile
    blank = Blank(id="b", kind=BlankKind.TEXT, width_pt=100,
                  label=LabelContext(left="Signature Date mm/dd/yyyy",
                                     section="Supplement A. Preparer and/or "
                                             "Translator Certification"))
    assert match_blank(blank, Profile({"full_name": "x"})) is None
