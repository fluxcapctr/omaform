"""Decide which profile key belongs in which blank, deterministically.

This is the heuristic tier. It handles the common case well and knows when it is
unsure, which is what matters: a wrong value typed confidently into a tax form is
worse than a blank left empty. Anything it scores below THRESHOLD is reported as
unmatched rather than guessed at, and the LLM layer in Phase 8 gets first refusal
on exactly those.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .model import Blank, BlankKind
from .profile import BY_KEY, NOT_YOURS, SCHEMA, Field, Profile

# Below this, say "I do not know" rather than fill something in.
THRESHOLD = 0.55
# A fuzzy match is only ever a suggestion, and a weak one is noise: "stated"
# is not worth offering as "State". Typos in a real label score 0.84 and up.
SUGGEST_THRESHOLD = 0.75
# A fuzzy alias match must be at least this close before it counts at all.
FUZZY_FLOOR = 0.82
# Label tokens beyond this many stop diluting the score. See dilution, below.
DILUTION_CAP = 10
# How hard dilution bites. Single-word aliases like "address" and "city" are
# legitimate and common, and a steeper curve pushed them under THRESHOLD in any
# label with a few words of preamble: the I-9's "Section 1., Enter Address
# (Street Number and Name)" scored 0.49 for address1 and was declined.
DILUTION_POWER = 0.15
# Only this many leading tokens of a section heading name the party it belongs
# to. See in_someone_elses_section.
SECTION_QUALIFIER_TOKENS = 8
# ... and this many leading tokens of a label do the same for the blank itself.
LABEL_QUALIFIER_TOKENS = 6
# How far *before* a matched alias a disqualifying phrase still vetoes it.
# Only before, never after: English puts the qualifier in front of the noun, so
# "Requester's name and address" disqualifies both, while the same word trailing
# eleven tokens behind an address label is just the next column bleeding in.
VETO_BEFORE = 4

_LEAD_NO_RE = re.compile(r"^\d{1,2}[a-z]?\b")
_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_ABBREV = {
    "no": "number", "num": "number", "nbr": "number", "addr": "address",
    "tel": "telephone", "ph": "phone", "st": "street", "apt": "apartment",
}


def normalize(text: str) -> list[str]:
    """Lower-case word tokens, with form line numbers and punctuation stripped."""
    # A rotated title glues onto the next row's first word when the page is
    # read as lines: the W-4's vertical "Employers Only" came out as
    # "OnlyEmployer's". A lower-to-upper case change is a word boundary.
    t = re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower().strip()
    # "5address" happens constantly: IRS forms set the line number hard against
    # the label with no space, and mid-string it survives label cleaning.
    t = re.sub(r"(\d)([a-z])", r"\1 \2", t)
    t = _LEAD_NO_RE.sub(" ", t)
    t = _PUNCT_RE.sub(" ", t)
    return [_ABBREV.get(w, w) for w in t.split() if w]


# The first token of every NOT_YOURS phrase, for row-level checks.
NOT_YOURS_TOKENS = frozenset(normalize(p)[0] for p in NOT_YOURS if normalize(p))


def _sublist_at(hay: list[str], needle: list[str]) -> int | None:
    n = len(needle)
    for i in range(len(hay) - n + 1):
        if hay[i:i + n] == needle:
            return i
    return None


def _window_ratio(hay: list[str], needle: list[str]) -> tuple[float, int]:
    """Closest same-length window of `hay` to `needle`, as a 0..1 ratio.

    Compared as text, not as token lists. Token by token, a one-letter typo in
    "security" counted as a whole word missed and nothing fuzzy ever cleared the
    floor; as text it is a near-match, which is what a suggestion is for. A
    fuzzy match is only ever offered, never written, so the looser comparison
    costs nothing on the page.
    """
    n = len(needle)
    target = " ".join(needle)
    best, at = 0.0, 0
    for i in range(max(1, len(hay) - n + 1)):
        window = " ".join(hay[i:i + n])
        r = difflib.SequenceMatcher(None, window, target).ratio()
        if r > best:
            best, at = r, i
    return best, at


@dataclass
class Match:
    key: str
    score: float
    alias: str
    source: str  # which label slot produced it: inside, right, left, above, native
    # True when the alias was found word for word. Every fill verified right on
    # the real forms was exact; the fuzzy path has produced nothing but
    # near-misses, so a fuzzy match is offered rather than written.
    exact: bool = True

    @property
    def field(self) -> Field:
        return BY_KEY[self.key]


def _vetoed(spec: Field, tokens: list[str], at: int, span: int) -> bool:
    """Is there a disqualifying phrase close enough to poison this alias match?

    Position matters, and so does direction. "Requester's name and address"
    disqualifies both the name key and the address key, because the qualifier
    leads. The same word eleven tokens *behind* an address label is the next
    column bleeding into this one and says nothing about this blank. A global
    veto makes noisy labels unmatchable; a leading-only veto does not.
    """
    lo, hi = at - VETO_BEFORE, at + span - 1
    for phrase in spec.avoid:
        found = _sublist_at(tokens, normalize(phrase))
        if found is not None and lo <= found <= hi:
            return True
    return False


def _score_field(spec: Field, tokens: list[str]) -> tuple[float, str, bool] | None:
    if not tokens:
        return None

    best: tuple[float, str, bool] | None = None
    for alias in spec.aliases:
        atoks = normalize(alias)
        if not atoks:
            continue
        at = _sublist_at(tokens, atoks)
        exact = at is not None
        if exact:
            base = 1.0
        else:
            ratio, at = _window_ratio(tokens, atoks)
            if ratio < FUZZY_FLOOR:
                continue
            base = ratio * 0.9

        # An alias found at the start of the label is more likely to be the
        # label; one found deep inside a long line is probably incidental prose.
        position = 1.0 - 0.18 * (at / max(1, len(tokens)))
        # A longer alias that still matched is a more specific claim: prefer
        # "city state and zip code" over the bare "city" it contains.
        specificity = min(1.0, 0.82 + 0.06 * len(atoks))
        # A short label that is almost entirely the alias beats a long one that
        # merely contains it. The denominator is capped: government labels wrap
        # into long sentences ("1 Name of entity/individual. An entry is
        # required. (For a sole proprietor...)") and without a cap the genuine
        # match at the front gets crushed by the explanatory tail behind it.
        dilution = (len(atoks) / min(len(tokens), DILUTION_CAP)) ** DILUTION_POWER
        dilution = min(1.0, dilution)

        if _vetoed(spec, tokens, at, len(atoks)):
            continue

        score = base * position * specificity * dilution
        if best is None or score > best[0]:
            best = (score, alias, exact)
    return best


def rank(text: str, source: str = "label") -> list[Match]:
    """Every plausible profile key for one piece of label text, best first."""
    tokens = normalize(text)
    out: list[Match] = []
    for spec in SCHEMA:
        hit = _score_field(spec, tokens)
        if hit:
            out.append(Match(spec.key, hit[0], hit[1], source, hit[2]))
    out.sort(key=lambda m: -m.score)
    return out


def _capacity_penalty(blank: Blank, key: str, profile: Profile) -> float:
    """Punish a key whose value obviously cannot fit the box.

    Box width is often the only thing separating a two-character state field from
    a street address, so it is worth consulting even though it is crude.
    """
    cap = blank.capacity
    if cap <= 0 or BY_KEY[key].image:
        # An image's "length" is its encoding, not its size on the page. A
        # signature PNG is thousands of base64 characters against a 60-character
        # box, and without this it scored itself out of every signature line.
        return 1.0
    # peek, not get: scoring must never trigger a passphrase prompt for a
    # candidate that is about to be discarded.
    value = profile.peek(key)
    if not value:
        return 1.0
    if blank.group:  # split boxes hold a slice each, not the whole value
        return 1.0
    if len(value) <= cap:
        return 1.0
    return max(0.25, cap / len(value))


def in_someone_elses_section(blank: Blank) -> bool:
    """Is this blank inside a section another party is supposed to complete?

    A heading carries further than a label: every date and address under
    "Section 2. Employer Review and Verification" belongs to the employer, even
    though the individual boxes are labelled with nothing more than "Date".

    Only the opening of the heading is consulted, and by whole tokens. Headings
    name their party up front, while the sentences that follow mention everyone:
    the I-9's own "Section 1. Employee Information and Attestation" continues
    "...Employers must ensure...", and a looser test hands the employee's entire
    section to the employer.
    """
    tokens = normalize(blank.label.section)[:SECTION_QUALIFIER_TOKENS]
    if not tokens:
        return False
    return any(_sublist_at(tokens, normalize(phrase)) is not None
               for phrase in NOT_YOURS)


_SENTENCE_RE = re.compile(r"[.;:]\s+")


def _sentence_with(text: str, alias_tokens: list[str]) -> list[str]:
    """Tokens of the first sentence of `text` that contains the alias.

    A tooltip has no column bleed, so within one sentence every word is about
    this blank. The I-9's Supplement B signature line opens with a long
    section name and only says "Employer" in its second sentence, past any
    fixed window; the employee's own line says "preparer" in a third sentence
    that is an aside. The sentence the alias lives in is the one that counts.
    """
    for sentence in _SENTENCE_RE.split(text):
        tokens = normalize(sentence)
        if _sublist_at(tokens, alias_tokens) is not None:
            return tokens
    return []


# Blocks no key may fill, whatever its own exemptions. The EIN key has to be
# allowed the word "employer", because that is what its own box is called on a
# W-9; but inside a W-4's "Employers Only" block the same words are the
# employer's. "Only" beside a party word is the tell, in either order, since a
# vertical title can be read out of order.
_HARD_PHRASES = ("office use", "official use", "do not write", "for employer use",
                 "employer use")


def _somebody_elses_block(*texts: str) -> bool:
    for text in texts:
        opening = normalize(text)[:LABEL_QUALIFIER_TOKENS]
        if not opening:
            continue
        if any(_sublist_at(opening, normalize(p)) is not None for p in _HARD_PHRASES):
            return True
        if "only" in opening and any(tok in NOT_YOURS_TOKENS for tok in opening):
            return True
    return False


def _leading_qualifier(blank: Blank, spec: Field, alias: str = "") -> bool:
    """Does this blank's label *open* with something that rules this key out?

    Checked across every label slot including the field's internal name, and
    limited to the opening tokens, which is where a label states whose blank it
    is and what kind: "Preparer or Translator ZIP Code" is the preparer's box no
    matter which slot that text arrived in, and the I-9's "Supplement B.
    Reverification and Rehire ... Enter Today's Date" is a rehire date rather
    than today's, even though the word "rehire" sits too far from "date" for the
    positional veto to reach. The same word trailing at the *end* of a label is
    the neighbouring column bleeding in and means nothing, which is why this
    looks only at the opening.

    Per-key exemptions apply, which is how the EIN field survives leading with
    the word "employer".
    """
    if _somebody_elses_block(blank.label.row, blank.label.section, blank.label.inside,
                             blank.label.left, blank.label.above):
        return True
    vetoes = [normalize(p) for p in spec.avoid]
    if not vetoes:
        return False
    alias_tokens = normalize(alias) if alias else []
    for text in (blank.label.inside, blank.label.right, blank.label.left,
                 blank.label.above, blank.label.native_name):
        opening = normalize(text)[:LABEL_QUALIFIER_TOKENS]
        if opening and any(_sublist_at(opening, v) is not None for v in vetoes):
            return True
        # A tooltip is a sentence or several, with no column bleed, so the
        # sentence that holds the alias can name the party anywhere in it:
        # "Enter the full legal name of the employer". Only that sentence: a
        # later one that merely mentions a preparer says nothing about this box.
        if alias_tokens and blank.label.authoritative:
            sentence = _sentence_with(text, alias_tokens)
            if sentence and any(_sublist_at(sentence, v) is not None for v in vetoes):
                return True
    # The row the label sits in may name the party even where the label does
    # not: "Employers name and address | First date of employment | ...".
    party_words = [v for v in vetoes if v and v[0] in NOT_YOURS_TOKENS]
    row = normalize(blank.label.row)[:4]
    if row and any(_sublist_at(row, v) is not None for v in party_words):
        return True
    return False


def match_blank(blank: Blank, profile: Profile) -> Match | None:
    """Best profile key for one blank, or None when nothing clears THRESHOLD."""
    if blank.readonly or in_someone_elses_section(blank):
        return None

    # Section headings are deliberately absent here. A heading is context for
    # deciding whose blank this is, never a description of the blank itself:
    # ranking it as a label is how "Supplement B. Reverification and Rehire"
    # ends up putting today's date into three of the employer's boxes.
    candidates: list[Match] = []
    for source, text in (("inside", blank.label.inside),
                         ("right", blank.label.right),
                         ("left", blank.label.left),
                         ("above", blank.label.above),
                         ("native", blank.label.native_name)):
        if not text:
            continue
        # A caption inside the box, or a tooltip, is the form's own word for
        # the blank. Text inferred from the page around it is not, and when a
        # caption exists the surroundings are not consulted at all: that is how
        # the W-4's address line borrowed "name" from the column beside it.
        if blank.label.inside and source in ("left", "above"):
            continue
        weight = 1.0 if source in ("inside", "right", "left", "above") else 0.7
        for m in rank(text, source):
            candidates.append(Match(m.key, m.score * weight, m.alias, source, m.exact))

    if blank.kind is BlankKind.SIGNATURE:
        candidates.append(Match("signature", 0.95, "kind", "kind"))

    def rank_key(m: Match) -> tuple[float, int]:
        # On a tie, the more specific alias wins: a box labelled "first name and
        # middle initial" wants the composite key, not bare first_name.
        return (m.score, len(normalize(m.alias)))

    best: Match | None = None
    for m in candidates:
        if _leading_qualifier(blank, BY_KEY[m.key], m.alias):
            continue
        adjusted = Match(m.key, m.score * _capacity_penalty(blank, m.key, profile),
                         m.alias, m.source, m.exact)
        if best is None or rank_key(adjusted) > rank_key(best):
            best = adjusted
    if best and best.score >= (THRESHOLD if best.exact else SUGGEST_THRESHOLD):
        return best
    return None


def distribute(value: str, blanks: list[Blank], spec: Field | None) -> dict[str, str]:
    """Spread one value across a run of split boxes, as on the W-9's TIN row.

    Uses the field's canonical digit grouping when the box count agrees with it,
    which is what makes a Social Security number land as 3 / 2 / 4 rather than
    being chopped by pixel width.
    """
    digits = re.sub(r"\D", "", value) or value
    parts: list[str]
    if spec and spec.split and len(blanks) == len(spec.split):
        parts, at = [], 0
        for n in spec.split:
            parts.append(digits[at:at + n])
            at += n
    else:
        total = sum(max(1, b.capacity) for b in blanks) or 1
        parts, at = [], 0
        for b in blanks:
            take = max(1, round(len(digits) * max(1, b.capacity) / total))
            parts.append(digits[at:at + take])
            at += take
    return {b.id: p for b, p in zip(blanks, parts)}
