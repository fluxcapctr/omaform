"""The vault: does it keep the secret, and does it refuse tampering.

Calibration is skipped in most tests by writing the KDF parameters directly:
deriving a real key takes about a second by design, and a suite that pays that
forty times is a suite nobody runs.
"""

import base64
import json
import os
import stat

import pytest

from omaform import vault as vaulting
from omaform.vault import BadPassphrase, Kdf, Vault, VaultError, VaultLocked

PASSPHRASE = "correct horse battery staple"
# Real Argon2id shape, minimum cost. A suite that pays a second per derivation
# is a suite nobody runs, and the code path is identical either way.
CHEAP = dict(time_cost=1, memory_cost=32, parallelism=1)


def cheap_kdf() -> Kdf:
    return Kdf(salt=os.urandom(16), **CHEAP)


@pytest.fixture
def vault(tmp_path) -> Vault:
    v = Vault(tmp_path / "vault.enc")
    v.create(PASSPHRASE, {"me": {"ssn": "123-45-6789"}}, kdf=cheap_kdf())
    return v


def envelope(v: Vault) -> dict:
    return json.loads(v.path.read_text())


def rewrite(v: Vault, data: dict) -> None:
    v.path.write_text(json.dumps(data))


# --- the basics -------------------------------------------------------------

def test_round_trip(vault):
    vault.lock()
    vault.unlock(PASSPHRASE)
    assert vault.get("me", "ssn") == "123-45-6789"


def test_file_is_not_world_readable(vault):
    assert stat.S_IMODE(vault.path.stat().st_mode) == 0o600


def test_the_secret_is_not_in_the_file(vault):
    raw = vault.path.read_bytes()
    assert b"123-45-6789" not in raw
    assert b"6789" not in raw


def test_locked_values_are_refused(vault):
    vault.lock()
    with pytest.raises(VaultLocked):
        vault.get("me", "ssn")
    with pytest.raises(VaultLocked):
        vault.set("me", "ein", "12-3456789")


def test_wrong_passphrase_is_refused(vault):
    vault.lock()
    with pytest.raises(BadPassphrase):
        vault.unlock("not the passphrase")


def test_empty_passphrase_is_refused(tmp_path):
    with pytest.raises(VaultError):
        Vault(tmp_path / "v.enc").create("", kdf=cheap_kdf())


def test_create_refuses_to_clobber(vault):
    with pytest.raises(VaultError, match="already exists"):
        vault.create(PASSPHRASE)


def test_set_and_unset_persist(vault):
    vault.set("me", "ein", "12-3456789")
    vault.lock()
    vault.unlock(PASSPHRASE)
    assert vault.get("me", "ein") == "12-3456789"
    assert vault.unset("me", "ein") is True
    assert vault.unset("me", "ein") is False
    vault.lock()
    vault.unlock(PASSPHRASE)
    assert vault.get("me", "ein") is None


def test_key_names_need_no_passphrase(vault):
    """So that `omaform inspect` can say what a form wants without prompting."""
    vault.set("me", "ein", "12-3456789")
    vault.lock()
    assert Vault(vault.path).key_names() == ["me/ein", "me/ssn"]


def test_changing_the_passphrase_keeps_the_contents(vault, monkeypatch):
    monkeypatch.setattr(vaulting, "calibrate", lambda target=None: 1)
    vault.change_passphrase("a whole new passphrase")
    vault.lock()
    with pytest.raises(BadPassphrase):
        vault.unlock(PASSPHRASE)
    vault.unlock("a whole new passphrase")
    assert vault.get("me", "ssn") == "123-45-6789"


def test_each_save_uses_a_fresh_nonce(vault):
    first = envelope(vault)["body"]["nonce"]
    vault.set("me", "ein", "12-3456789")
    assert envelope(vault)["body"]["nonce"] != first


# --- tampering --------------------------------------------------------------
# The cleartext header is convenient, so every part of it that could change
# behaviour has to be authenticated.

def test_editing_the_key_list_is_detected(vault):
    """Otherwise an attacker could delete "ssn" from the list and Omaform would
    quietly believe the vault holds no Social Security number."""
    vault.lock()
    data = envelope(vault)
    data["keys"] = []
    rewrite(vault, data)
    with pytest.raises(VaultError):
        Vault(vault.path).unlock(PASSPHRASE)


