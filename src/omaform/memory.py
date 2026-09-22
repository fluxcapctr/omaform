"""Remember how a form was filled, once it has been saved.

Pressing Fill and save is the check: the page was looked at and accepted. So
that is when the form is remembered, with no separate step. The next time the
same form opens it fills the way it was saved, and the rows say so.

What is kept is structure, never content: which key went in which blank,
which boxes were ticked, which blanks were cleared, and where a signature or
date was placed by hand. Values are resolved from the profile at the time,
exactly as a fresh fill would resolve them. Free text typed for one occasion
is not kept, because it was for that occasion.

Keyed by the form's fingerprint, so a revised edition of a form is a new form
and is read fresh.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.pdf import placed_by_hand
from .model import BlankKind, Document
from .profile import BY_KEY, data_dir


@dataclass
class Memory:
    keys: dict[str, str] = field(default_factory=dict)        # blank id -> profile key
    # Boxes ticked by hand, per identity. Boxes the planner ticks from a stored
    # answer (filing status, citizenship) are not kept: they are worked out
    # again from whoever is filling, so one person's answer never follows the
    # form to another.
    checked: dict[str, list[str]] = field(default_factory=dict)
    cleared: dict[str, list[str]] = field(default_factory=dict)  # identity slug -> ids
    placed: list[dict] = field(default_factory=list)          # hand-placed blanks
    moved: dict[str, list[float]] = field(default_factory=dict)  # blank id -> [dx, dy]
    saved_by: str = ""

    def to_json(self) -> dict:
        return {"keys": self.keys, "checked": self.checked, "cleared": self.cleared,
                "placed": self.placed, "moved": self.moved, "saved_by": self.saved_by}

    @classmethod
    def from_json(cls, data: dict) -> "Memory":
        return cls(keys={str(k): str(v) for k, v in (data.get("keys") or {}).items()},
                   checked=_checked_from_json(data),
                   cleared={str(k): [str(x) for x in v]
                            for k, v in (data.get("cleared") or {}).items()},
                   placed=[p for p in data.get("placed") or [] if isinstance(p, dict)],
                   moved={str(k): [float(v[0]), float(v[1])]
                          for k, v in (data.get("moved") or {}).items()
                          if isinstance(v, (list, tuple)) and len(v) == 2},
                   saved_by=str(data.get("saved_by") or ""))


def _checked_from_json(data: dict) -> dict[str, list[str]]:
    raw = data.get("checked") or {}
    if isinstance(raw, list):
        # The first format kept one list for everyone; it was whoever saved.
        return {str(data.get("saved_by") or ""): [str(x) for x in raw]}
    return {str(k): [str(x) for x in v] for k, v in raw.items() if isinstance(v, list)}


def _store(fingerprint: str) -> Path:
    return data_dir() / "forms" / f"{fingerprint}.json"


def recall(doc: Document) -> Memory | None:
    path = _store(doc.fingerprint)
    if not path.exists():
        return None
    try:
        return Memory.from_json(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None


def forget(doc: Document) -> None:
    try:
        _store(doc.fingerprint).unlink()
    except FileNotFoundError:
        pass


def remember(doc: Document, plan, identity_slug: str) -> Memory:
    """Record the shape of a plan that was just saved."""
    memory = recall(doc) or Memory()
    memory.saved_by = identity_slug
    memory.keys = {}
    ticked: list[str] = []
    for entry in plan.entries:
        blank = entry.blank
        by_hand = isinstance(blank.native, dict) and blank.id.split(":", 1)[1].startswith("~hand")
        if by_hand:
            continue  # recorded below, from the blank itself
        if entry.value == "checked":
            spec = BY_KEY.get(entry.match.key) if entry.match else None
            if spec is None or not spec.choice:
                ticked.append(blank.id)   # by hand; a choice is re-derived
        elif entry.filled and entry.match is not None:
            spec = BY_KEY.get(entry.match.key)
            if spec is not None and not spec.choice:
                memory.keys[blank.id] = entry.match.key
        # A value with no matched key was typed for the occasion: not kept.
    memory.checked[identity_slug] = ticked
    memory.cleared[identity_slug] = [
        e.blank.id for e in plan.entries
        if not e.filled and e.match is not None and not e.blank.readonly
        and e.cleared]
    # Hand-placed blanks keep their final spot; anything else dragged keeps
    # how far it was dragged, since its own position comes from the form.
    memory.moved = {b.id: [b.offset[0], b.offset[1]] for b in doc.blanks
                    if b.offset != (0.0, 0.0)
                    and not b.id.split(":", 1)[1].startswith("~hand")}
    memory.placed = [
        {"page": b.page, "kind": b.kind.value,
         "x": b.rect[0] + b.offset[0], "y": b.rect[1] + b.offset[1]}
        for b in doc.blanks
        if isinstance(b.native, dict) and b.id.split(":", 1)[1].startswith("~hand")
        and b.rect is not None and b.kind in (BlankKind.SIGNATURE, BlankKind.DATE)]

    path = _store(doc.fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(memory.to_json(), fh, indent=2)
    os.replace(tmp, path)
    return memory


def restore_placements(memory: Memory, doc: Document) -> int:
    """Put hand-placed signature and date blanks back on the document."""
    for b in doc.blanks:
        if b.id in memory.moved:
            b.offset = tuple(memory.moved[b.id])
    have = {(b.page, b.kind.value, round(b.rect[0]), round(b.rect[1]))
            for b in doc.blanks if b.rect is not None}
    added = 0
    for item in memory.placed:
        try:
            page, kind = int(item["page"]), BlankKind(item["kind"])
            x, y = float(item["x"]), float(item["y"])
        except (KeyError, ValueError, TypeError):
            continue
        if (page, kind.value, round(x), round(y)) in have:
            continue
        doc.blanks.append(placed_by_hand(page, kind, x, y))
        added += 1
    return added


def apply(memory: Memory, plan, profile, identity_slug: str) -> list:
    """Lay a remembered fill over a fresh plan. Returns the entries it changed.

    Runs before the model's reading and before the person's own edits for this
    session, both of which win. A key remembered for a blank is resolved from
    the profile now, through the same rules the planner used: a key it ruled
    out (SSN when EIN is preferred) stays out, a split number is spread across
    its boxes rather than typed whole into each, and a box that stands for a
    stored answer is left to that answer.
    """
    from .matching import _leading_qualifier, distribute, in_someone_elses_section

    changed = []
    cleared = set(memory.cleared.get(identity_slug, []))
    ticked = set(memory.checked.get(identity_slug, []))
    by_group: dict[str, list] = {}
    for entry in plan.entries:
        if entry.blank.group:
            by_group.setdefault(entry.blank.group, []).append(entry)
    done_groups: set[str] = set()

    for entry in plan.entries:
        blank = entry.blank
        if blank.readonly:
            continue
        if blank.id in cleared:
            entry.cleared = True   # so saving again keeps it cleared
            if entry.filled:
                entry.value, entry.image = None, None
                entry.note = "left empty, as last time"
                changed.append(entry)
            continue
        if blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
            # This identity has a stored answer and this box is not it: the
            # answer decides. With no answer stored, last time's tick stands.
            decided_by_answer = entry.note == "not this one"
            if blank.id in ticked and not entry.filled and not decided_by_answer:
                entry.value, entry.note = "checked", "ticked, as last time"
                changed.append(entry)
            continue
        key = memory.keys.get(blank.id)
        if not key or entry.filled or key in plan.ruled_out:
            continue
        spec = BY_KEY.get(key)
        if spec is None or spec.image or spec.choice:
            continue
        # The same ownership checks a fresh fill makes. An empty entry is not
        # permission to fill: it may be empty because it is someone else's.
        if in_someone_elses_section(blank) or _leading_qualifier(blank, spec):
            continue
        if not (key == "date_today" or profile.has(key)):
            continue
        if blank.group and spec.split:
            if blank.group in done_groups:
                continue
            done_groups.add(blank.group)
            members = sorted(by_group.get(blank.group, []), key=lambda e: e.blank.group_index)
            if len(members) != len(spec.split) or any(e.filled for e in members):
                continue
            value = profile.get(key)
            if not value:
                continue
            parts = distribute(value, [e.blank for e in members], spec)
            for member in members:
                part = parts.get(member.blank.id)
                if part:
                    member.resolve_to(key, part, f"{spec.title}, as last time", "memory")
                    changed.append(member)
            continue
        if blank.group:
            continue  # one box of a split run is never given the whole value
        value = profile.get(key)
        if value:
            entry.resolve_to(key, value, f"{spec.title}, as last time", "memory")
            changed.append(entry)
    return changed
