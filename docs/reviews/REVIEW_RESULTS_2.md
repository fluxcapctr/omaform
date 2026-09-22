Omaform round-two review, 2026-09-22, at commit `83c90c4`, including the round-one fixes in `04fe2d6`.

I followed the round-two prompt in `docs/reviews/REVIEW.md`, checked the earlier report and its fix table, and reviewed the implementation against README.md and docs/PLAN.md. This report lists new findings and incomplete fixes, rather than repeating resolved round-one cases. P1 denotes mandatory-rule violations or data loss, P2 functional defects, and P3 test quality. All reproduction data was synthetic. No application or test files were changed.

1. **[P1] The redaction staging filename can delete the original source.** `src/omaform/ui/app.py:1220-1222`.

   Open `original.redacting.pdf`, add a black-out, and Save As `original.pdf`. The requested destination differs from the source, so the save is allowed. `do_fill` then derives `original.redacting.pdf` as its intermediate redaction destination, overwrites the source with that intermediate, and moves it over the final output with `os.replace`. The original source pathname disappears. Reproduced by invoking the actual `Window.do_fill` with synthetic PDFs and a lightweight window double; `source.exists()` became false. The same deterministic staging name can overwrite an unrelated existing file. Use a uniquely created temporary file and validate all actual destinations, including intermediates, against the source before writing.

2. **[P1] Cropped-page redaction still leaves the requested text visible.** `src/omaform/redact.py:93-97`, `src/omaform/redact.py:218-231`. Incomplete round-one fix 3.

   `_display_size` and `find_text` use Poppler's cropped display space, but `_render` invokes `pdftoppm` without `-cropbox`, producing the MediaBox raster. Scaling that raster by the cropped dimensions does not account for the crop translation. Reproduced with a 400-by-400-point page, CropBox `[100,100,300,300]`, and `SECRET` drawn at Cairo position `(120,150)`. Text search finds it and redaction succeeds, but the saved image shows a black rectangle above the complete, still-readable word. I rendered and visually inspected that output; variants with all four rotations were also generated. Render the same effective crop used for region coordinates, including its origin. The current rotation regression test has no CropBox and therefore misses this mismatch.

3. **[P1] Pruning `/Fields` does not remove secrets retained by `/AcroForm/CO`.** `src/omaform/redact.py:187-192`, `src/omaform/redact.py:232-238`. Incomplete round-one fix 2.

   Extend the two-page parent-field fixture so the first page's secret-bearing child is also referenced from the form's calculation-order array, `/CO`. Redact page one while keeping page two. The fix removes the child from `/Kids`, but leaves `/CO` unchanged. After saving, `pdf.Root.AcroForm.CO[0].V` still returns `123-45-6789`. Reproduced against the actual writer. This is a reachable object, so normal removal of unreferenced objects cannot help. Remove references to retired fields from calculation order and other form-level structures, and check the saved object graph rather than only the remaining field tree.

4. **[P1] Ownership-changing revisions still collide with saved form memory.** `src/omaform/adapters/pdf.py:499-511`, `src/omaform/adapters/docx.py:602-607`; replay: `src/omaform/memory.py:195-226`. Incomplete round-one fix 6, plus the equivalent DOCX case.

   The PDF fingerprint hashes the direct page content streams but not referenced Form XObjects, images or their resources. It also hashes `label.best()` rather than the section and row context that determines ownership. Reproduced two PDFs with identical `/Fm0 Do` page streams, identical `Name` widgets, and different Form XObject headings: `Section 1. Employee Information` versus `Section 2. Employer Information`. Discovery recognizes the different sections, but fingerprints are equal. The fresh planner correctly leaves the employer form empty; replaying the employee form's memory fills it with `Alex Rivera`.

   DOCX fingerprints similarly exclude section context. Two documents with headings `APPLICANT INFORMATION` and `EMPLOYER INFORMATION`, each followed by the same Name blank, collide and reproduce the same wrong-party fill. Include referenced visual content and all matching-relevant context in fingerprints. Memory should also recheck ownership instead of treating an empty entry as permission to fill.

