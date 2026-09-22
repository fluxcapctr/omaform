"""Storage rules, including the one that matters: no plaintext SSN on disk."""

import json
import stat

import pytest

from omaform.profile import SENSITIVE_KEYS, Profile


def test_composite_uses_the_template(sample_profile):
    assert sample_profile.get("city_state_zip") == "Springfield, OR 97403"


def test_composite_degrades_without_every_part():
    assert Profile({"city": "Springfield", "state": "OR"}).get("city_state_zip") == "Springfield OR"


def test_composite_is_none_when_nothing_is_set():
    assert Profile({}).get("city_state_zip") is None


def test_once_values_beat_stored_ones():
    profile = Profile({"city": "Springfield"}, {"city": "Dallas"})
    assert profile.get("city") == "Dallas"


def test_date_today_is_derived():
    assert Profile({}).get("date_today")


def test_profiles_are_written_0600(library):
    identity = library.create("Me", {"city": "Springfield"})
    mode = stat.S_IMODE((library.profiles_dir / f"{identity.slug}.json").stat().st_mode)
    assert mode == 0o600, f"profile is {oct(mode)}, must not be group or world readable"


def test_values_round_trip(library):
    library.create("Me")
    library.set_value("me", "full_name", "Eric Stevens")
    assert library.get("me").values["full_name"] == "Eric Stevens"


def test_unknown_keys_are_rejected(library):
    library.create("Me")
    with pytest.raises(KeyError):
        library.set_value("me", "favourite_colour", "blue")


@pytest.mark.parametrize("key", sorted(SENSITIVE_KEYS))
def test_plain_profiles_refuse_every_sensitive_key(key, library):
    """A plaintext SSN in the home directory is also an SSN in every backup,
    which is the whole reason the vault tier exists."""
    library.create("Me")
    with pytest.raises(PermissionError):
        library.set_value("me", key, "123-45-6789")
    assert key not in (library.get("me").values)


def test_clearing_a_key_removes_it(library):
    library.create("Me", {"city": "Springfield"})
    library.set_value("me", "city", "")
    assert "city" not in library.get("me").values


# --- several identities -----------------------------------------------------

def test_identities_are_independent(library):
    library.create("Eric Stevens", {"full_name": "Eric Stevens", "city": "Springfield"})
    library.create("Eric Stevens Design", {"business_name": "Eric Stevens Design"})
    assert library.slugs() == ["eric-stevens", "eric-stevens-design"]
    assert library.get("eric-stevens").values.get("business_name") is None


def test_a_clashing_label_gets_its_own_file(library):
    first = library.create("Me")
    second = library.create("Me")
    assert first.slug != second.slug


def test_the_first_identity_becomes_the_default(library):
    library.create("Eric Stevens")
    library.create("Sarah")
    assert library.default_slug() == "eric-stevens"


def test_the_default_can_be_changed_and_survives(library):
    library.create("Eric Stevens")
    library.create("Sarah")
    library.set_default("sarah")
    assert library.default_slug() == "sarah"


def test_deleting_the_default_picks_another(library):
    library.create("Eric Stevens")
    library.create("Sarah")
    library.delete("eric-stevens")
    assert library.default_slug() == "sarah"


def test_resolve_accepts_a_label_as_well_as_a_slug(library):
    library.create("Eric Stevens Design")
    assert library.resolve("Eric Stevens Design").slug == "eric-stevens-design"
    assert library.resolve("eric-stevens-design").slug == "eric-stevens-design"
    assert library.resolve("nobody") is None


def test_renaming_keeps_the_file_and_the_values(library):
    library.create("Sarah", {"city": "Springfield"})
    library.rename("sarah", "Sarah Stevens")
    assert library.get("sarah").label == "Sarah Stevens"
    assert library.get("sarah").values["city"] == "Springfield"


def test_a_single_profile_from_the_old_layout_is_migrated(tmp_path):
    """Nobody should lose what they had typed in because the format changed."""
    from omaform.profile import Library

    legacy = tmp_path / "profile.json"
    legacy.write_text(json.dumps({"full_name": "Eric Stevens", "city": "Springfield"}))

    library = Library(tmp_path)
    assert library.slugs() == ["me"]
    assert library.get("me").values["full_name"] == "Eric Stevens"
    assert library.get("me").label == "Eric Stevens"
    assert not legacy.exists(), "the old file should be moved aside, not left to confuse"


# --- what an identity is, and which tax number that implies -----------------

def test_a_person_files_under_a_social_security_number(library):
    identity = library.create("Eric Stevens", kind="person")
    assert identity.preference() == {"ssn"}


def test_a_business_files_under_an_ein(library):
    identity = library.create("Eric Stevens Design", kind="business")
    assert identity.preference() == {"ein"}


def test_the_kind_survives_a_reload(library):
    library.create("Eric Stevens Design", kind="business")
    assert library.get("eric-stevens-design").kind == "business"
    assert library.get("eric-stevens-design").preference() == {"ein"}


def test_an_unknown_kind_falls_back_to_person(library):
    identity = library.create("Odd", kind="martian")
    assert identity.kind == "person"


def test_an_explicit_preference_beats_the_kind(library):
    library.create("Eric Stevens", kind="person")
    library.set_preference("eric-stevens", "ein")
    assert library.get("eric-stevens").preference() == {"ein"}


def test_changing_the_kind_clears_a_stale_preference(library):
    """Otherwise the old choice silently outranks the one just made, which is
    the opposite of what picking a kind means."""
    library.create("Eric Stevens", kind="person")
    library.set_preference("eric-stevens", "ein")
    library.set_kind("eric-stevens", "business")
    assert library.get("eric-stevens").preference() == {"ein"}
    library.set_kind("eric-stevens", "person")
    assert library.get("eric-stevens").preference() == {"ssn"}


def test_setting_a_preference_replaces_the_other_side(library):
    library.create("Eric Stevens")
    library.set_preference("eric-stevens", "ein")
    library.set_preference("eric-stevens", "ssn")
    assert library.get("eric-stevens").prefers == ("ssn",)


def test_a_library_accepts_a_plain_string_path(tmp_path):
    from omaform.profile import Library

    assert Library(str(tmp_path)).slugs() == []
