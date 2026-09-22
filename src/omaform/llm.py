"""Ask a language model to read a form, when you choose to.

Never automatic. The heuristics fill what they are sure of; this is for the
forms with "complete Part III only if..." baked in, which no word list reads.

What the model sees is the form's *questions* and the *names* of your profile
keys. It never sees a value: it learns that a key called `ssn` exists, not what
it holds. So the worst case with a remote backend is that a third party reads
a blank form that was very likely published on irs.gov anyway. The answer is
a mapping, applied locally afterwards, and it is kept per form so the model
runs once for each new form you meet, not every time.

Two backends:

* **Ollama on this machine**, preferring Omarchy's bundled `omarchy` model. A
  localhost call: nothing leaves the box.
* **Omarchy's default agent**, whichever one `omarchy default agent` names,
  run in its headless mode. Omarchy's own launcher opens the agent in a
  terminal, which a program cannot read back from, so the agent's print mode
  is used instead. This can be a cloud service, and the window says so.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .model import Blank, BlankKind, Document
from .profile import SCHEMA, data_dir

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
PREFERRED_LOCAL_MODELS = ("omarchy", "qwen3.6:35b-a3b", "gpt-oss:20b", "qwen3-coder:30b")

# How each agent Omarchy knows is run for one answer, without a terminal.
HEADLESS = {
    "claude": ["claude", "-p", "--output-format", "text"],
    "codex": ["codex", "exec", "--skip-git-repo-check"],
    "opencode": ["opencode", "run"],
    "gemini": ["gemini", "-p"],
    "copilot": ["copilot", "-p"],
    "cursor-agent": ["cursor-agent", "-p", "--output-format", "text"],
    "crush": ["crush", "run"],
    "pi": ["pi", "-p"],
    "hermes": ["hermes", "-p"],
}
# These leave the machine unless the user has pointed them at a local model.
REMOTE_AGENTS = {"claude", "codex", "opencode", "gemini", "copilot", "cursor-agent", "crush"}
# How Omarchy names them, for people.
AGENT_NAMES = {"claude": "Claude Code", "codex": "Codex", "opencode": "OpenCode",
               "gemini": "Gemini", "copilot": "GitHub Copilot", "cursor-agent": "Cursor",
               "crush": "Crush", "pi": "Pi", "hermes": "Hermes"}


def agent_status() -> dict:
    """What onboarding and `omaform doctor` say about the Omarchy agent.

    `chosen` is what `omarchy default agent` says, `usable` whether Omaform can
    run it without a terminal, and `why` what to do when it cannot."""
    name = default_agent()
    if not name:
        return {"chosen": None, "usable": False,
                "why": "No Omarchy agent is chosen yet. Pick one with: omarchy default agent"}
    label = AGENT_NAMES.get(name, name)
    if name not in HEADLESS:
        return {"chosen": name, "label": label, "usable": False,
                "why": f"{label} has no mode Omaform can run in the background."}
    if not shutil.which(HEADLESS[name][0]):
        return {"chosen": name, "label": label, "usable": False,
                "why": f"{label} is chosen but not installed. Run: omarchy default agent {name}"}
    return {"chosen": name, "label": label, "usable": True,
            "remote": name in REMOTE_AGENTS, "why": ""}


def test_backend(backend: str, timeout: float = 90.0) -> str:
    """Ask the backend for one word, to show the connection works. Sends
    nothing about the person or any form."""
    prompt = "Reply with exactly the word: ready"
    if backend == "agent":
        name = agent_available()
        if not name:
            raise ModelError(agent_status()["why"])
        answer = ask_agent(prompt, name, timeout=timeout)
    elif backend == "ollama":
        model = pick_local_model()
        if not model:
            raise ModelError("Ollama is not running, or has no model pulled")
        answer = ask_ollama(prompt, model)
    else:
        raise ModelError(f"unknown backend {backend!r}")
    return answer.strip()[:80] or "(an empty reply)"


class ModelError(Exception):
    """The model could not be reached, or did not answer usably."""


# -- what the model is asked -------------------------------------------------

def key_catalogue() -> list[dict]:
    """The profile keys by name and title. Names only, never values."""
    return [{"key": f.key, "means": f.title} for f in SCHEMA
            if f.key not in ("date_today", "llc_tax_code")]


# Shapes that are answers, never questions: whatever else a label says, these
# are taken out before it leaves the machine. Any run of three or more digits
# goes, which takes the pieces of a split number with it ("123-45-" in one slot
# and "6789" in the next) along with dates and ZIP codes; a form's own numbers
# ("OMB No. 1615-0047") are no loss to a reader deciding whose box this is.
_ANSWER_SHAPES = [
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),                  # email
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"),              # dates
    re.compile(r"\d(?:[\s().-]*\d){2,}"),                            # 3+ digits
]

# "Date of birth: 01/02/1990". In text read as a stream (a Word document, or
# a flat page Omaform wrote on) the answer sits right after its own label, so
# whatever follows a colon in such a label is treated as an answer.
_AFTER_COLON = re.compile(r":[ \t]*[^:_\s][^:]*?(?=\s{2,}|\t|$)")


def _norm(text: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", text)


def scrub(text: str, known: list[str] = (), stream: bool = False) -> str:
    """A label with any answer in it removed.

    Label text is read from the document, and a document that was filled
    before carries its answers right beside the next question: a Word form's
    "Email address: alex@example.com" becomes the "above" of the phone box.
    So every value the document already holds and every stored value in hand
    is cut out, longest first and in one Unicode form, and so is anything
    shaped like an answer. `stream` also cuts what follows a colon.
    """
    if not text:
        return text
    text = _norm(text)
    for value in sorted({_norm(v).strip() for v in known if v and v.strip()},
                        key=len, reverse=True):
        if len(value) < 2:
            # "A" cannot be cut from prose without cutting every "A"; as a
            # whole word after a colon, it is cut below.
            text = re.sub(rf"(:\s*){re.escape(value)}\b", r"\1[answer]", text)
            continue
        text = re.sub(re.escape(value), "[answer]", text, flags=re.IGNORECASE)
    for shape in _ANSWER_SHAPES:
        text = shape.sub("[answer]", text)
    if stream:
        text = _AFTER_COLON.sub(": [answer]", text)
    return text


def _is_stream_label(blank: Blank, doc_fmt: str) -> bool:
    return doc_fmt == "docx" or (isinstance(blank.native, dict)
                                 and bool(blank.native.get("synthetic")))


def describe_blank(blank: Blank, proposed: str | None = None,
                   known: list[str] = (), question_id: str = "", stream: bool = False) -> dict:
    """One blank as the model sees it: where it is, what is printed near it,
    and which key the matcher put there, if any, so that it can be argued with.

    It is named by an opaque question number, never its own id: a PDF's ids
    carry the source's field names, and a field can be named anything."""
    label = blank.label
    out = {"id": question_id or blank.id, "kind": blank.kind.value, "page": blank.page,
           "width_pt": round(blank.width_pt)}
    for name, text in (("beside", label.right or label.left), ("above", label.above),
                       ("section", label.section), ("field_name", label.native_name)):
        if text:
            out[name] = scrub(text, list(known), stream)[:220]
    if proposed:
        out["matcher_put"] = proposed
    return out