5. **[P1] The new prompt scrubber still transmits recoverable answers.** `src/omaform/llm.py:87-105`, `src/omaform/llm.py:120-124`. Incomplete round-one fix 4.

   Concrete reproduced cases:

   - A previously completed DOCX paragraph `Date of birth: 01/02/1990` followed by a Phone content control sends the date in that control's `above` label. Completed underscore fields no longer produce blanks, so their values never enter `known`; DOB has no shape filter. This also affects values absent from the current identity or still behind its locked vault.
   - With known SSN `123-45-6789`, label slots containing `123-45-` and `6789` retain both fragments. Each slot is scrubbed independently, so the full answer can be reconstructed from the prompt.
   - Known single-character answers are explicitly skipped. `scrub("Apartment: A", ["A"])` returns the complete answer unchanged.
   - A known composed Unicode value `José Rivera` does not remove the canonically equivalent decomposed spelling `Jose\u0301 Rivera` from a label.
   - `blank.id` is copied without scrubbing. An ID such as `p1:123-45-6789` leaks the known SSN even when the label itself is clean. PDF IDs include source field names.

   These were prompt-building tests only; no model was contacted. Regex escaping correctly handles literal regex metacharacters, but exact substring replacement and a few shape filters do not establish the promised value-free boundary. Use opaque prompt IDs and separate question text from existing answers; account for normalized and split representations before sending context.

6. **[P1] Model response prose is cached and displayed without sensitive-value filtering.** `src/omaform/llm.py:201-203`, `src/omaform/llm.py:298-305`; display: `src/omaform/ui/app.py:1035-1041`, `src/omaform/cli.py:232-237`.

   If the model echoes an answer from the context in `why` or `questions`, parsing merely truncates it. `read_form` persists that prose and `apply` passes it into entry notes and UI/CLI output. Reproduced parsing and remembering a response with `why="SSN 123-45-6789"` and `questions=["Is 123-45-6789 correct?"]`: the plaintext secret appears in `readings/<fingerprint>.json`. A cached reading is also loaded without sanitization. This becomes a persistent disclosure after a prompt leak, even if the prompt is subsequently fixed. Apply the sensitive-data policy to response prose and cache loading before storage or display, not only to outgoing labels.

7. **[P1] Sensitive checkbox answers bypass CLI masking.** `src/omaform/cli.py:197-202`.

   The `checked` branch precedes the sensitivity check and prints `entry.note`. Reproduced planning the I-9 with synthetic `citizenship=citizen`: `_show` prints `citizenship = ☑ U.S. citizen`, even though the new `Plan.sensitive_used` correctly includes `citizenship`. The field label plus the selected-state marker also reveals which sensitive answer was chosen, so masking only the note is insufficient. This violates the explicit rule against printing sensitive stored values. Apply sensitivity handling before formatting checkbox states and answer-specific labels, as well as before formatting text values.

8. **[P2] Accepting a sensitive suggestion still waits for a GTK dialog on the GTK thread.** `src/omaform/ui/app.py:1133-1139`; blocking path: `src/omaform/ui/app.py:552-598`. Remaining path from round-one finding 14.

   Open a scan or a form with a fuzzy SSN label, keep the vault locked with no usable keyring passphrase, and press Accept beside the suggested SSN. The base planner does not fetch a suggested value, but `_accept_suggestion` calls `build_profile(identity).get(...)` synchronously from the click callback. Its unlocker queues a passphrase dialog with `idle_add` and then waits up to 300 seconds on the same thread needed to show that dialog. Reproduced the callback using the real unlock path with the event wait intercepted: it queued `ask` and then requested a 300-second wait. No real five-minute freeze was induced. Move accepted-suggestion resolution through the asynchronous worker/unlock path too.

9. **[P2] Switching forms during a model request leaves the model button permanently busy.** `src/omaform/ui/app.py:338-351`, `src/omaform/ui/app.py:498-536`, `src/omaform/ui/app.py:1077-1079`. Regression in round-one fix 13.

   Start a model request for form A, then open form B before it finishes. Loading B does not reset `model_busy`. Both completion callbacks return early for A before clearing that flag, so the busy timer continues and the model button remains disabled, even after the worker has finished or failed. Reproduced calling the actual stale-completion handler against a window double for B: the old busy state remains. Associate busy state with a request generation and clear or detach it when its document is replaced, without letting an obsolete completion alter a newer request.

