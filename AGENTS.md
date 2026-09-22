# Omaform, for AI agents

Read this first whether you are installing Omaform for someone or changing its code.

## Installing it for someone

1. Install: `omarchy plugin add https://github.com/fluxcapctr/omaform --enable`, then
   run `~/.config/omarchy/plugins/io.github.fluxcapctr.omaform/install.sh`. From a clone,
   `./install.sh`. It needs no root. If it stops, it prints the one `pacman -S --needed`
   line to run; show that to the person rather than running it yourself.
2. Check: `omaform doctor` lists what is present and what each missing piece adds.
   `omaform doctor --json` is the same, for a program.
3. Set up: `omaform-ui --setup` opens the first-run pages. Leave the passphrase and the
   personal details to the person. Do not type their SSN, date of birth, signature or
   passphrase anywhere, and do not ask them to paste those into a chat.
4. Connect their agent: Omaform uses whatever `omarchy default agent` names, if that
   agent has a headless mode (see `HEADLESS` in `src/omaform/llm.py`). If none is set,
   tell them to run `omarchy default agent` and pick one; the setup page's refresh
   button then finds it. Ollama with any pulled model is the local alternative.

## How Omaform uses an agent

Only when the person presses "Ask a model" (or passes `--ask-model` to `omaform fill`).
The prompt carries the form's questions, scrubbed of anything shaped like an answer, and
the names of the person's stored details, never the values. Blanks are named by opaque
numbers. The reply is a mapping from those numbers to detail names, which Omaform
resolves locally; the agent never supplies a value. Its reasons and questions are
scrubbed again before they are shown or cached. See `build_prompt`, `scrub` and `apply`
in `src/omaform/llm.py`, and `tests/test_llm.py`, `tests/test_review_round*.py`.

## Working on the code

- Python 3.11+, GTK 4 and libadwaita through PyGObject, pikepdf, Poppler.
  `python -m venv --system-site-packages .venv && .venv/bin/pip install -e . pytest`.
- `./.venv/bin/python -m pytest -q` runs everything. Window tests need a Wayland or X
  display and skip without one; scan tests need tesseract; one test needs LibreOffice.
- Run the window against throwaway data with `OMAFORM_HOME=$(mktemp -d) omaform-ui`. A
  separate `OMAFORM_HOME` runs as its own instance, so it never reuses a real window.
- Map: `adapters/` read and write each format; `labeling.py` finds a blank's label
  from the page; `matching.py` and `plan.py` decide what goes where; `vault.py` is
  the encrypted store; `memory.py` remembers forms by fingerprint; `redact.py`,
  `pages.py`, `scan.py`, `flat.py`, `watch.py`, `doctor.py`; `ui/` is the window.
  `Panel.qml` and `manifest.json` are the Omarchy bar widget.

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

No real personal data in the repository, tests, screenshots or video: use made-up
details such as "Alex Rivera", 1200 Maple Avenue, 123-45-6789.