def question_ids(doc: Document) -> dict[str, str]:
    """Blank id to the opaque number the model sees, stable for one document."""
    return {b.id: f"q{n}" for n, b in enumerate(
        (b for b in doc.blanks if not b.readonly), start=1)}


def build_prompt(doc: Document, identity_kind: str, filled_keys: dict[str, str],
                 known_values: list[str] = ()) -> str:
    """The whole question, as text. Contains no profile values by construction.

    An audit, not a cold read. The matcher's own choice is shown on every blank
    it filled, and the model is asked to keep, change or clear each one. A word
    list is confident in exactly the way that produces wrong fills, and a second
    reader that knows what the first did is the cheapest correction there is.
    """
    # Every answer already in the document, plus the caller's values in hand.
    known = [b.value for b in doc.blanks if b.value and b.value != "checked"]
    known += list(known_values)
    ids = question_ids(doc)
    blanks = [describe_blank(b, filled_keys.get(b.id), known, ids[b.id],
                             _is_stream_label(b, doc.fmt))
              for b in doc.blanks if not b.readonly]
    return (
        "You are checking a form that a simple word-matching program has filled in "
        f"on behalf of a {identity_kind}, and filling what it missed. For each blank "
        "below, decide which of the person's stored details belongs in it, or that it "
        "should be left empty.\n\n"
        "Where a blank shows 'matcher_put', that is the key the program chose. Judge "
        "it. Answer the same key to keep it, a different key to correct it, or null "
        "to clear it. Be strict: a wrong value on a signed form is worse than an "
        "empty box, so when in doubt, clear.\n\n"
        "Rules:\n"
        "- Use only keys from the catalogue. Answer null when no key fits, when the "
        "blank belongs to another party (an employer, preparer, requester, witness, "
        "notary, spouse), or when an instruction such as 'complete only if...' means "
        "it should not be filled without knowing something you were not told.\n"
        "- Checkboxes are decided locally from stored facts you cannot see. Answer "
        "null for every checkbox.\n"
        "- Dates asking for today may take 'date_today'. Expiry, birth and hire "
        "dates may not.\n"
        "- If deciding a blank would need a fact you do not have (marital status, "
        "citizenship, whether a condition applies), leave it null and put the "
        "question in 'questions'.\n\n"
        "Reply with JSON only, of the form:\n"
        '{"blanks": [{"id": "...", "key": "<catalogue key>|date_today|null", '
        '"why": "<short reason>"}], "questions": ["..."]}\n\n'
        f"Stored detail keys (names only):\n{json.dumps(key_catalogue())}\n\n"
        f"Blanks:\n{json.dumps(blanks)}\n"
    )


