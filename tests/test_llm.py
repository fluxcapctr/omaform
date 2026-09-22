"""The model layer: what it is told, what it may do, and what it may never do."""

import json

import pytest

from omaform import llm, plan as planning
from omaform.adapters import for_path
from omaform.profile import Profile

SECRETS = {"ssn": "987-65-4321", "ein": "98-7654321"}
DETAILS = {"full_name": "Zelda Quillfeather", "business_name": "Quillfeather Ltd",
           "address1": "9 Unlikely Lane", "city": "Bexleyheath", "state": "OR",
           "zip": "97403", "tax_classification": "s_corp"}


def w9_plan(w9):
    doc = for_path(w9).discover(w9)
    profile = Profile(dict(DETAILS), dict(SECRETS))
    result = planning.build(doc, profile, prefer={"ein"})
    return doc, profile, result


def test_the_prompt_never_contains_a_value(w9):
    """The whole privacy argument rests on this."""
    doc, _profile, result = w9_plan(w9)
    filled = {e.blank.id: e.match.key for e in result.filled if e.match}
    prompt = llm.build_prompt(doc, "business", filled)
    for value in list(DETAILS.values()) + list(SECRETS.values()):
        assert value not in prompt, f"{value!r} leaked into the prompt"
    assert "ssn" in prompt and "business_name" in prompt, "key names are what it gets"


def test_the_prompt_carries_the_forms_questions(w9):
    doc, _profile, result = w9_plan(w9)
    prompt = llm.build_prompt(doc, "business", {})
    assert "City, state, and ZIP code" in prompt
    assert "Social security number" in prompt


def test_parse_takes_json_from_chatter_and_drops_nonsense(w9):
    doc, _profile, _result = w9_plan(w9)
    good = doc.blanks[0].id
    q = llm.question_ids(doc)   # the model only ever sees these numbers
    text = ("Sure! Here you go:\n"
            + json.dumps({"blanks": [{"id": q[good], "key": "full_name", "why": "line 1"},
                                     {"id": "q99999", "key": "city"},
                                     {"id": good, "key": "city"},
                                     {"id": q[doc.blanks[1].id], "key": "made_up_key"},
                                     {"id": q[doc.blanks[2].id], "key": None, "why": "n/a"}],
                          "questions": ["Are you a resident alien?"]})
            + "\nHope that helps.")
    reading = llm.parse_answer(text, doc, "test")
    assert reading.mapping[good] == "full_name"
    assert set(reading.mapping) == {good, doc.blanks[2].id}, "raw ids and unknown numbers ignored"
    assert doc.blanks[1].id not in reading.mapping, "unknown keys are refused"
    assert reading.mapping[doc.blanks[2].id] is None
    assert reading.questions == ["Are you a resident alien?"]


def test_parse_refuses_non_json(w9):
    doc, _profile, _result = w9_plan(w9)
    with pytest.raises(llm.ModelError):
        llm.parse_answer("I would rather not.", doc, "test")


def test_apply_adds_a_fill_the_matcher_missed_from_the_profile_only(w9):
    doc, profile, result = w9_plan(w9)
    empty = next(e for e in result.entries if not e.filled and e.blank.kind.value == "text")
    reading = llm.Reading(mapping={empty.blank.id: "phone"}, reasons={}, backend="t")
    changed, notes = llm.apply(reading, doc, result, profile)
    # phone is not stored, so nothing can be invented for it
    assert not empty.filled and changed == []
    assert any("Phone" in n for n in notes)

    reading = llm.Reading(mapping={empty.blank.id: "city"}, backend="t")
    changed, _ = llm.apply(reading, doc, result, profile)
    assert empty.value == "Bexleyheath" and changed == [empty]


def test_apply_can_clear_what_the_matcher_filled(w9):
    doc, profile, result = w9_plan(w9)
    filled = next(e for e in result.filled if e.match and e.match.key == "full_name")
    reading = llm.Reading(mapping={filled.blank.id: None},
                          reasons={filled.blank.id: "only if line 4 applies"}, backend="t")
    changed, _ = llm.apply(reading, doc, result, profile)
    assert not filled.filled and "line 4" in filled.note and changed == [filled]