def test_weakening_the_kdf_parameters_is_detected(vault):
    vault.lock()
    data = envelope(vault)
    # Deliberately different from the fast fixture's own settings, or the test
    # tampers with nothing and passes for the wrong reason.
    data["kdf"]["time_cost"] = 3
    data["kdf"]["memory_cost"] = 64
    rewrite(vault, data)
    with pytest.raises(VaultError):
        Vault(vault.path).unlock(PASSPHRASE)


def test_flipping_a_ciphertext_bit_is_detected(vault):
    vault.lock()
    data = envelope(vault)
    raw = bytearray(base64.b64decode(data["body"]["ciphertext"]))
    raw[3] ^= 0x01
    data["body"]["ciphertext"] = base64.b64encode(bytes(raw)).decode()
    rewrite(vault, data)
    with pytest.raises(VaultError):
        Vault(vault.path).unlock(PASSPHRASE)


def test_swapping_in_another_vaults_wrapped_key_is_detected(tmp_path, vault):
    other = Vault(tmp_path / "other.enc")
    other.create("a different passphrase", {"me": {"ssn": "999-99-9999"}},
                 kdf=cheap_kdf())
    vault.lock()
    data = envelope(vault)
    data["wrap"] = envelope(other)["wrap"]
    rewrite(vault, data)
    with pytest.raises(VaultError):
        Vault(vault.path).unlock(PASSPHRASE)


def test_a_foreign_file_is_rejected(tmp_path):
    path = tmp_path / "not-a-vault.enc"
    path.write_text('{"format": "something else", "version": 1}')
    with pytest.raises(VaultError, match="not a Omaform vault"):
        Vault(path).unlock(PASSPHRASE)


def test_a_newer_format_is_refused_rather_than_guessed_at(vault):
    vault.lock()
    data = envelope(vault)
    data["version"] = 99
    rewrite(vault, data)
    with pytest.raises(VaultError, match="newer Omaform"):
        Vault(vault.path).unlock(PASSPHRASE)


# --- calibration ------------------------------------------------------------

def test_calibration_returns_something_usable(monkeypatch):
    monkeypatch.setattr(vaulting, "MEMORY_KIB", 8)
    monkeypatch.setattr(vaulting, "PARALLELISM", 1)
    cost = vaulting.calibrate(target=0.01)
    assert vaulting.MIN_TIME_COST <= cost <= vaulting.MAX_TIME_COST


def test_kdf_rejects_an_unknown_algorithm():
    with pytest.raises(VaultError, match="unsupported"):
        Kdf(salt=b"0" * 16, algorithm="rot13").derive("x")


# --- how the rest of the program uses it -----------------------------------

def test_the_vault_is_not_opened_unless_a_value_is_needed(vault, w9):
    """A form that wants no sensitive value must never cause a prompt."""
    from omaform import plan as planning
    from omaform.adapters import for_path
    from omaform.profile import Profile

    vault.lock()
    calls = []

    def unlocker():
        calls.append(1)
        raise AssertionError("should not have been asked to unlock")

    profile = Profile({"city": "Springfield", "state": "OR", "zip": "97403"},
                      vault_keys=frozenset(), unlocker=unlocker)
    planning.build(for_path(w9).discover(w9), profile)
    assert not calls


def test_a_form_that_needs_the_ssn_triggers_exactly_one_unlock(vault, w9):
    from omaform import plan as planning
    from omaform.adapters import for_path
    from omaform.profile import Profile

    vault.lock()
    calls = []

    def unlocker():
        calls.append(1)
        vault.unlock(PASSPHRASE)
        return vault.values_for("me")

    profile = Profile({"full_name": "Eric Stevens"},
                      vault_keys=frozenset({"ssn"}), unlocker=unlocker)
    result = planning.build(for_path(w9).discover(w9), profile, prefer={"ssn"})
    assert len(calls) == 1, "the vault should be opened once, not once per blank"
    assert [e.value for e in result.filled if e.match and e.match.key == "ssn"] == \
        ["123", "45", "6789"]


