"""Encrypted storage for the sensitive tier.

Holds the values that would hurt to leak: Social Security and Employer
Identification numbers, bank account and routing numbers, date of birth, driving
licence, and eventually the signature image. Everything else lives in plain JSON
next door, because an address is annoying to retype and not damaging to lose.

## Why this exists even with full-disk encryption

A powered-off LUKS volume already yields nothing, so FDE does most of the work.
This tier covers the four things it cannot:

1. Anything running in your session reads ``~/.local/share`` freely. A malicious
   dependency, a browser extension, a curl-pipe-bash installer: all of it runs as
   you, and FDE is unlocked and irrelevant by then.
2. Backups leave the machine. A plaintext SSN in home is an SSN on whatever
   external drive the backups land on. Ciphertext stays ciphertext there.
3. Accidents: screen sharing, a stray ``git add``, a pasted config, a support log.
4. Other people. This ships publicly, and plenty of users have no FDE at all.

## Construction

A random 32-byte data key encrypts the contents. That data key is itself
encrypted ("wrapped") under a key derived from your passphrase with Argon2id.
Two layers rather than one so that changing your passphrase re-wraps 32 bytes
instead of re-encrypting everything, and so that each save can use a fresh
nonce under a stable key.

Both the wrapped key and the body are ChaCha20-Poly1305, and both are
authenticated over the same associated data: the format version, the KDF
parameters, and the list of key *names*. Authenticating the name list matters.
It is stored in the clear so that ``omaform inspect`` can tell you a form needs
your SSN without asking for a passphrase, and binding it into the AEAD means
nobody can quietly delete ``ssn`` from that list to make Omaform believe the
vault does not hold one.

Argon2id parameters are calibrated on first use rather than hardcoded: this
machine computes 64 MiB / t=3 in 17ms, which is about 60 guesses a second, and a
fixed default that felt slow on the author's laptop is not a defence anywhere
else.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import json
import os
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

FORMAT = "omaform-vault"
# 2 nests values under an identity slug. 1 was a flat key/value map and is
# migrated on unlock, since a vault written by the single-profile version is
# still somebody's only copy of their tax identification number.
VERSION = 2
LEGACY_SLUG = "me"

KEY_BYTES = 32
NONCE_BYTES = 12
SALT_BYTES = 16

# Calibration target for one passphrase derivation. Half a second is the usual
# interactive compromise: unnoticeable once per session, and expensive enough
# per guess to make an offline attack on a decent passphrase unattractive.
TARGET_SECONDS = 0.5
# Memory is the parameter that actually hurts a GPU or ASIC attacker. 256 MiB is
# chosen to stay allocatable on a small machine while being far past the point
# where massively parallel guessing stays cheap.
MEMORY_KIB = 256 * 1024
PARALLELISM = 4
MIN_TIME_COST, MAX_TIME_COST = 3, 64
# What a vault file may ask for when it is opened: generous against what this
# program writes, so a faster future machine's vault still opens, but bounded.
MAX_ACCEPTED_TIME_COST = 4 * MAX_TIME_COST
MAX_MEMORY_KIB = 2 * 1024 * 1024
MAX_PARALLELISM = 16


class VaultError(Exception):
    """The vault could not be read, written or decrypted."""


class VaultLocked(VaultError):
    """A value was requested before the vault was unlocked."""


class BadPassphrase(VaultError):
    """The passphrase did not decrypt the vault."""


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _wipe(buf: bytearray | None) -> None:
    """Overwrite key material in place.

    Best effort, and worth being honest about its limits: CPython copies bytes
    objects around freely and the interpreter may have left fragments elsewhere.
    This reliably clears the buffer we control, which is better than leaving a
    key sitting in a long-lived process.
    """
    if buf:
        for i in range(len(buf)):
            buf[i] = 0


def lock_memory() -> bool:
    """Ask the kernel to keep this process out of swap. Best effort.

    Usually fails without a raised RLIMIT_MEMLOCK, which is why it returns a
    boolean instead of pretending. On this machine the swapfile lives inside the
    encrypted root volume, so a failure here is not the exposure it would be on a
    system with plaintext swap.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        MCL_CURRENT, MCL_FUTURE = 1, 2
        return libc.mlockall(MCL_CURRENT | MCL_FUTURE) == 0
    except Exception:
        return False


