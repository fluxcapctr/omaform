"""The facts about you, and where they live.

Two tiers, because a street address and a Social Security number do not deserve
the same treatment:

* **profile** is plain JSON at ``~/.local/share/omaform/profile.json``, mode 0600.
  Annoying to retype, not damaging to leak. Plain text on purpose: greppable,
  diffable, and editable in any editor when this program has a bug.
* **vault** holds the sensitive tier and is encrypted. Phase 4 implements it. Until
  then sensitive values are accepted only for a single run, via ``--once``, and are
  never written to disk. That ordering is deliberate: a plaintext SSN sitting in
  the home directory would also sit in every six-hourly backup.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Field:
    """One profile key: what it is called, what it answers to, what it must not."""

    key: str
    title: str
    aliases: tuple[str, ...] = ()
    avoid: tuple[str, ...] = ()
    sensitive: bool = False
    # Canonical digit grouping for split-box fields, as on the W-9's TIN boxes.
    split: tuple[int, ...] | None = None
    # An image rather than text: never typed into a box, placed on the page.
    image: bool = False
    # One of a fixed set of answers, each naming the checkbox it ticks.
    choice: bool = False
    # Vetoes this key opts out of. See _f and NOT_YOURS.
    # Built from other keys when not set directly. `template` gives the exact
    # punctuation, which matters: "Springfield, OR 97403" is right and
    # "Springfield, OR, 97403" looks like a data-entry error on a tax form.
    composite: tuple[str, ...] | None = None
    template: str | None = None
    joiner: str = " "


# Phrases that mean "this blank belongs to somebody else". A form's employer,
# preparer, translator, requester, witness or notary sections ask the same
# questions about a different person, and answering them with your own details
# is not a cosmetic error: on an I-9 the employer's section is a legal
# attestation by the employer. Merged into every personal key's veto list.
NOT_YOURS: tuple[str, ...] = (
    "employer", "employers", "employer s", "preparer", "translator", "requester",
    "witness", "notary", "spouse", "officer", "co applicant", "co signer",
    "landlord", "guarantor", "beneficiary", "physician", "provider",
    # Not parties, but the names of blocks an employer completes: the I-9's
    # Supplement B is "Reverification and Rehire", and every box in it asks
    # for the employee's details in the employer's hand.
    "rehire", "reverification", "re verification", "office use",
)


# Federal tax classification, the W-9's line 3a. Each answer carries the
# words printed beside its checkbox, and the three LLC answers also carry the
# letter that goes in the small code box beside the LLC checkbox.
TAX_CLASSIFICATIONS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "individual": ("Individual / sole proprietor",
                   ("individual sole proprietor", "individual", "sole proprietor"), ""),
    "c_corp": ("C corporation", ("c corporation", "c corp"), ""),
    "s_corp": ("S corporation", ("s corporation", "s corp"), ""),
    "partnership": ("Partnership", ("partnership",), ""),
    "trust_estate": ("Trust / estate", ("trust estate", "trust", "estate"), ""),
    "llc_c": ("LLC, taxed as a C corporation", ("llc", "limited liability company"), "C"),
    "llc_s": ("LLC, taxed as an S corporation", ("llc", "limited liability company"), "S"),
    "llc_p": ("LLC, taxed as a partnership", ("llc", "limited liability company"), "P"),
}


# The W-4's Step 1(c). The first box reads "Single or Married filing
# separately", so two answers stand behind one box; each answer carries the
# words that open its box, and a box is ticked if it stands for the answer.
FILING_STATUSES: dict[str, tuple[str, tuple[str, ...]]] = {
    "single": ("Single", ("single",)),
    "married_separately": ("Married filing separately",
                           ("single or married filing separately", "married filing separately")),
    "married_jointly": ("Married filing jointly", ("married filing jointly",)),
    "qualifying_surviving_spouse": (
        "Qualifying surviving spouse",
        ("married filing jointly or qualifying surviving spouse",
         "qualifying surviving spouse", "qualifying widow")),
    "head_of_household": ("Head of household", ("head of household",)),
}

# The I-9's Section 1 attestation. Kept in the vault: a legal status is not
# an address.
CITIZENSHIP_STATUSES: dict[str, tuple[str, tuple[str, ...]]] = {
    "citizen": ("U.S. citizen", ("a citizen of the united states",
                                 "citizen of the united states", "u s citizen")),
    "noncitizen_national": ("Noncitizen national of the U.S.",
                            ("a noncitizen national", "noncitizen national")),
    "permanent_resident": ("Lawful permanent resident",
                           ("a lawful permanent resident", "lawful permanent resident",
                            "permanent resident")),
    "authorized_alien": ("Noncitizen authorized to work",
                         ("an alien authorized to work", "a noncitizen authorized to work",
                          "alien authorized to work", "noncitizen other than")),
}

# Every choice key, with its answers and the words that open each answer's box.
CHOICES: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "tax_classification": {k: (v[0], v[1]) for k, v in TAX_CLASSIFICATIONS.items()},
    "filing_status": FILING_STATUSES,
    "citizenship": CITIZENSHIP_STATUSES,
}


def _f(key, title, aliases=(), avoid=(), allow=(), **k) -> Field:
    """Every personal key inherits the NOT_YOURS vetoes, minus its exemptions."""
    inherited = tuple(p for p in NOT_YOURS if p not in allow)
    return Field(key, title, aliases=aliases, avoid=tuple(avoid) + inherited, **k)


# Ordered roughly by how often a form asks for it.
SCHEMA: tuple[Field, ...] = (
    _f("full_name", "Full name",
       aliases=("name", "full name", "your name", "print name", "printed name",
                "name of entity individual", "name of entity/individual",
                "name as shown on your income tax return", "legal name",
                "applicant name", "employee name", "individual name",
                "name of individual", "print or type name", "name of taxpayer"),
       avoid=("business name", "requester", "entity name", "employer name",
              "spouse", "preparer", "trade name", "dba", "witness", "officer")),
    _f("first_name", "First name",
       aliases=("first name", "given name", "first"),
       # "Other first names used" is a former-name box, not your first name.
       avoid=("last name only", "other", "former", "maiden", "previous")),
    _f("middle_initial", "Middle initial", aliases=("middle initial", "mi", "middle")),
    _f("last_name", "Last name",
       aliases=("last name", "surname", "family name", "last"),
       avoid=("other", "former", "maiden", "previous")),
    _f("first_middle", "First name and middle initial",
       aliases=("first name and middle initial",),
       composite=("first_name", "middle_initial")),

    _f("business_name", "Business name",
       aliases=("business name", "business name disregarded entity name",
                "disregarded entity name", "trade name", "dba", "doing business as",
                "company", "company name", "firm name"),
       avoid=("requester",)),

    _f("address1", "Street address",
       aliases=("address", "street address", "mailing address", "home address",
                "address number street and apt or suite no", "street",
                "address street number and name",
                "number street and apt or suite no", "address line 1",
                "present address"),
       avoid=("requester", "email", "e mail", "employer address", "city")),
    _f("address2", "Apartment or suite", aliases=("apt", "apartment", "suite", "unit",
                                                  "apt or suite no", "address line 2")),
    _f("city", "City", aliases=("city", "town", "city or town"), avoid=("state", "zip")),
    _f("state", "State", aliases=("state", "province"), avoid=("city", "zip", "statement")),
    _f("zip", "ZIP code", aliases=("zip", "zip code", "postal code", "postcode")),
    _f("city_state_zip", "City, state and ZIP",
       aliases=("city state and zip code", "city town state and zip code",
                "city state zip", "city or town state and zip code"),
       composite=("city", "state", "zip"), template="{city}, {state} {zip}"),
    _f("country", "Country", aliases=("country",)),

    _f("phone", "Phone", aliases=("phone", "telephone", "phone number",
                                  "telephone number", "daytime phone", "mobile",
                                  "cell", "contact number")),
    _f("email", "Email", aliases=("email", "e mail", "email address", "e mail address")),
    _f("website", "Website", aliases=("website", "web site", "url", "homepage")),
    _f("job_title", "Job title", aliases=("title", "job title", "position",
                                          "occupation", "role")),

    _f("ssn", "Social Security number", sensitive=True, split=(3, 2, 4),
       aliases=("social security number", "ssn", "social security no",
                "social security", "ss number", "taxpayer identification number",
                "tin", "your social security number")),
    # An EIN is "employer identification number" *about you*: the whole phrase
    # is the name of your own tax id, so this key alone is exempt from the
    # employer veto that protects every other field.
    _f("ein", "Employer Identification number", sensitive=True, split=(2, 7),
       allow=("employer", "employer s"),
       aliases=("employer identification number", "ein",
                "employer identification no", "federal tax id", "fein",
                # A form labelled only "TIN" is answerable by either number.
                # Both keys claim it, and the identity's kind decides.
                "taxpayer identification number", "tin", "tax id number")),
    _f("dob", "Date of birth", sensitive=True,
       aliases=("date of birth", "dob", "birth date", "birthdate")),
    _f("drivers_license", "Driver's licence", sensitive=True,
       aliases=("driver s license", "drivers license", "license number",
                "driver s license number", "dl number", "state id number")),
    _f("bank_account", "Bank account number", sensitive=True,
       aliases=("account number", "bank account number", "checking account number",
                "deposit account number"),
       avoid=("list account number", "requester")),
    _f("bank_routing", "Bank routing number", sensitive=True,
       aliases=("routing number", "aba number", "aba routing number",
                "bank routing number")),

    _f("tax_classification", "Tax classification", choice=True,
       aliases=("federal tax classification", "tax classification")),
    _f("filing_status", "Filing status", choice=True,
       aliases=("filing status", "federal filing status")),
    _f("citizenship", "Citizenship status", choice=True, sensitive=True,
       aliases=("citizenship status", "immigration status", "citizenship")),
    # The letter beside the LLC box on a W-9, derived from the classification.
    _f("llc_tax_code", "LLC tax classification code",
       aliases=("enter the tax classification", "llc enter the tax classification",
                "tax classification c c corporation s s corporation p partnership")),

    _f("date_today", "Today's date",
       aliases=("date", "date signed", "today s date", "dated", "date of signature"),
       # An expiry or hire date is not today's date, and writing today into one
       # is a silent factual error on the form.
       avoid=("expiration", "expires", "exp", "hire", "rehire", "termination",
              "reverification", "birth", "issue", "issued", "effective",
              "employment", "start", "end",
              # A date box never says "number": a label that does is bleed.
              "number", "ein", "identification")),
    # Kept in the vault as a PNG. Sensitive because a signature image is the
    # thing forgery is made of; an image because it is never typed anywhere.
    _f("signature", "Signature", sensitive=True, image=True,
       aliases=("signature", "sign here", "signature of", "authorized signature",
                "applicant s signature", "employee s signature", "your signature"),
       # "Signature Date" and "date of signature" are dates that happen to
       # lead with the word; a drawn signature does not belong in them.
       avoid=("date", "dated", "printed name", "print name", "title")),
)

# Keys that answer the same question in different ways. A W-9 asks for a Social
# Security number *or* an Employer Identification number, with a literal "or"
# printed between the two rows, and filling both is wrong on a form you sign.
# When a document offers both and the profile can satisfy both, Omaform fills
# neither and says so, rather than picking for you.
ALTERNATIVES: tuple[frozenset[str], ...] = (
    frozenset({"ssn", "ein"}),
)


def alternatives_for(key: str) -> frozenset[str]:
    for group in ALTERNATIVES:
        if key in group:
            return group - {key}
    return frozenset()


BY_KEY: dict[str, Field] = {f.key: f for f in SCHEMA}
SENSITIVE_KEYS: frozenset[str] = frozenset(f.key for f in SCHEMA if f.sensitive)


def data_dir() -> Path:
    base = os.environ.get("OMAFORM_HOME") or os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    root = Path(base).expanduser()
    return root if os.environ.get("OMAFORM_HOME") else root / "omaform"


def slugify(label: str) -> str:
    """A filename-safe identity key. Stable, so renaming the label keeps the file."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return slug or "identity"


