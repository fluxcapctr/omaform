"""Choices that tick boxes on the W-4 and I-9, and a form remembered once saved."""

import json

from omaform import memory, plan as planning
from omaform.adapters import for_path
from omaform.adapters.pdf import placed_by_hand
from omaform.model import BlankKind
from omaform.profile import CHOICES, SENSITIVE_KEYS, Library, Profile


def ticked(result):
    return [e.blank.label.best() for e in result.entries if e.value == "checked"]


# --- filing status on the W-4 ------------------------------------------------

def test_married_filing_jointly_ticks_its_box_only(w4):
    profile = Profile({"first_name": "Alex", "filing_status": "married_jointly"},
                      {"ssn": "1"})
    result = planning.build(for_path(w4).discover(w4), profile, prefer={"ssn"})
    assert [b[:22] for b in ticked(result)] == ["Married filing jointly"]


def test_single_and_married_separately_share_the_first_box(w4):
    for status in ("single", "married_separately"):
        profile = Profile({"first_name": "Alex", "filing_status": status}, {"ssn": "1"})
        result = planning.build(for_path(w4).discover(w4), profile, prefer={"ssn"})
        [label] = ticked(result)
        assert label.startswith("Single or Married filing separately"), status


def test_head_of_household(w4):
    profile = Profile({"first_name": "Alex", "filing_status": "head_of_household"}, {"ssn": "1"})
    result = planning.build(for_path(w4).discover(w4), profile, prefer={"ssn"})
    assert [b[:17] for b in ticked(result)] == ["Head of household"]


def test_no_filing_status_ticks_nothing_and_says_so(w4):
    result = planning.build(for_path(w4).discover(w4),
                            Profile({"first_name": "Alex"}, {"ssn": "1"}), prefer={"ssn"})
    assert ticked(result) == []
    assert "filing_status" in result.missing()


# --- citizenship on the I-9, from the vault ------------------------------------

def test_citizenship_is_a_vault_key():
    assert "citizenship" in SENSITIVE_KEYS
    assert "citizen" in CHOICES["citizenship"]


def test_a_citizen_ticks_box_one_only(i9):
    profile = Profile({"first_name": "Alex"}, {"ssn": "1", "citizenship": "citizen"})
    result = planning.build(for_path(i9).discover(i9), profile, prefer={"ssn"})
    assert [b[:35] for b in ticked(result)] == ["1. A citizen of the United States"]


def test_an_authorised_noncitizen_ticks_box_four(i9):
    profile = Profile({"first_name": "Alex"}, {"ssn": "1", "citizenship": "authorized_alien"})
    result = planning.build(for_path(i9).discover(i9), profile, prefer={"ssn"})
    assert [b[:22] for b in ticked(result)] == ["4. An alien authorized"]


def test_a_plain_profile_refuses_citizenship(library):
    library.create("Me")
    import pytest
    with pytest.raises(PermissionError):
        library.set_value("me", "citizenship", "citizen")


def test_an_unknown_answer_is_refused(library):
    library.create("Me")
    import pytest
    with pytest.raises(KeyError):
        library.set_value("me", "filing_status", "complicated")


# --- remembering a form --------------------------------------------------------

PROFILE = Profile({"full_name": "Alex Rivera", "business_name": "Rivera Design Co",
                   "address1": "742 Evergreen Terrace", "city": "Springfield", "state": "OR",
                   "zip": "97403"}, {"ssn": "123-45-6789"})


def test_saving_remembers_structure_and_never_values(w9, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    doc = for_path(w9).discover(w9)
    result = planning.build(doc, PROFILE, prefer={"ssn"})
    # clear one, tick a box by hand, type something for the occasion, place a signature
    cleared = next(e for e in result.filled if e.match and e.match.key == "business_name")
    cleared.value, cleared.note, cleared.cleared = None, "cleared by you", True
    box = next(e for e in result.entries if e.blank.kind is BlankKind.CHECKBOX and not e.filled)
    box.value = "checked"
    typed = next(e for e in result.entries if not e.filled and e.blank.kind is BlankKind.TEXT
                 and e.match is None)
    typed.value = "one-off note"
    hand = placed_by_hand(1, BlankKind.SIGNATURE, 300.0, 100.0)
    doc.blanks.append(hand)

    memory.remember(doc, result, "alex-rivera")
    [path] = list((tmp_path / "forms").glob("*.json"))
    raw = path.read_text()
    for value in ("Alex Rivera", "742 Evergreen", "123-45-6789", "one-off note"):
        assert value not in raw, f"{value!r} was written to the memory file"

    kept = memory.recall(doc)
    assert kept.keys[next(e.blank.id for e in result.filled if e.match and e.match.key == "full_name")] == "full_name"
    assert box.blank.id in kept.checked["alex-rivera"]
    assert cleared.blank.id in kept.cleared["alex-rivera"]
    assert typed.blank.id not in kept.keys
    assert kept.placed == [{"page": 1, "kind": "signature", "x": 300.0, "y": 100.0}]


def test_a_remembered_form_fills_the_same_way_next_time(w9, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    doc = for_path(w9).discover(w9)
    result = planning.build(doc, PROFILE, prefer={"ssn"})
    cleared = next(e for e in result.filled if e.match and e.match.key == "business_name")
    cleared.value, cleared.note, cleared.cleared = None, "cleared by you", True
    box = next(e for e in result.entries if e.blank.kind is BlankKind.CHECKBOX and not e.filled)
    box.value = "checked"
    doc.blanks.append(placed_by_hand(1, BlankKind.SIGNATURE, 300.0, 100.0))
    memory.remember(doc, result, "alex-rivera")

    fresh_doc = for_path(w9).discover(w9)
    kept = memory.recall(fresh_doc)
    assert memory.restore_placements(kept, fresh_doc) == 1
    fresh = planning.build(fresh_doc, PROFILE, prefer={"ssn"})
    changed = memory.apply(kept, fresh, PROFILE, "alex-rivera")
    by_id = {e.blank.id: e for e in fresh.entries}
    assert not by_id[cleared.blank.id].filled and "as last time" in by_id[cleared.blank.id].note
    assert by_id[box.blank.id].value == "checked"
    assert changed, "something was applied"
    # Another identity gets neither this one's cleared blanks nor its hand ticks.
    other = planning.build(fresh_doc, PROFILE, prefer={"ssn"})
    memory.apply(kept, other, PROFILE, "somebody-else")
    others = {e.blank.id: e for e in other.entries}
    assert others[cleared.blank.id].filled, "cleared by alex, not by somebody else"
    assert others[box.blank.id].value != "checked", "ticked by alex, not by somebody else"


def test_forgetting_reads_fresh(w9, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    doc = for_path(w9).discover(w9)
    memory.remember(doc, planning.build(doc, PROFILE, prefer={"ssn"}), "me")
    assert memory.recall(doc) is not None
    memory.forget(doc)
    assert memory.recall(doc) is None


def test_a_dragged_signature_is_remembered_by_how_far(w9, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    from omaform import memory, plan as planning
    from omaform.adapters import for_path
    from omaform.model import BlankKind
    from omaform.profile import Profile

    doc = for_path(w9).discover(w9)
    sig = next(b for b in doc.blanks if b.kind is BlankKind.SIGNATURE)
    sig.offset = (12.0, -4.0)
    memory.remember(doc, planning.build(doc, Profile({"full_name": "Alex Rivera"})), "alex")
    fresh = for_path(w9).discover(w9)
    memory.restore_placements(memory.recall(fresh), fresh)
    assert next(b for b in fresh.blanks if b.id == sig.id).offset == (12.0, -4.0)