@dataclass(frozen=True)
class Kdf:
    salt: bytes
    time_cost: int = MIN_TIME_COST
    memory_cost: int = MEMORY_KIB
    parallelism: int = PARALLELISM
    algorithm: str = "argon2id"

    def derive(self, passphrase: str) -> bytearray:
        if self.algorithm != "argon2id":
            raise VaultError(f"unsupported key derivation: {self.algorithm}")
        try:
            raw = hash_secret_raw(
                secret=passphrase.encode("utf-8"),
                salt=self.salt,
                time_cost=self.time_cost,
                memory_cost=self.memory_cost,
                parallelism=self.parallelism,
                hash_len=KEY_BYTES,
                type=Type.ID,
            )
        except Exception as exc:
            # The parameters come out of a file, so they can be nonsense or
            # hostile: Argon2 rejects memory_cost below 8 per lane, for
            # instance. A corrupt vault should report itself as corrupt, not
            # surface a library traceback.
            raise VaultError(f"these key derivation parameters are unusable: "
                             f"{exc}") from exc
        return bytearray(raw)

    def to_json(self) -> dict:
        return {"algorithm": self.algorithm, "time_cost": self.time_cost,
                "memory_cost": self.memory_cost, "parallelism": self.parallelism,
                "salt": _b64(self.salt)}

    @classmethod
    def from_json(cls, data: dict) -> "Kdf":
        try:
            kdf = cls(salt=_unb64(data["salt"]),
                      time_cost=int(data["time_cost"]),
                      memory_cost=int(data["memory_cost"]),
                      parallelism=int(data["parallelism"]),
                      algorithm=str(data["algorithm"]))
        except (KeyError, ValueError, TypeError) as exc:
            raise VaultError(f"unreadable key derivation parameters: {exc}") from exc
        # These come from the file and are only authenticated after Argon2 has
        # run with them, so they are bounded first: a tampered file must not be
        # able to ask for an hour of work or eight gigabytes of memory.
        kdf.check()
        return kdf

    def check(self) -> None:
        """The bounds every vault must meet, when written as when read, so no
        vault this program writes can be one it then refuses to open."""
        kdf = self
        if kdf.algorithm != "argon2id":
            raise VaultError(f"unsupported key derivation: {kdf.algorithm}")
        if not 1 <= kdf.parallelism <= MAX_PARALLELISM:
            raise VaultError("key derivation parallelism is out of range")
        if not 8 * kdf.parallelism <= kdf.memory_cost <= MAX_MEMORY_KIB:
            raise VaultError("key derivation memory cost is out of range")
        if not 1 <= kdf.time_cost <= MAX_ACCEPTED_TIME_COST:
            raise VaultError("key derivation time cost is out of range")
        if not 16 <= len(kdf.salt) <= 64:
            raise VaultError("key derivation salt has the wrong length")


def _time_one(time_cost: int) -> float:
    probe = Kdf(salt=b"\x00" * SALT_BYTES, time_cost=time_cost)
    start = time.perf_counter()
    _wipe(probe.derive("calibration"))
    return max(time.perf_counter() - start, 1e-4)


def calibrate(target: float = TARGET_SECONDS) -> int:
    """Pick a time cost so one derivation takes roughly `target` seconds here.

    Two passes. Cost is not quite linear in time_cost at the low end, where the
    fixed cost of filling 256 MiB dominates, so a single probe at the minimum
    overshoots badly. Measuring again near the estimate corrects the slope.

    The result is an approximation and lands high rather than low. Calibration
    runs its probes back to back with the memory already faulted in, while a
    real unlock starts cold in a fresh process, so the achieved time can be
    close to double the target: on this machine calibration picks 17 and unlock
    takes about 0.9s. That error is in the safe direction for a security
    parameter, so it is left alone rather than tuned away.
    """
    def estimate(from_cost: int, elapsed: float) -> int:
        scaled = round(from_cost * target / elapsed)
        return max(MIN_TIME_COST, min(MAX_TIME_COST, scaled))

    first = estimate(MIN_TIME_COST, _time_one(MIN_TIME_COST))
    if first <= MIN_TIME_COST or first >= MAX_TIME_COST:
        return first
    return estimate(first, _time_one(first))