def test_peek_never_unlocks(vault):
    from omaform.profile import Profile

    def unlocker():
        raise AssertionError("peek must not unlock")

    profile = Profile({}, vault_keys=frozenset({"ssn"}), unlocker=unlocker)
    assert profile.peek("ssn") is None
    assert profile.has("ssn") is True


def test_the_plain_store_still_refuses_sensitive_keys(library):
    """Two tiers stay two tiers: the vault's existence is not a reason to let
    an SSN into a plaintext profile."""
    library.create("Me")
    with pytest.raises(PermissionError, match="omaform vault set"):
        library.set_value("me", "ssn", "123-45-6789")


def test_unusable_kdf_parameters_report_as_a_damaged_vault(vault):
    """A file can hold nonsense, so nonsense must not reach the caller as a
    library traceback."""
    vault.lock()
    data = envelope(vault)
    data["kdf"]["memory_cost"] = 1
    data["kdf"]["parallelism"] = 64
    rewrite(vault, data)
    with pytest.raises(VaultError):
        Vault(vault.path).unlock(PASSPHRASE)


# --- identities -------------------------------------------------------------

def test_identities_keep_separate_values(tmp_path):
    """Your Social Security number and your spouse's are different secrets that
    happen to answer the same question."""
    v = Vault(tmp_path / "v.enc")
    v.create(PASSPHRASE, {"eric": {"ssn": "111-11-1111"}}, kdf=cheap_kdf())
    v.set("sarah", "ssn", "222-22-2222")
    v.lock()
    v.unlock(PASSPHRASE)
    assert v.get("eric", "ssn") == "111-11-1111"
    assert v.get("sarah", "ssn") == "222-22-2222"
    assert v.identities() == ["eric", "sarah"]
    assert v.keys_for("sarah") == ["ssn"]


def test_deleting_an_identity_forgets_its_secrets(vault):
    vault.set("sarah", "ssn", "222-22-2222")
    assert vault.forget_identity("sarah") is True
    vault.lock()
    assert Vault(vault.path).key_names() == ["me/ssn"]


def test_an_emptied_identity_leaves_no_trace(vault):
    vault.set("sarah", "ssn", "222-22-2222")
    vault.unset("sarah", "ssn")
    vault.lock()
    assert Vault(vault.path).key_names() == ["me/ssn"]


def write_version_1_vault(path, passphrase, values) -> None:
    """A vault in the pre-identities layout, built the way version 1 built it."""
    import secrets

    from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

    kdf = cheap_kdf()
    kek = bytes(kdf.derive(passphrase))
    dek = secrets.token_bytes(32)
    keys = sorted(values)
    aad = Vault._aad(kdf, keys, version=1)
    wrap_nonce, body_nonce = secrets.token_bytes(12), secrets.token_bytes(12)
    path.write_text(json.dumps({
        "format": "omaform-vault", "version": 1, "kdf": kdf.to_json(), "keys": keys,
        "wrap": {"nonce": base64.b64encode(wrap_nonce).decode(),
                 "ciphertext": base64.b64encode(
                     ChaCha20Poly1305(kek).encrypt(wrap_nonce, dek, aad)).decode()},
        "body": {"nonce": base64.b64encode(body_nonce).decode(),
                 "ciphertext": base64.b64encode(ChaCha20Poly1305(dek).encrypt(
                     body_nonce, json.dumps(values, sort_keys=True).encode(), aad)).decode()},
    }))


def test_a_version_1_vault_migrates_without_losing_anything(tmp_path):
    """Somebody's only copy of their tax identification number may be in one."""
    path = tmp_path / "old.enc"
    write_version_1_vault(path, PASSPHRASE, {"ssn": "123-45-6789", "ein": "12-3456789"})

    v = Vault(path)
    v.unlock(PASSPHRASE)
    assert v.get("me", "ssn") == "123-45-6789"
    assert v.get("me", "ein") == "12-3456789"

    # and it is rewritten in the new shape rather than left to surprise us later
    after = json.loads(path.read_text())
    assert after["version"] == 2
    assert after["keys"] == ["me/ein", "me/ssn"]

    again = Vault(path)
    again.unlock(PASSPHRASE)
    assert again.get("me", "ssn") == "123-45-6789"