@dataclass
class Profile:
    """Resolved values: the plain tier, the vault, and anything passed for one run.

    Vault values are fetched lazily through `unlocker`, so a passphrase is asked
    for only when a form genuinely needs a sensitive value, and never merely
    because one is available. `has` and `peek` exist for the same reason: the
    matcher needs to know whether a key is obtainable, and roughly how long its
    value is, while scoring candidates it may well discard.
    """

    values: dict[str, str] = field(default_factory=dict)
    once: dict[str, str] = field(default_factory=dict)
    # Sensitive keys the vault holds, known from its cleartext key list.
    vault_keys: frozenset[str] = frozenset()
    # Called with no arguments the first time a vault value is actually needed.
    # Returns the decrypted values, or None if the unlock was declined.
    unlocker: object = None
    _vault_values: dict[str, str] | None = field(default=None, repr=False)

    def _from_vault(self, key: str) -> str | None:
        if key not in self.vault_keys:
            return None
        if self._vault_values is None:
            if not callable(self.unlocker):
                return None
            self._vault_values = self.unlocker() or {}
        return self._vault_values.get(key) or None

    def street_line(self) -> str | None:
        """The street with the apartment folded in, for forms with no apartment box.

        "2450 Chelsea Pl" on line 5 of a W-9 loses the apartment entirely, since
        the W-9 has nowhere else to put it. "Apt" is added when the stored unit
        is bare, and left alone when it already says Suite, Unit, or #.
        """
        street = self.get("address1")
        unit = self.get("address2")
        if not street:
            return None
        if not unit:
            return street
        lowered = unit.lower()
        if any(w in lowered for w in ("apt", "suite", "ste", "unit", "#", "floor", "fl ")):
            return f"{street}, {unit}"
        return f"{street}, Apt {unit}"

    def has(self, key: str) -> bool:
        """Could this key be resolved, without resolving it?

        True for a vault key while the vault is still locked: the caller is
        deciding whether the form needs it, which is not the same as needing it.
        """
        if key in self.vault_keys:
            return True
        return self.get(key) is not None

    def values_in_hand(self) -> list[str]:
        """Every value already readable without unlocking, for scrubbing."""
        out = []
        for source in (self.once, self.values, self._vault_values or {}):
            out += [str(v) for k, v in source.items()
                    if v and not (BY_KEY.get(k) and BY_KEY[k].image)]
        return out

    def peek(self, key: str) -> str | None:
        """The value, but only if it is already in hand. Never unlocks anything."""
        for source in (self.once, self.values, self._vault_values or {}):
            if source.get(key):
                return source[key]
        return None

    def get(self, key: str) -> str | None:
        """Resolve a key, building composites from their parts when needed."""
        for source in (self.once, self.values):
            if source.get(key):
                return source[key]

        from_vault = self._from_vault(key)
        if from_vault:
            return from_vault

        if key == "date_today":
            return _dt.date.today().strftime("%m/%d/%Y")
        if key == "llc_tax_code":
            chosen = self.get("tax_classification") or ""
            return TAX_CLASSIFICATIONS.get(chosen, ("", (), ""))[2] or None

        spec = BY_KEY.get(key)
        if spec and spec.composite:
            resolved = {k: (self.get(k) or "") for k in spec.composite}
            if not any(resolved.values()):
                return None
            if spec.template and all(resolved.values()):
                return spec.template.format(**resolved)
            # Partial data: fall back to joining whatever is there rather than
            # emitting a template with holes in it.
            return spec.joiner.join(v for v in resolved.values() if v)
        return None

    def available(self) -> list[str]:
        return [f.key for f in SCHEMA if self.has(f.key)]

    def is_sensitive(self, key: str) -> bool:
        return key in SENSITIVE_KEYS