class Vault:
    """The encrypted store. Locked until `unlock` succeeds."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._kek: bytearray | None = None
        self._dek: bytearray | None = None
        # {identity slug: {key: value}}
        self._values: dict[str, dict[str, str]] = {}
        self._kdf: Kdf | None = None
        self._migrated = False

    # -- inspection that does not need the passphrase ---------------------

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def unlocked(self) -> bool:
        return self._dek is not None

    def _envelope(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError as exc:
            raise VaultError(f"no vault at {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise VaultError(f"{self.path} is not a readable vault: {exc}") from exc
        if data.get("format") != FORMAT:
            raise VaultError(f"{self.path} is not a Omaform vault")
        if int(data.get("version", 0)) > VERSION:
            raise VaultError(f"{self.path} was written by a newer Omaform")
        return data

    @staticmethod
    def _flatten(values: dict[str, dict[str, str]]) -> list[str]:
        return sorted(f"{slug}/{key}"
                      for slug, entries in values.items() for key in entries)

    def key_names(self) -> list[str]:
        """Every "identity/key" the vault holds, without unlocking it.

        Stored in the clear on purpose, so `omaform inspect` can say "this form
        wants your SSN and you have one" without a passphrase prompt. The list is
        covered by the AEAD's associated data, so it cannot be edited unnoticed.
        Identity slugs are already visible as filenames next door, so naming them
        here gives nothing away that was private.
        """
        if self.unlocked:
            return self._flatten(self._values)
        if not self.exists:
            return []
        envelope = self._envelope()
        names = [str(k) for k in envelope.get("keys", [])]
        if int(envelope.get("version", VERSION)) < 2:
            # A single-profile vault names its keys bare ("ssn"). They belong
            # to the identity migration gives them, and are reported that way
            # so a fill finds them, asks for the passphrase, and migrates.
            names = [n if "/" in n else f"{LEGACY_SLUG}/{n}" for n in names]
        return sorted(names)

    def keys_for(self, slug: str) -> list[str]:
        """Which sensitive keys one identity has stored."""
        prefix = f"{slug}/"
        return sorted(name[len(prefix):] for name in self.key_names()
                      if name.startswith(prefix))

    def identities(self) -> list[str]:
        return sorted({name.split("/", 1)[0] for name in self.key_names()
                       if "/" in name})

    # -- the crypto -------------------------------------------------------

    @staticmethod
    def _aad(kdf: Kdf, keys: list[str], version: int = VERSION) -> bytes:
        """Associated data: everything in the clear that must not change.

        The version is a parameter rather than the module constant, because an
        older vault was authenticated under the version it was written with.
        Hardcoding the current one makes every previous vault fail to decrypt,
        which reads as a wrong passphrase and looks exactly like data loss.
        """
        return json.dumps(
            {"format": FORMAT, "version": version,
             "kdf": kdf.to_json(), "keys": sorted(keys)},
            sort_keys=True, separators=(",", ":")).encode("utf-8")

    def create(self, passphrase: str,
               values: dict[str, dict[str, str]] | None = None,
               kdf: Kdf | None = None) -> None:
        """Write a brand new vault. Refuses to clobber an existing one.

        `kdf` overrides the calibrated defaults. Used by the test suite, which
        cannot afford a real derivation per test, and available for anyone who
        wants to dial the cost up beyond what calibration chooses.
        """
        if self.exists:
            raise VaultError(f"a vault already exists at {self.path}")
        if not passphrase:
            raise VaultError("an empty passphrase protects nothing")
        self._kdf = kdf or Kdf(salt=secrets.token_bytes(SALT_BYTES),
                               time_cost=calibrate())
        self._kdf.check()
        self._kek = self._kdf.derive(passphrase)
        self._dek = bytearray(secrets.token_bytes(KEY_BYTES))
        self._values = {s: dict(v) for s, v in (values or {}).items()}
        self.save()

    def unlock(self, passphrase: str) -> None:
        envelope = self._envelope()
        kdf = Kdf.from_json(envelope.get("kdf", {}))
        keys = sorted(str(k) for k in envelope.get("keys", []))
        version = int(envelope.get("version", VERSION))
        aad = self._aad(kdf, keys, version)

        kek = kdf.derive(passphrase)
        try:
            wrap = envelope["wrap"]
            dek = bytearray(ChaCha20Poly1305(bytes(kek)).decrypt(
                _unb64(wrap["nonce"]), _unb64(wrap["ciphertext"]), aad))
        except InvalidTag as exc:
            _wipe(kek)
            raise BadPassphrase("wrong passphrase, or the vault has been altered") from exc
        except (KeyError, ValueError) as exc:
            _wipe(kek)
            raise VaultError(f"damaged vault: {exc}") from exc

        try:
            body = envelope["body"]
            plain = ChaCha20Poly1305(bytes(dek)).decrypt(
                _unb64(body["nonce"]), _unb64(body["ciphertext"]), aad)
            values = json.loads(plain.decode("utf-8"))
        except (InvalidTag, KeyError, ValueError) as exc:
            _wipe(kek)
            _wipe(dek)
            raise VaultError(f"damaged vault: {exc}") from exc

        if version < 2:
            # Version 1 held one flat map, from before identities existed.
            values = {LEGACY_SLUG: {str(k): str(v) for k, v in values.items()}}
            self._migrated = True
        else:
            values = {str(s): {str(k): str(v) for k, v in entries.items()}
                      for s, entries in values.items()}

        if self._flatten(values) != keys and not self._migrated:
            _wipe(kek)
            _wipe(dek)
            raise VaultError("vault contents do not match its key list")

        self._kdf, self._kek, self._dek = kdf, kek, dek
        self._values = values
        if self._migrated:
            # Rewrite in the new shape straight away, so the old layout does not
            # linger and surprise a later version.
            self.save()
            self._migrated = False

    def lock(self) -> None:
        """Forget everything. Safe to call more than once."""
        _wipe(self._kek)
        _wipe(self._dek)
        self._kek = self._dek = None
        self._values = {}

    def save(self) -> None:
        if not self.unlocked or self._kdf is None or self._kek is None:
            raise VaultLocked("unlock the vault before saving it")

        self._values = {s: v for s, v in self._values.items() if v}
        keys = self._flatten(self._values)
        aad = self._aad(self._kdf, keys)
        body_nonce = secrets.token_bytes(NONCE_BYTES)
        wrap_nonce = secrets.token_bytes(NONCE_BYTES)
        plain = json.dumps(self._values, sort_keys=True).encode("utf-8")

        envelope = {
            "format": FORMAT,
            "version": VERSION,
            "kdf": self._kdf.to_json(),
            "keys": keys,
            "wrap": {"nonce": _b64(wrap_nonce),
                     "ciphertext": _b64(ChaCha20Poly1305(bytes(self._kek)).encrypt(
                         wrap_nonce, bytes(self._dek), aad))},
            "body": {"nonce": _b64(body_nonce),
                     "ciphertext": _b64(ChaCha20Poly1305(bytes(self._dek)).encrypt(
                         body_nonce, plain, aad))},
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(envelope, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def change_passphrase(self, new_passphrase: str) -> None:
        """Re-wrap the data key. The body is not re-encrypted."""
        if not self.unlocked:
            raise VaultLocked("unlock the vault before changing its passphrase")
        if not new_passphrase:
            raise VaultError("an empty passphrase protects nothing")
        old_kek = self._kek
        self._kdf = Kdf(salt=secrets.token_bytes(SALT_BYTES), time_cost=calibrate())
        self._kek = self._kdf.derive(new_passphrase)
        _wipe(old_kek)
        self.save()

    # -- values -----------------------------------------------------------

    def get(self, slug: str, key: str) -> str | None:
        if not self.unlocked:
            raise VaultLocked(f"{key} is in the vault, which is locked")
        return self._values.get(slug, {}).get(key)

    def set(self, slug: str, key: str, value: str) -> None:
        if not self.unlocked:
            raise VaultLocked("unlock the vault before writing to it")
        entries = self._values.setdefault(slug, {})
        if value == "":
            entries.pop(key, None)
        else:
            entries[key] = value
        self.save()

    def unset(self, slug: str, key: str) -> bool:
        if not self.unlocked:
            raise VaultLocked("unlock the vault before writing to it")
        existed = self._values.get(slug, {}).pop(key, None) is not None
        if existed:
            self.save()
        return existed

    def forget_identity(self, slug: str) -> bool:
        """Drop everything an identity had stored, when it is deleted."""
        if not self.unlocked:
            raise VaultLocked("unlock the vault before writing to it")
        existed = self._values.pop(slug, None) is not None
        if existed:
            self.save()
        return existed

    def values_for(self, slug: str) -> dict[str, str]:
        if not self.unlocked:
            raise VaultLocked("the vault is locked")
        return dict(self._values.get(slug, {}))

    def values(self) -> dict[str, dict[str, str]]:
        if not self.unlocked:
            raise VaultLocked("the vault is locked")
        return {s: dict(v) for s, v in self._values.items()}


# -- optional keyring backing -------------------------------------------------
# libsecret through its own command line tool rather than a Python binding: it
# is already installed, it is what gnome-keyring speaks, and it keeps a C
# dependency out of the package.

_KEYRING_ATTRS = ("service", "omaform", "key", "vault-passphrase")


def keyring_available() -> bool:
    from shutil import which
    return which("secret-tool") is not None


def keyring_lookup() -> str | None:
    if not keyring_available():
        return None
    try:
        done = subprocess.run(["secret-tool", "lookup", *_KEYRING_ATTRS],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout or None if done.returncode == 0 else None


def keyring_store(passphrase: str) -> bool:
    """Hand the passphrase to the login keyring so unlocking needs no prompt.

    Weaker, in exactly the way a browser's saved passwords are weaker: while
    your session is unlocked, anything running as you can ask the keyring for
    this. Offered as a choice, with that said plainly, rather than as a default.
    """
    if not keyring_available():
        return False
    done = subprocess.run(
        ["secret-tool", "store", "--label=Omaform vault passphrase", *_KEYRING_ATTRS],
        input=passphrase, capture_output=True, text=True, timeout=30)
    return done.returncode == 0


def keyring_clear() -> bool:
    if not keyring_available():
        return False
    done = subprocess.run(["secret-tool", "clear", *_KEYRING_ATTRS],
                          capture_output=True, text=True, timeout=15)
    return done.returncode == 0
