# Review prompt

Paste everything below the line into Astra, pointing it at this folder:
`/home/estevens/code/omaform`.

---

Review the Python project at /home/estevens/code/omaform for bugs. This is the first review; the code
is at commit 722f8bd.

Context: Omaform fills forms on Linux (Omarchy) from saved identities: PDF forms with fields, flat
PDFs with no fields, scans, and Word documents. It has a GTK4/libadwaita window, a CLI, and an
encrypted vault for sensitive values like a Social Security number. Read README.md and docs/PLAN.md
first; the PLAN's later sections record why the matcher works the way it does. Python 3.14, venv in
.venv (created with --system-site-packages for PyGObject and pycairo).

Layout:

- src/omaform/model.py: Blank, LabelContext, Document.
- src/omaform/textmap.py, labeling.py: page text from Poppler and the label found around each box.
- src/omaform/matching.py, profile.py, plan.py: which stored value goes in which blank.
- src/omaform/adapters/pdf.py, adapters/docx.py: discover and write, one adapter per format.
- src/omaform/flat.py, scan.py: blanks from drawn rules and underscores, and from OCR on scans.
- src/omaform/vault.py: Argon2id-derived key wrapping a random data key, ChaCha20-Poly1305.
- src/omaform/llm.py: optional model reading (Ollama or `claude -p`); memory.py: remembered forms.
- src/omaform/pages.py, redact.py, watch.py, signature.py, cli.py.
- src/omaform/ui/: app.py (the window), preview.py (page view), pages_tab.py, signature_dialog.py.

Rules the code must keep. A break in any of these is a severity 1 finding:

1. A source file is never overwritten or modified. Every write goes to a new path.
2. Profile values never reach a model. llm.py may send the form's labels and profile key names
   only. Check build_prompt and everything that feeds it, including notes and cached readings.