# What an identity is, and which tax number that implies. A business files
# under an Employer Identification number; a person files under a Social
# Security number. Both answer the box a form labels "TIN", which is why the
# choice has to be recorded per identity rather than asked every time.
KINDS: dict[str, tuple[str, ...]] = {
    "person": ("ssn",),
    "business": ("ein",),
}


@dataclass
class Identity:
    """One person or business whose details can fill a form.

    Forms get filled on behalf of different parties: yourself, your company,
    your spouse. Adobe keeps a shelf of saved signatures for the same reason.
    The slug is the filename and never changes; the label is what you see and
    can be edited freely.
    """

    slug: str
    label: str
    values: dict[str, str] = field(default_factory=dict)
    # "person" or "business". Drives `prefers` and nothing else, so an unknown
    # value from a hand-edited file is harmless.
    kind: str = "person"
    # Which side of an either/or this identity takes, as with a W-9 asking for
    # a Social Security number *or* an Employer Identification number.
    prefers: tuple[str, ...] = ()

    def preference(self) -> set[str]:
        return set(self.prefers) or set(KINDS.get(self.kind, ()))

    def summary(self) -> str:
        """A line of context for a chooser, so two identities are tellable apart."""
        for key in ("business_name", "full_name", "email", "city"):
            if key != "label" and self.values.get(key):
                if self.values[key] != self.label:
                    return self.values[key]
        return ""