def test_apply_never_puts_a_secret_the_model_asked_for_without_it_being_stored(w9):
    doc, _profile, result = w9_plan(w9)
    profile = Profile(dict(DETAILS))  # no secrets at all
    empty = next(e for e in result.entries if not e.filled and e.blank.kind.value == "text")
    reading = llm.Reading(mapping={empty.blank.id: "ssn"}, backend="t")
    changed, notes = llm.apply(reading, doc, result, profile)
    assert changed == [] and not empty.filled
    # Asked for, and not stored: pointed out, unless the planner ruled SSN out
    # for this form, in which case the model cannot bring it back either.
    assert empty.missing_key == "ssn" or "ssn" in result.ruled_out


def test_the_model_cannot_touch_a_checkbox(w9):
    """It ticked 'Individual' for an S corporation once, because the stored
    classification is a fact it is never shown. Boxes are not its to decide."""
    doc, profile, result = w9_plan(w9)
    ticked = next(e for e in result.entries if e.value == "checked")
    empty = next(e for e in result.entries if e.blank.kind.value == "checkbox" and not e.filled)
    reading = llm.Reading(mapping={ticked.blank.id: None, empty.blank.id: "full_name"},
                          backend="t")
    changed, _ = llm.apply(reading, doc, result, profile)
    assert changed == []
    assert ticked.value == "checked" and not empty.filled


def test_the_prompt_tells_the_model_to_leave_checkboxes_alone(w9):
    doc, _profile, _result = w9_plan(w9)
    assert "null for every checkbox" in llm.build_prompt(doc, "business", {})


def test_a_reading_is_remembered_per_form(w9, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    doc, _profile, _result = w9_plan(w9)
    assert llm.remembered(doc) is None
    reading = llm.Reading(mapping={doc.blanks[0].id: "full_name"}, questions=["q?"],
                          backend="ollama:test")
    llm.remember(doc, reading)
    back = llm.remembered(doc)
    assert back is not None and back.mapping == reading.mapping
    assert back.questions == ["q?"] and back.backend == "ollama:test"
    llm.forget(doc)
    assert llm.remembered(doc) is None


def test_read_form_refuses_an_unknown_backend(w9):
    doc, _profile, _result = w9_plan(w9)
    with pytest.raises(llm.ModelError):
        llm.read_form(doc, "person", {}, "carrier-pigeon")


def test_the_prompt_shows_the_model_what_the_matcher_did(w9):
    """An audit needs the first reader's answers. Key names only, as ever."""
    doc, _profile, result = w9_plan(w9)
    filled = {e.blank.id: e.match.key for e in result.filled if e.match}
    prompt = llm.build_prompt(doc, "business", filled)
    assert '"matcher_put": "business_name"' in prompt
    assert "when in doubt, clear" in prompt


def test_apply_corrects_a_fill_to_a_different_key(w9):
    doc, profile, result = w9_plan(w9)
    entry = next(e for e in result.filled if e.match and e.match.key == "full_name")
    reading = llm.Reading(mapping={entry.blank.id: "business_name"},
                          reasons={entry.blank.id: "line 1 wants the entity name here"},
                          backend="t")
    changed, _ = llm.apply(reading, doc, result, profile)
    assert changed == [entry]
    assert entry.value == "Quillfeather Ltd"
    assert "corrected by the model from full_name" in entry.note


def test_apply_leaves_an_agreed_fill_untouched(w9):
    doc, profile, result = w9_plan(w9)
    entry = next(e for e in result.filled if e.match and e.match.key == "full_name")
    reading = llm.Reading(mapping={entry.blank.id: "full_name"}, backend="t")
    changed, _ = llm.apply(reading, doc, result, profile)
    assert changed == [] and entry.value == "Zelda Quillfeather"