3. Sensitive keys (SSN, EIN/TIN, date of birth, citizenship, signature, and the rest of
   SENSITIVE_KEYS in profile.py) live only in the vault, never in profiles/*.json, forms/*.json
   (memory), logs, exceptions, toasts, or temp files left behind.
4. The matcher fills only exact matches; anything fuzzy is a suggestion. A fill in another party's
   section (employer, preparer, Supplement A or B, "Office use only") is a bug.
5. Memory stores structure (which key went in which blank), never values.
6. A black-out removes content. After redact.apply, the region's text, the page's form fields, and
   annotations on that page must be gone from the saved file, not just covered.

What I want:

1. Concrete bugs with file:line, what input triggers them, and what goes wrong. Rank by severity.
2. Security and privacy: the vault (vault.py) against its own docstring: KDF parameters, nonce
   reuse, AAD binding, what happens on a wrong passphrase, on a tampered file, on migration from v1,
   and whether plaintext is ever written to disk or left in a temp directory. The keyring and
   OMAFORM_ASKPASS paths. Whether redact.py can leave the original text recoverable (incremental
   save, object streams, XMP, an unreferenced object still in the file).
3. Untrusted input. Every PDF and DOCX is someone else's file. Look for crashes, hangs and unbounded
   work on malformed files through discover(): pikepdf errors that escape, a content stream with a
   huge or cyclic CTM, a DOCX zip bomb or a document.xml with deep nesting or external entities,
   a PNG that is not one, a scan page with millions of dark pixels in scan.raster_rules. Also
   subprocess calls (pdftoppm, tesseract, soffice, xdg-email, notify-send, claude): argument
   injection from file names, timeouts, and what happens when the tool is missing.
4. The matcher and planner (matching.py, labeling.py, plan.py) against the rules above: the
   NOT_YOURS vetoes, the section headings, alternatives (SSN versus EIN by identity kind), apartment
   folding, split SSN and EIN boxes, and checkbox choices (CHOICES). Construct label contexts that
   would fill the wrong value.
5. The writers: PDF appearance streams, comb fields, lock() and flatten, images with soft masks,
   synthetic blanks drawn into page content, rotated pages; the DOCX writer's run surgery in
   _set_span and _set_span_runs, namespace preservation, the image relationship ids, and writing
   twice from one discovery.
6. The GTK layer: callbacks that run after the document changed (the LibreOffice preview thread,
   the model thread, the watcher thread touching GTK off the main loop), dialogs opened twice,
   state that survives loading a new file when it should not (overrides, redactions, last_saved,
   keyboard buffer), and exceptions swallowed inside dialog callbacks.
7. Anything in tests/ that asserts the wrong value, passes for the wrong reason, or would pass with
   the feature removed.

Run the tests: `./.venv/bin/python -m pytest -q` (265 should pass; the window tests need a Wayland or
X display and skip without one; scan tests need tesseract; one DOCX test needs LibreOffice). Report
anything that fails. If you can only read files and not run commands, skip that.

Report only; do not refactor, restyle or fix. Write the report to
/home/estevens/code/omaform/docs/reviews/REVIEW_RESULTS.md. Never put real personal data in the report;
use made-up values like "Alex Rivera" and 123-45-6789. No em dashes in your output.

---

# Round 2 prompt

Paste everything below the line into Astra. It assumes the round 1 findings in `REVIEW_RESULTS.md`
were fixed (commit 04fe2d6).

---

Review the Python project at /home/estevens/code/omaform for bugs, second pass.

Context: Omaform fills forms on Linux from saved identities (PDF forms, flat PDFs, scans, Word
documents), with a GTK4 window, a CLI and an encrypted vault. Read README.md, docs/PLAN.md and
docs/reviews/REVIEW_RESULTS.md first. The results file ends with a status table of how each of the
twenty round 1 findings was fixed; do not re-report those unless a fix is wrong, incomplete, or
introduced a new problem. The six rules from round 1 still hold, and a break in any is severity 1:
sources never modified, no profile values to a model, sensitive keys only in the vault, exact matches
only, memory keeps structure not values, black-outs remove content.

Cover:

1. The round 1 fixes themselves. In particular:
   - `llm.scrub` and `build_prompt` (src/omaform/llm.py): can a value still reach the prompt?
     Consider values shorter than two characters, values with regex-special or Unicode characters,
     a value split across two label slots, dates and ZIP codes, a signature's text, the `why`
     reasons and `questions` a model sends back and whether any of that is later shown or stored,
     and the cached reading file.
   - `Entry.resolve_to` and the derived `Plan.sensitive_used` (src/omaform/plan.py): every path
     that sets a value (hand edits, accepted suggestions, keyboard typing, memory, model) and
     whether masking and the "holds your SSN in plain text" warning are right for each.
   - The new PDF fingerprint (`fingerprint` in src/omaform/adapters/pdf.py): is it stable across
     reopening the same file, across the page view's discovery and the CLI's, and does it change
     when it should? Is anything nondeterministic in it (dict order, object ids, float noise)?
   - `memory.apply` (src/omaform/memory.py): ruled-out keys, split runs, per-identity ticks and
     the old list format, the order memory then model then hand edits, in both the window
     (`rebuild_plan` in src/omaform/ui/app.py) and the CLI (`_recall`, `_consult_model`).
   - Redaction (src/omaform/redact.py): the shown-page coordinate space across CropBox offsets,
     all four rotations, a page whose MediaBox does not start at 0,0; `_prune_form` on radio
     groups and shared parents; whether anything else in the saved file still reaches the removed
     page's old content (named destinations, outlines, page labels, OCG, JavaScript, embedded
     files, the old content stream through an unreferenced but written object).
   - The DOCX writer (src/omaform/adapters/docx.py): right-to-left span ordering when text and a
     picture share a paragraph, `_insert_run_at` on runs with tabs, breaks or several `w:t`,
     relationship id allocation, the temporary folder for locked output, and `_read_body` limits.
   - Vault bounds and legacy key names (src/omaform/vault.py): can a valid vault written by this
     program ever be refused now? Does `keys_for` agree with what `unlock` then returns?
2. Code written since round 1 started: dragging placed items (the `offset` on `Blank` in
   src/omaform/model.py, `_drag_*`, `_hit_rect` and `_draggable_at` in src/omaform/ui/preview.py,
   the writer's use of `shifted`, and `moved` in memory). Look at click versus drag on release,
   dragging off the page or onto another page, a moved signature on a widget that is then locked,
   and offsets on blanks that no longer exist.
3. The window's threads and lifetimes: the plan generation check, the model thread, the Word
   preview thread and its folder, the Downloads watcher (src/omaform/watch.py), and any GTK call
   made off the main loop. Also Save as (`_save_as`, `do_fill(out)`): overwriting an existing
   file the user picked, a name without an extension, a locked Word form, and black-outs.
4. Anything in tests/ that asserts the wrong value, passes for the wrong reason, or would pass
   with the fix reverted, including tests/test_review_round1.py.

What I want: concrete bugs with file:line, the input that triggers them, and what goes wrong, ranked
by severity (P1 for rule breaks and data loss, P2 for functional defects, P3 for test quality). Run
`./.venv/bin/python -m pytest -q` (289 should pass; window tests need a display, scan tests need
tesseract, one DOCX test needs LibreOffice) and report anything that fails. Report only; do not
refactor, restyle or fix. Write the report to
/home/estevens/code/omaform/docs/reviews/REVIEW_RESULTS_2.md. Use made-up data only, like
"Alex Rivera" and 123-45-6789. No em dashes in your output.