class Library:
    """Every identity on disk, in `<data>/profiles/<slug>.json`.

    Replaces the single profile.json of the first version, and migrates it on
    first use so nobody loses what they had typed in.
    """

    def __init__(self, directory: Path | str | None = None) -> None:
        self.dir = Path(directory) if directory is not None else data_dir()
        self.profiles_dir = self.dir / "profiles"
        self.config_path = self.dir / "config.json"
        self._migrate_legacy()

    # -- storage ---------------------------------------------------------

    def _migrate_legacy(self) -> None:
        legacy = self.dir / "profile.json"
        if not legacy.exists() or self.profiles_dir.exists():
            return
        try:
            values = json.loads(legacy.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        self._write(Identity("me", values.get("full_name") or "Me",
                             {k: str(v) for k, v in values.items() if v}))
        legacy.rename(self.dir / "profile.json.migrated")

    def _path(self, slug: str) -> Path:
        return self.profiles_dir / f"{slug}.json"

    def _write(self, identity: Identity) -> None:
        self.profiles_dir.mkdir(parents=True, exist_ok=True)
        payload = {"label": identity.label, "kind": identity.kind,
                   "prefers": list(identity.prefers), "values": identity.values}
        tmp = self._path(identity.slug).with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self._path(identity.slug))

    # -- reading ---------------------------------------------------------

    def slugs(self) -> list[str]:
        if not self.profiles_dir.exists():
            return []
        return sorted(p.stem for p in self.profiles_dir.glob("*.json"))

    def get(self, slug: str) -> Identity | None:
        path = self._path(slug)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            return None
        # Tolerate a hand-edited file that is just a flat map of values.
        if "values" not in data:
            return Identity(slug, data.get("label") or slug.title(),
                            {k: str(v) for k, v in data.items()
                             if k not in ("label", "kind", "prefers") and v})
        return Identity(
            slug,
            str(data.get("label") or slug.title()),
            {str(k): str(v) for k, v in (data.get("values") or {}).items() if v},
            kind=str(data.get("kind") or "person"),
            prefers=tuple(str(k) for k in (data.get("prefers") or [])))

    def all(self) -> list[Identity]:
        found = [self.get(s) for s in self.slugs()]
        return [i for i in found if i]

    # -- writing ---------------------------------------------------------

    def create(self, label: str, values: dict[str, str] | None = None,
               kind: str = "person") -> Identity:
        slug = slugify(label)
        taken = set(self.slugs())
        if slug in taken:
            n = 2
            while f"{slug}-{n}" in taken:
                n += 1
            slug = f"{slug}-{n}"
        identity = Identity(slug, label.strip() or slug.title(), dict(values or {}),
                            kind=kind if kind in KINDS else "person")
        # A person is an individual on a W-9 unless they say otherwise; a
        # business has to choose, since the answer is what the form is for.
        if identity.kind == "person":
            identity.values.setdefault("tax_classification", "individual")
        self._write(identity)
        if len(self.slugs()) == 1:
            self.set_default(identity.slug)
        return identity

    def save(self, identity: Identity) -> None:
        self._write(identity)

    def set_value(self, slug: str, key: str, value: str) -> Identity:
        if key not in BY_KEY:
            raise KeyError(f"unknown profile key: {key}")
        if key in SENSITIVE_KEYS:
            raise PermissionError(
                f"{key} is sensitive and does not belong in a plain profile. "
                f"Store it encrypted with `omaform vault set {key}`, or pass it "
                f"for a single run with --once {key}=VALUE"
            )
        if key in CHOICES and value and value not in CHOICES[key]:
            raise KeyError(f"{key} must be one of: {', '.join(CHOICES[key])}")
        identity = self.get(slug) or self.create(slug.title())
        if value == "":
            identity.values.pop(key, None)
        else:
            identity.values[key] = value
        self._write(identity)
        return identity

    def set_kind(self, slug: str, kind: str) -> Identity | None:
        """Change what an identity is, and with it which tax number it uses."""
        identity = self.get(slug)
        if identity is None or kind not in KINDS:
            return None
        identity.kind = kind
        # An explicit preference set earlier would otherwise silently outrank
        # the kind the user just chose, which is the opposite of what they meant.
        identity.prefers = ()
        self._write(identity)
        return identity

    def set_preference(self, slug: str, key: str) -> Identity | None:
        """Record which side of an either/or this identity takes."""
        identity = self.get(slug)
        if identity is None:
            return None
        group = alternatives_for(key) | {key}
        keep = tuple(k for k in identity.prefers if k not in group)
        identity.prefers = keep + (key,)
        self._write(identity)
        return identity

    def rename(self, slug: str, label: str) -> Identity | None:
        identity = self.get(slug)
        if identity is None:
            return None
        identity.label = label.strip() or identity.label
        self._write(identity)
        return identity

    def delete(self, slug: str) -> bool:
        path = self._path(slug)
        if not path.exists():
            return False
        path.unlink()
        if self.default_slug() == slug:
            remaining = self.slugs()
            self.set_default(remaining[0] if remaining else "")
        return True

    # -- which one is in force -------------------------------------------

    def default_slug(self) -> str:
        try:
            stored = json.loads(self.config_path.read_text()).get("default")
        except (OSError, json.JSONDecodeError, AttributeError):
            stored = None
        slugs = self.slugs()
        if stored in slugs:
            return stored
        return slugs[0] if slugs else ""

    # -- settings that are not about any one identity ----------------------

    def setting(self, key: str, default=None):
        try:
            config = json.loads(self.config_path.read_text())
        except (OSError, json.JSONDecodeError):
            return default
        return config.get(key, default) if isinstance(config, dict) else default

    def set_setting(self, key: str, value) -> None:
        self._update_config({key: value})

    def set_default(self, slug: str) -> None:
        self._update_config({"default": slug})

    def _update_config(self, changes: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        config = {}
        if self.config_path.exists():
            try:
                config = json.loads(self.config_path.read_text())
            except json.JSONDecodeError:
                config = {}
        if not isinstance(config, dict):
            config = {}
        config.update(changes)
        tmp = self.config_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(config, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.config_path)

    def resolve(self, slug: str | None) -> Identity | None:
        """The identity named, the default, or nothing at all."""
        if slug:
            found = self.get(slug)
            if found:
                return found
            for identity in self.all():
                if identity.label.lower() == slug.lower():
                    return identity
            return None
        return self.get(self.default_slug()) if self.default_slug() else None