10. **[P2] A signature is inserted after the wrong text when a DOCX run contains several text nodes.** `src/omaform/adapters/docx.py:343-353`. Incomplete round-one fix 15.

    Use one `w:r` containing three `w:t` nodes: `Signature: `, `________`, and ` Date: ________`. Fill Date and insert a signature image. After removing the signature underscores, `_insert_run_at` reaches the end of the first text node and inserts after its entire parent run, although that run also contains the date. Reproduced saved run order: first the complete text `Signature:  Date: 09/22/2026`, then the drawing. The signature therefore lands after the date instead of on its own line. Boundaries between text nodes need run splitting just like offsets inside a text node. When splitting, preserve tabs, breaks and other children exactly once rather than cloning them indiscriminately.

11. **[P2] Saving a recalled clear forgets it on the following open.** `src/omaform/memory.py:109-112`, `src/omaform/memory.py:180-186`.

    Clear a normally matched Name field, save, reopen the original blank form, and save again without changing that field. The first replay correctly leaves it empty but changes the note to `left empty, as last time`. `remember` only retains entries whose note contains `cleared by you`, so the second save overwrites the identity's cleared list with an empty list. On the third open, Alex Rivera's name fills again. Reproduced the full remember/apply/remember/apply cycle. Track clear provenance explicitly or preserve recalled clears until the user intentionally replaces them; presentation strings should not determine whether a persisted decision survives.

12. **[P2] Signatures placed over native text widgets cannot be dragged.** `src/omaform/model.py:118-121`, `src/omaform/ui/preview.py:303-309`.

    A real signature location is often an AcroForm text widget, not `BlankKind.SIGNATURE`. The planner stores an image in its entry, but `Blank.movable` checks only synthetic native data or the blank's kind. Reproduced on the I-9 fixture with a generated signature: `p1:Signature of Employee#1` has an image, `kind="text"`, and `movable=False`. `_draggable_at` therefore refuses it, even though the PDF writer already supports its offset. Determine movability from the resolved image entry as well as the underlying widget kind. Existing drag tests exercise a synthetic date and a synthetic W-9 signature, so they do not cover this case.

13. **[P2] The vault writer accepts KDF settings that the new reader refuses.** `src/omaform/vault.py:185-195`, `src/omaform/vault.py:330-335`. Regression at the boundary of round-one fix 17.

    `Vault.create(..., kdf=...)` still accepts a custom `Kdf` directly and writes it without the new envelope validation. Reproduced creating a vault with an eight-byte salt, time cost 1, memory cost 32 KiB and parallelism 1. Creation succeeds and encrypts the synthetic value; after locking, unlocking with the correct passphrase raises `VaultError("key derivation salt has the wrong length")`. This is the supported custom-KDF API, not the normal calibrated CLI path, whose generated settings remain inside the bounds. Validate settings before creating a vault, and define compatibility for previously writable settings, so successful writes cannot produce files the same version refuses to reopen.

14. **[P3] The repeat-signing regression test signs the Date blank and can skip its second assertion.** `tests/test_review_round1.py:345-352`.

    The test finds the second signature target by checking whether its label contains `Signature`. After signing the fixture, the original signature underscore run is gone; the only matching blank is the adjacent Date line, now labelled `Signature:    Date`. Reproduced that exact discovered target. The test then signs this Date blank and checks only that `omaform2.png` exists, rather than checking replacement of the original signature and valid relationships. If no such label exists, `if sig2` silently skips the entire second write/assertion. Use a fixture with a persistent signature control, require that target to exist, and check the resulting placement and relationship resolution on both writes.

Validation and scope:

