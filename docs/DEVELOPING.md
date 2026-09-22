# Developing Omaform

How the code is laid out, how it is tested, and the rules it keeps. This is a
reference for people reading or changing the code; installation is in the README.

## Setting up

Python 3.11 or later, GTK 4 and libadwaita through PyGObject, pikepdf and Poppler.

```console
python -m venv --system-site-packages .venv
.venv/bin/pip install --require-hashes --no-deps -r requirements.lock
.venv/bin/pip install --no-deps --no-build-isolation -e . pytest
.venv/bin/python -m pytest -q
```

Window tests need a Wayland or X display and are skipped without one; scan tests need
tesseract; one test needs LibreOffice. `OMAFORM_HOME=$(mktemp -d) omaform-ui` runs the
window against throwaway data, as its own instance, so it never reaches a real window.

Dependencies are pinned in `requirements.in` and locked with hashes in
`requirements.lock`, which is what `install.sh` installs from. After changing a pin:

```console
uv pip compile requirements.in --generate-hashes --universal --python-version 3.11 \
  -o requirements.lock
```

## Layout

- `src/omaform/adapters/` reads and writes each format (PDF, Word).
- `labeling.py` finds a blank's label from the page; `matching.py` and `plan.py` decide
  what goes where.
- `vault.py` is the encrypted store; `memory.py` remembers forms by fingerprint.
- `llm.py` is the optional model reading; `doctor.py` reports what is installed.
- `redact.py`, `pages.py`, `scan.py`, `flat.py`, `watch.py` are the other features.
- `ui/` is the window; `ui/onboarding.py` is the first-run setup.
- `Panel.qml` and `manifest.json` are the Omarchy bar widget.

## How a model is used

Only when someone presses "Ask a model", or passes `--ask-model` to `omaform fill`. The
model is whichever agent `omarchy default agent` names, if it has a headless mode
(`HEADLESS` in `llm.py`), or a local Ollama model. The prompt carries the form's
questions, scrubbed of anything shaped like an answer, and the names of stored details,
never their values; blanks are named by opaque numbers. The reply maps those numbers to
detail names, which Omaform resolves locally, so a model never supplies a value. Its
reasons and questions are scrubbed again before they are shown or cached. See
`build_prompt`, `scrub` and `apply`, and `tests/test_llm.py` and
`tests/test_review_round*.py`.

## Rules the code keeps

These are tested; a change that breaks one is a bug, whatever else it fixes.

1. A source file is never modified. Every write goes to a new path.
2. Profile values never reach a model.
3. Sensitive details live only in the vault, never in profile files, memory, logs,
   notifications or temporary files left behind.
4. Only exact matches fill. Anything fuzzy is a suggestion. Another party's section
   (employer, preparer, office use) is never filled.
5. Memory keeps structure (which detail went in which blank), never values.
6. A black-out removes content from the file; it does not cover it.

The repository, tests, screenshots and video use made-up details only, such as
"Alex Rivera", 1200 Maple Avenue, 123-45-6789.
