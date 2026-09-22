"""Turn a document plus a profile into a concrete set of writes.

Kept separate from both the adapter and the matcher so that the review UI in
Phase 2 and the LLM layer in Phase 8 can inspect and amend a plan before
anything is written. A plan is data, not an action.

Built in two passes. The first decides *which* keys this document wants, using
nothing but labels; the second fetches values for the keys that survive. The
split is what keeps the vault shut: deciding that a W-9 offers an SSN row and an
EIN row needs no secret at all, and if `--prefer ein` has already ruled the SSN
out, no passphrase should ever be asked for.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import signature
from .matching import (Match, _somebody_elses_block, _sublist_at, distribute,
                       in_someone_elses_section, match_blank, normalize)
from .model import Blank, BlankKind, Document
from .profile import BY_KEY, CHOICES, Profile, alternatives_for


@dataclass
class Entry:
    """One decision about one blank."""

    blank: Blank
    match: Match | None = None
    value: str | None = None
    # A drawn signature, placed over the blank rather than typed into it.
    image: bytes | None = None
    note: str = ""
    # A key the matcher thinks likely but was not sure enough to write. Offered
    # for one click, never filled on its own.
    suggested_key: str | None = None
    # Set when the form asks for something this identity could answer but has
    # not got stored. The difference from any other empty blank matters: this
    # one is fixable, and the caller should offer to fix it rather than bury it.
    missing_key: str | None = None
    # Left empty on purpose, by the person now or as they did last time. Kept
    # as a fact so that saving again keeps it, whatever the note says.
    cleared: bool = False

    @property
    def filled(self) -> bool:
        return bool(self.value) or self.image is not None

    def resolve_to(self, key: str, value: str, note: str, source: str) -> None:
        """Fill this entry from a key decided after planning, by the model or
        by memory. Everything that describes the entry changes together: its
        key (so masking and memory see the right one), its value, and whatever
        it was offered or missing before."""
        self.match = Match(key, 1.0, self.blank.label.best(), source)
        self.value, self.image = value, None
        self.suggested_key = self.missing_key = None
        self.note = note


@dataclass
class Plan:
    document: Document
    entries: list[Entry] = field(default_factory=list)
    # Either/or fields the document offered and the profile could both satisfy.
    conflicts: list[frozenset[str]] = field(default_factory=list)
    # Keys the planner decided against (SSN when EIN is preferred, say). What
    # comes after planning, memory and the model, must not bring them back.
    ruled_out: set[str] = field(default_factory=set)

    def values(self) -> dict[str, str]:
        return {e.blank.id: e.value for e in self.entries if e.value}

    def images(self) -> dict[str, bytes]:
        return {e.blank.id: e.image for e in self.entries if e.image is not None}

    @property
    def filled(self) -> list[Entry]:
        return [e for e in self.entries if e.filled]

    @property
    def sensitive_used(self) -> set[str]:
        """Sensitive keys that will be written, from what is filled now, so a
        change by the model, memory or a hand edit can never leave it stale."""
        return {e.match.key for e in self.entries
                if e.filled and e.match is not None and e.match.key in BY_KEY
                and BY_KEY[e.match.key].sensitive}

    @property
    def unmatched(self) -> list[Entry]:
        return [e for e in self.entries if not e.filled and not e.blank.readonly]

    @property
    def suggested(self) -> list[Entry]:
        return [e for e in self.entries if e.suggested_key and not e.filled]

    def missing(self) -> list[str]:
        """Keys this form asks for that the identity has not got, in form order."""
        seen: list[str] = []
        for entry in self.entries:
            if entry.missing_key and entry.missing_key not in seen:
                seen.append(entry.missing_key)
        return seen


def _decide_alternatives(wanted: set[str], profile: Profile,
                         prefer: set[str]) -> tuple[set[str], list[frozenset[str]]]:
    """Rule out one half of every either/or this document offers.

    The W-9 prints a literal "or" between its Social Security row and its
    Employer Identification row. Filling both is not a cosmetic problem, it is a
    wrong answer on a document signed under penalty of perjury. So when a form
    offers both and the profile can satisfy both, neither is used and the caller
    is told to choose.

    Decided on key *names* only, before any value is read, so that a preference
    already expressed never costs a passphrase prompt.
    """
    ruled_out: set[str] = set()
    conflicts: list[frozenset[str]] = []

    for key in sorted(wanted):
        others = alternatives_for(key) & wanted
        if not others:
            continue
        group = others | {key}

        # A stated preference settles it outright, whether or not the other
        # side is stored. A person files under a Social Security number, so the
        # W-9's Employer Identification row is not a gap in their details and
        # must not be offered as one.
        stated = {k for k in group if k in prefer}
        if stated:
            ruled_out |= group - {sorted(stated)[0]}
            continue

        satisfiable = {k for k in group if profile.has(k)}
        if len(satisfiable) < 2:
            continue  # only one of them is answerable, so there is no choice
        chosen = next((k for k in sorted(satisfiable) if k in prefer), None)
        if chosen is None:
            ruled_out |= satisfiable
            if frozenset(satisfiable) not in conflicts:
                conflicts.append(frozenset(satisfiable))
        else:
            ruled_out |= satisfiable - {chosen}
    return ruled_out, conflicts


def build(doc: Document, profile: Profile, prefer: set[str] | None = None) -> Plan:
    """Decide what goes where, without touching the file."""
    prefer = prefer or set()
    plan = Plan(document=doc)

    # --- pass one: what does this document ask for ------------------------
    # Split runs are one decision, not several: three boxes under a single
    # "Social security number" label hold one value between them. A run is only
    # a candidate until its matched key turns out to have a digit grouping of
    # the same length, so two unrelated neighbouring fields cannot be merged.
    group_matches: dict[str, tuple[list[Blank], Match]] = {}
    for group_id, members in doc.groups().items():
        anchor = next((b for b in members if b.label.best()), members[0])
        match = match_blank(anchor, profile)
        if match is None:
            continue
        spec = BY_KEY[match.key]
        if not spec.split or len(spec.split) != len(members):
            continue  # dissolve: the run is not this key's shape
        # The same bar as a single blank: a split run is written only on an
        # exact match, only if every box in it can take a value, and never
        # from a scan's OCR. Otherwise it dissolves, and each box is judged
        # on its own, as a suggestion at most.
        if not match.exact or any(b.readonly for b in members) or any(
                isinstance(b.native, dict) and b.native.get("scanned") for b in members):
            continue
        group_matches[group_id] = (members, match)

    grouped_ids = {b.id for members, _ in group_matches.values() for b in members}

    single_matches: list[tuple[Blank, Match | None]] = []
    for blank in doc.blanks:
        if blank.id in grouped_ids:
            continue
        # A checkbox takes a state, never a string. Its label is often the same
        # sentence as the text box beside it ("LLC. Enter the tax classification
        # (C = ...)"), and matching it as text wrote the LLC letter into the LLC
        # checkbox. Boxes are decided by _tick_choices alone.
        is_box = blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO)
        single_matches.append(
            (blank, None if blank.readonly or is_box else match_blank(blank, profile)))

    wanted = {m.key for _, m in group_matches.values()}
    wanted |= {m.key for _, m in single_matches if m}

    ruled_out, plan.conflicts = _decide_alternatives(wanted, profile, prefer)
    plan.ruled_out = set(ruled_out)

    # --- pass two: fetch values for what survived -------------------------
    def is_missing(key: str) -> bool:
        return key not in ruled_out and not profile.has(key)

    def note_for(key: str) -> str:
        spec = BY_KEY[key]
        if spec.choice:
            return (f"{spec.title} is decided by the box beside it" if profile.has(key)
                    else f"{spec.title} is not set")
        if spec.image:
            return (f"stored {spec.title.lower()} could not be read"
                    if profile.has(key) else f"{spec.title} is not drawn yet")
        if key in ruled_out:
            others = sorted((alternatives_for(key) & wanted) | {key})
            return (f"this form wants {' or '.join(others)}; "
                    f"choose with --prefer {others[0]}"
                    if any(frozenset(others) <= c for c in plan.conflicts)
                    else f"not used; another of {' or '.join(others)} was preferred")
        return (f"{spec.title} is not set"
                + (" (sensitive: omaform vault set, or --once)" if spec.sensitive else ""))

    # A form with its own apartment box gets the apartment there; one without
    # gets it folded into the street line, or it is lost.
    apartment_box_on_form = "address2" in wanted

    def resolve(key: str) -> str | None:
        # An image is placed, a choice ticks a box: neither is ever text.
        if key in ruled_out or not profile.has(key) or BY_KEY[key].image \
                or BY_KEY[key].choice:
            return None
        if key == "address1" and not apartment_box_on_form:
            return profile.street_line()
        return profile.get(key)

    def resolve_image(key: str) -> bytes | None:
        """The drawn signature, decoded. Never typed: the I-9 asks for it in a
        plain text field, and base64 in there is not a signature."""
        if key in ruled_out or not profile.has(key):
            return None
        return signature.decode(profile.get(key) or "")

    for members, match in group_matches.values():
        spec = BY_KEY[match.key]
        value = resolve(match.key)
        if not value:
            for blank in members:
                plan.entries.append(Entry(
                    blank=blank, match=match, note=note_for(match.key),
                    missing_key=match.key if is_missing(match.key) else None))
            continue
        parts = distribute(value, members, spec)
        for blank in members:
            plan.entries.append(Entry(
                blank=blank, match=match, value=parts.get(blank.id) or None,
                note=f"{spec.title} part {blank.group_index + 1} of {len(members)}"))

    for blank, match in single_matches:
        if blank.readonly:
            plan.entries.append(Entry(blank=blank, note="read only"))
            continue
        if match is None:
            note = ("decided by its label" if blank.kind in (BlankKind.CHECKBOX,
                                                              BlankKind.RADIO)
                    else "no confident match")
            plan.entries.append(Entry(blank=blank, note=note))
            continue
        spec = BY_KEY[match.key]
        scanned = isinstance(blank.native, dict) and blank.native.get("scanned")
        if not match.exact or scanned:
            # Near enough to mention, not near enough to write. A scan's labels
            # were read by OCR rather than given by the form, so even an exact
            # match there is offered for a look first.
            plan.entries.append(Entry(
                blank=blank, match=match, suggested_key=match.key,
                note=(f"probably {spec.title}, read from a scan" if scanned
                      else f"probably {spec.title}, but not a sure match")))
            continue
        if spec.image:
            png = resolve_image(match.key)
            if png is None:
                plan.entries.append(Entry(
                    blank=blank, match=match, note=note_for(match.key),
                    missing_key=match.key if is_missing(match.key) else None))
            else:
                plan.entries.append(Entry(blank=blank, match=match, image=png,
                                          note="drawn over the line"))
            continue
        value = resolve(match.key)
        if not value:
            plan.entries.append(Entry(
                blank=blank, match=match, note=note_for(match.key),
                missing_key=match.key if is_missing(match.key) else None))
            continue
        plan.entries.append(Entry(blank=blank, match=match, value=value))

    _tick_choices(doc, profile, plan)

    # Entries come out in document order, not in the order decisions were made.
    order = {b.id: i for i, b in enumerate(doc.blanks)}
    plan.entries.sort(key=lambda e: order.get(e.blank.id, 0))
    return plan


def _tick_choices(doc: Document, profile: Profile, plan: Plan) -> None:
    """Tick the checkbox whose printed label names the identity's answer.

    Checkboxes are never matched by the text path: "C corporation" is not a
    value anyone stores. Instead each choice key's answers carry the words
    printed beside their boxes. A box stands for every answer whose words open
    its label, which is how the W-4's "Single or Married filing separately"
    serves two answers and the W-9's one LLC box serves three. The box is
    ticked if it stands for the answer stored; the others in the group are
    left exactly as they were.
    """
    decided = {e.blank.id for e in plan.entries if e.filled}
    by_id = {e.blank.id: e for e in plan.entries}

    for blank in doc.blanks:
        if blank.kind not in (BlankKind.CHECKBOX, BlankKind.RADIO) or blank.readonly:
            continue
        # A box in another party's section or block is theirs to tick, however
        # well its words name one of your answers.
        if in_someone_elses_section(blank) or _somebody_elses_block(
                blank.label.row, blank.label.section, blank.label.inside,
                blank.label.left, blank.label.above, blank.label.right):
            continue
        tokens = normalize(blank.label.right or blank.label.best())
        entry = by_id.get(blank.id)
        if not tokens or entry is None or blank.id in decided:
            continue
        for key, options in CHOICES.items():
            stands_for = {opt for opt, (_title, aliases) in options.items()
                          if any(_sublist_at(tokens, normalize(a)) == 0 for a in aliases)}
            if not stands_for:
                continue
            chosen = profile.get(key) if profile.has(key) else None
            if chosen not in options:
                # The form has this group and the identity has not answered:
                # a gap worth pointing out, not a silent miss.
                entry.note = f"{BY_KEY[key].title} is not set"
                entry.missing_key = key
            elif chosen in stands_for:
                entry.value = "checked"
                entry.match = Match(key, 1.0, blank.label.best(), "right")
                entry.note = options[chosen][0]
            else:
                entry.note = "not this one"
            break