- `./.venv/bin/python -m pytest -q --tb=short`, with GTK/display and LibreOffice access: **289 passed, 17 warnings in 81.78 seconds**. No test failures. Warnings were the existing GI version/deprecation and Pillow deprecation warnings.
- Additional bounded reproductions exercised the actual writers, saved PDF object graphs, prompt builder, parser/cache, matcher, memory and KDF API. The source-deletion reproduction called the actual `do_fill` with a window double. The suggestion-unlock reproduction intercepted waiting and queued callbacks; the stale-model reproduction called the completion handler directly. They did not require live model calls, real secrets, or long UI hangs.
- The simple round-one cases now pass: source-preserving DOCX-to-PDF conversion, parent-child field pruning without other references, uncropped rotation redaction, exact known-value scrubbing, corrected entry metadata, direct-page fingerprint changes, sentence ownership vetoes, checkbox section vetoes, fuzzy-group suppression, per-identity choice replay, KDF envelope bounds, legacy-name migration and cyclic-parent rejection. The findings above identify the remaining cases or separate defects.
- Plan generation/document checks and moving memory/model resolution into the worker address the original stale-plan and normal-plan unlock paths. Memory precedes model application in both UI and CLI, and manual edits are applied afterward in the UI. Word preview cleanup is now wired to file changes and window close. I did not run a prolonged concurrency or crash-cleanup stress test.
- Page content, direct rendering, crop/rotation behavior, field graphs and calculation-order references were checked. This is not an exhaustive proof that every attachment, JavaScript payload, XFA packet, metadata dictionary or optional-content resource is sanitized. Unreferenced-object retention alone was not the cause of the reproduced disclosure.
- `_read_body` now bounds ZIP member bytes and rejects ordinary UTF-8 entity declarations. A small UTF-16 document containing an internal entity still bypassed its ASCII-byte declaration check and was expanded by minidom. I did not demonstrate resource exhaustion and have not presented this as a proven XML denial-of-service exploit. Large expansion bombs were not run. The normal calibrated vault settings and version-one migration tests pass; the custom-KDF mismatch above is narrower.
- Dragging off a page still leaves an offset on the original page, rather than transferring the blank to the destination page. I did not classify cross-page movement as a regression because the implementation does not establish it as a supported operation. Nonexistent moved IDs are ignored on restore. No production file, profile, vault, model service, keyring or notification delivery was used for reproduction.

---

## Status after fixes

Fixed in the commit after 83c90c4. Regressions in `tests/test_review_round2.py`, the rewritten test 16
in `tests/test_review_round1.py`, and `test_round2_window_findings` in `tests/test_ui_flow.py`.
Suite: 304 passed.

| # | Finding | Fix |
| --- | --- | --- |
| 1 | Redaction staging name can delete the source | Staged in a uniquely created temp file beside the output; `do_fill` refuses any destination (including the locked PDF) that resolves to the source |
| 2 | Cropped-page redaction misses | `pdftoppm -cropbox` in redaction and scans, so the raster is the same cropped, rotated page the coordinates describe; tested at all four rotations |
| 3 | `/CO` keeps retired fields | Calculation order filtered to live fields; an XFA packet is dropped |
| 4 | Fingerprints miss XObject content and ownership context | PDF fingerprint hashes referenced XObjects recursively and each blank's section, row, inside and field name; DOCX adds section, inside and field name; memory re-runs the ownership checks before filling |
| 5 | Scrubber leaks | Opaque question ids (`q1`...) replace blank ids; NFC normalisation; any run of three or more digits and dates cut; one-letter answers cut after a colon; in stream-read labels (Word, synthetic) anything after a colon is cut |
| 6 | Model prose cached and shown raw | Reasons and questions scrubbed on parse (with known values), on cache load, and again with profile values at apply |
| 7 | Sensitive ticks shown in the CLI | A ticked sensitive choice prints as "(a sensitive choice) = ***" |
| 8 | Accepting a sensitive suggestion blocks GTK | The value is fetched in a worker; applied only if the same document is still open |
| 9 | Model button stuck after switching files | Opening a file clears `model_busy` |
| 10 | Signature after the wrong text in multi-node runs | `_insert_run_at` splits at text-node boundaries by moving children, keeping tabs and breaks once and formatting on both halves |
| 11 | A recalled clear is forgotten on the next save | `Entry.cleared` flag set by the window and by memory; `remember` reads the flag, not the note |
| 12 | Signatures on text widgets not draggable | Any entry carrying an image is movable, whatever the widget kind |
| 13 | Vault writes settings it later refuses | `Kdf.check()` runs on create as well as on read |
| 14 | Repeat-signing test signs the Date blank | Rewritten with a picture content control that persists; asserts both writes and that every picture resolves |

Also: entity declarations are looked for in the decoded text, so a UTF-16 body cannot hide one.
Not changed: dragging a placed item onto another page keeps it on its own page (not a supported move).