# -- the answer --------------------------------------------------------------

@dataclass
class Reading:
    """What the model made of the form."""

    mapping: dict[str, str | None] = field(default_factory=dict)   # blank id -> key
    reasons: dict[str, str] = field(default_factory=dict)
    questions: list[str] = field(default_factory=list)
    backend: str = ""

    def to_json(self) -> dict:
        return {"mapping": self.mapping, "reasons": self.reasons,
                "questions": self.questions, "backend": self.backend}

    @classmethod
    def from_json(cls, data: dict) -> "Reading":
        return cls(mapping=dict(data.get("mapping", {})),
                   reasons=dict(data.get("reasons", {})),
                   questions=list(data.get("questions", [])),
                   backend=str(data.get("backend", "")))


def parse_answer(text: str, doc: Document, backend: str, known: list[str] = ()) -> Reading:
    """Pull the JSON out of whatever the model said around it.

    The model's own words (its reasons and questions) are scrubbed the same
    way labels are before they are kept or shown: a model can repeat anything
    it was told, and what it repeats would otherwise land in a cache file."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ModelError("the model did not answer with JSON")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ModelError(f"the model's JSON did not parse: {exc}") from exc

    by_question = {q: blank_id for blank_id, q in question_ids(doc).items()}
    valid_keys = {f.key for f in SCHEMA}
    reading = Reading(backend=backend)
    for item in data.get("blanks", []) or []:
        if not isinstance(item, dict):
            continue
        blank_id = by_question.get(str(item.get("id", "")))
        if blank_id is None:
            continue
        key = item.get("key")
        key = None if key in (None, "", "null") else str(key)
        if key is not None and key not in valid_keys:
            continue
        reading.mapping[blank_id] = key
        if item.get("why"):
            reading.reasons[blank_id] = scrub(str(item["why"])[:160], known)
    reading.questions = [scrub(str(q)[:200], known)
                         for q in (data.get("questions") or [])][:12]
    return reading


# -- backends ----------------------------------------------------------------

def local_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as response:
            data = json.load(response)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []
    return [m.get("name", "") for m in data.get("models", [])]


def pick_local_model() -> str | None:
    names = local_models()
    for preferred in PREFERRED_LOCAL_MODELS:
        for name in names:
            if name == preferred or name.split(":")[0] == preferred:
                return name
    return names[0] if names else None


def ask_ollama(prompt: str, model: str, timeout: float = 240.0) -> str:
    body = json.dumps({
        "model": model, "stream": False, "format": "json",
        "options": {"temperature": 0, "num_ctx": 16384},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    request = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.URLError as exc:
        raise ModelError(f"Ollama is not answering at {OLLAMA_URL}: {exc.reason}") from exc
    except OSError as exc:
        raise ModelError(f"Ollama call failed: {exc}") from exc
    return data.get("message", {}).get("content", "")


def default_agent() -> str | None:
    """Whichever agent Omarchy has been told is the default, if any."""
    if not shutil.which("omarchy-default-agent"):
        return None
    try:
        done = subprocess.run(["omarchy-default-agent"], capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    name = done.stdout.strip()
    return name or None


def agent_available() -> str | None:
    """The default agent's name, if it is installed and has a headless mode."""
    name = default_agent()
    if name and name in HEADLESS and shutil.which(HEADLESS[name][0]):
        return name
    return None


def ask_agent(prompt: str, name: str, timeout: float = 300.0) -> str:
    command = HEADLESS.get(name)
    if not command or not shutil.which(command[0]):
        raise ModelError(f"{name} is not installed, or has no headless mode")
    try:
        done = subprocess.run(command + [prompt], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ModelError(f"{name} took longer than {int(timeout)}s") from exc
    except OSError as exc:
        raise ModelError(f"could not run {name}: {exc}") from exc
    if done.returncode != 0:
        raise ModelError(f"{name} failed: {(done.stderr or done.stdout).strip()[:200]}")
    return done.stdout


# -- remembered readings -----------------------------------------------------

def _store(fingerprint: str) -> Path:
    return data_dir() / "readings" / f"{fingerprint}.json"


def remembered(doc: Document) -> Reading | None:
    """What the model said about this exact form last time, if it was asked."""
    path = _store(doc.fingerprint)
    if not path.exists():
        return None
    try:
        reading = Reading.from_json(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    # A cache written before replies were scrubbed is scrubbed on the way in.
    reading.reasons = {k: scrub(v) for k, v in reading.reasons.items()}
    reading.questions = [scrub(q) for q in reading.questions]
    return reading


def remember(doc: Document, reading: Reading) -> None:
    path = _store(doc.fingerprint)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(reading.to_json(), fh, indent=2)
    os.replace(tmp, path)


def forget(doc: Document) -> None:
    try:
        _store(doc.fingerprint).unlink()
    except FileNotFoundError:
        pass


# -- the whole thing ---------------------------------------------------------

def read_form(doc: Document, identity_kind: str, filled_keys: dict[str, str],
              backend: str, model: str | None = None,
              known_values: list[str] = ()) -> Reading:
    """Ask, parse, remember. `backend` is 'ollama' or 'agent'.

    `known_values` are the person's values in hand, used only to cut them out
    of label text; they are never themselves put in the prompt."""
    prompt = build_prompt(doc, identity_kind, filled_keys, known_values)
    if backend == "ollama":
        chosen = model or pick_local_model()
        if not chosen:
            raise ModelError("no Ollama model is available")
        answer = ask_ollama(prompt, chosen)
        label = f"ollama:{chosen}"
    elif backend == "agent":
        name = model or agent_available()
        if not name:
            raise ModelError("Omarchy has no default agent with a headless mode; "
                             "pick one with `omarchy default agent <name>`")
        answer = ask_agent(prompt, name)
        label = f"agent:{name}"
    else:
        raise ModelError(f"unknown backend {backend!r}")
    known = [b.value for b in doc.blanks if b.value and b.value != "checked"]
    reading = parse_answer(answer, doc, label, known + list(known_values))
    remember(doc, reading)
    return reading


def apply(reading: Reading, doc: Document, plan, profile) -> tuple[list, list]:
    """Lay the model's reading over a plan. Returns (changed entries, notes).

    The model may add a fill the matcher missed, or take one away that an
    instruction rules out. It never supplies a value itself: a key it names is
    resolved from the profile exactly as the matcher's would be, so nothing the
    model said can put a value on the page that the person did not store.

    It never touches a checkbox. Boxes are ticked from stored facts the model is
    not shown, such as the tax classification, and on its first outing it
    ticked "Individual" for an S corporation on exactly that basis.
    """
    from .profile import BY_KEY

    changed, notes = [], []
    by_id = {e.blank.id: e for e in plan.entries}
    for blank_id, key in reading.mapping.items():
        entry = by_id.get(blank_id)
        if entry is None or entry.blank.readonly:
            continue
        if entry.blank.kind in (BlankKind.CHECKBOX, BlankKind.RADIO):
            continue
        reason = scrub(reading.reasons.get(blank_id, ""), profile.values_in_hand())
        if key is None:
            if entry.filled:
                entry.value, entry.image = None, None
                entry.note = f"left empty by the model: {reason}" if reason else \
                    "left empty by the model"
                changed.append(entry)
            continue
        spec = BY_KEY.get(key)
        if spec is None or spec.image or spec.choice:
            continue
        if key in getattr(plan, "ruled_out", ()) or entry.blank.group:
            # Not a key this identity is using here, or one box of a split
            # number, which only the planner may spread across its run.
            continue
        if not entry.filled and entry.cleared:
            continue
        if entry.filled and entry.match is not None and entry.match.key == key:
            continue  # the matcher had it, and the model agrees
        if key == "date_today" or profile.has(key):
            value = profile.get(key)
            if value:
                was = entry.match.key if (entry.filled and entry.match) else None
                entry.resolve_to(key, value,
                                 f"corrected by the model from {was}: {reason}" if was
                                 else f"model: {reason}" if reason else "filled by the model",
                                 "model")
                changed.append(entry)
        else:
            entry.missing_key = key
            entry.note = f"the model wants {spec.title} here, which is not stored"
            notes.append(entry.note)
    questions = [scrub(q, profile.values_in_hand()) for q in reading.questions]
    return changed, list(dict.fromkeys(notes + questions))
