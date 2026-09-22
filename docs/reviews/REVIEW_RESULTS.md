Omaform review, 2026-09-22. Reviewed against `docs/reviews/REVIEW.md`, after reading README.md and docs/PLAN.md. Report only; no application or test code was changed.

The checkout advanced during this review from `39c7e32` to `130c05b` (dragging signatures and placed content). Locations below refer to `130c05b`; I inspected that diff and checked the findings against it. P1 means urgent, including violations of the six mandatory rules in the review prompt. P2 means a functional or availability defect. P3 means a test-quality defect. Runtime reproductions used synthetic data and temporary files, not personal documents or the real profile/vault. Findings explicitly identified as source inspection were not exercised as full interactive flows.

1. **[P1] Saving a locked DOCX to its own PDF basename corrupts the source.** `src/omaform/adapters/docx.py:526-539`.

   Open `application.docx`, enable Lock, and Save As `application.pdf`, or run the equivalent CLI fill with `--lock -o application.pdf`. Both the caller and line 500 compare the source with the requested PDF path and allow it. The writer subsequently changes its intermediate destination to `application.docx` and opens that source ZIP with mode `w` while also reading it. Reproduced: `BadZipFile` is raised and the source bytes have already changed. This happens before LibreOffice runs. Compute and validate every actual write destination before opening anything, and use a temporary directory for conversion intermediates.

2. **[P1] Redacted fields remain recoverable through a surviving field's parent tree.** `src/omaform/redact.py:130-141`.

   Make a two-page AcroForm with one nonterminal parent whose `/Kids` contain separate widgets on each page. Put `123-45-6789` only in the page-one child's `/V`, and redact all of page one. `_prune_form` retains the parent because page two still has a widget, but never removes its page-one child. After saving, `pdf.Root.AcroForm.Fields[0].Kids[0].V` still returns the secret. Reproduced with a newly generated PDF. Removing page `/Annots` and replacing page content is insufficient when another object still reaches the removed fields. Prune the entire field tree, including obsolete child references and values.

3. **[P1] Text redaction misses its target on rotated pages.** `src/omaform/redact.py:78-85`, `src/omaform/redact.py:178-195`.

   Poppler's text boxes describe the rotated display, but `find_text` only flips their Y axis. `apply` then treats the result as an unrotated PDF-space rectangle and rotates it again. Reproduced with a 200-by-300-point PDF containing `SECRET`, rotated 90 degrees: `find_text` returns a region, `apply` reports success, and the saved image still visibly contains the complete word. I rendered and inspected the saved output. `_picture_page` also stretches the rotated raster into the old unswapped MediaBox after removing `/Rotate`. Convert between display and PDF coordinates consistently, including page origins, and preserve the displayed dimensions when rebuilding a rotated page.

4. **[P1] A previously filled document sends profile values to the model as label context.** `src/omaform/llm.py:71-74`; producer: `src/omaform/adapters/docx.py:113-127`.

   Fill only the Email address line of the test DOCX with `alex@example.com`, save it, reopen it, and ask a model to read the remaining fields. The following Phone content control has `label.above == "Email address: alex@example.com"`. `build_prompt` includes that value verbatim. Reproduced by building the prompt only; no model was called. The same mechanism can carry sensitive answers from preceding paragraphs. Excluding `Profile` from the prompt API does not guarantee that extracted document context is value-free. Separate questions from existing answers, or decline to send context whose contents cannot meet that guarantee. The current prompt privacy test uses only an unfilled form.

5. **[P1] Model corrections leave stale key metadata and expose sensitive values.** `src/omaform/llm.py:339-348`; consumers: `src/omaform/cli.py:198-201`, `src/omaform/ui/app.py:815-825`, `src/omaform/memory.py:87-91`.

   Apply a valid reading that changes an entry matched as `full_name` to `ssn`, with a stored synthetic SSN. `apply` replaces `entry.value` but leaves `entry.match.key` as `full_name` and does not update `sensitive_used`. Reproduced output from `_show`: `full_name = 123-45-6789`, without masking. The UI likewise treats it as an ordinary editable value, and memory records the wrong key. Applying a reading to a previously unmatched entry also leaves `match=None`, which makes `_show` raise `AttributeError` when formatting its key. Update the resolved key, image/text state, sensitivity and suggestion metadata together whenever a reading changes an entry. Memory's value restoration has the same stale-metadata problem.

6. **[P1] PDF fingerprints let unrelated or revised forms reuse mappings and signatures.** `src/omaform/adapters/pdf.py:544-546`.

   The digest includes only native widget names and their page numbers. Every flat PDF and scan has an empty `names` list, so all receive `pdf-acro-e3b0c44298fc1c14`. They share remembered placements, offsets and model readings. AcroForms with identical field names also collide when their labels or ownership change. Reproduced with two PDFs containing the same `f1` widget but tooltips `Name` and `Employer name`: their fingerprints and blank IDs are identical. The fresh matcher correctly leaves the employer form empty, but applying the first form's memory fills it with Alex Rivera's name. Include the relevant structure, labels, page geometry and revision information in the fingerprint; flat documents need their own content-derived fingerprint.

7. **[P1] The sentence-level ownership veto is unreachable.** `src/omaform/matching.py:301-306`.

   The `if alias_tokens` block is indented beneath an unconditional `return True`, so `_sentence_with` is never consulted. Reproduced with an authoritative label `Enter the full legal name of the employer`: the planner fills `full_name` with `Alex Rivera`. The ownership word is beyond the leading-token window and after the alias, exactly the case the sentence check was meant to catch. Restore the sentence check in the intended authoritative-label path, with regressions for both employer-owned labels and employee labels whose later sentences merely mention a preparer.

8. **[P1] Checkbox matching bypasses all ownership checks.** `src/omaform/plan.py:288-308`.

   `_tick_choices` checks kind and readonly status, then matches the choice label directly. It never applies the text matcher's section, row or ownership vetoes. Reproduced with a checkbox labelled `Single or Married filing separately` under `Office use only` and a profile containing `filing_status=single`: the result is `checked`. Citizenship and tax-classification choices follow this same path. Apply ownership exclusions before consulting any stored choice, including exclusions carried by the heading and row.

9. **[P1] Grouped fields write fuzzy matches automatically.** `src/omaform/plan.py:207-220`.

   The group path resolves and distributes a match before reaching the exact-match/scanned-label guard used for single fields. Reproduced with three grouped blanks labelled `Social securty number` and synthetic SSN `123-45-6789`: all three matches have `exact=False`, but their values are `123`, `45`, `6789`. This violates the rule that fuzzy matches are suggestions only. Apply confidence and per-member safety checks before resolving group values. A group can also contain readonly members because the initial grouping code does not exclude them and the group write path does not recheck them.

10. **[P1] DOCX previews leave plaintext copies behind after the window closes.** `src/omaform/ui/app.py:369-376`, `src/omaform/ui/app.py:389-393`.

    Source inspection: opening a previously completed DOCX renders the whole file to a directory created with `tempfile.mkdtemp`. `_preview_tmp` is only created and returned; there is no cleanup on document replacement or window/application shutdown. A DOCX containing a signature, SSN or other sensitive answer therefore leaves a plaintext PDF under `/tmp/omaform-preview-*` after normal exit. The directory's restrictive permissions do not satisfy the stated rule against leftover sensitive temporary files. Own the temporary directory for the window lifetime and remove its contents on normal close, coordinating cleanup with conversion workers.

11. **[P2] Remembered tax-number mappings override a new preference and break split fields.** `src/omaform/memory.py:164-174`.

    Save a W-9 using SSN, then reopen it with both SSN and EIN available and prefer EIN. The planner correctly rules out SSN, but `memory.apply` sees those entries as empty and repopulates them using the entire `profile.get("ssn")`. Reproduced: all three SSN boxes contain `123-45-6789`, while the EIN boxes also contain `12` and `3456789`. Memory needs to respect current exclusions and resolve through the same grouping and alternative-selection rules as the planner. The same unconditional restoration can undo a model's decision to leave an entry empty.

12. **[P2] Remembered checkbox answers follow the form across identities.** `src/omaform/memory.py:159-162`; storage: `src/omaform/memory.py:85-86`.

    `checked` is one global list per form. `saved_by` is not used when replaying it. Save a W-4 as an identity whose filing status is single, then open it as another identity whose status is married jointly. The planner selects the new identity's box; memory also reselects the old single box because it is currently unfilled. Reproduced the stale tick with a synthetic checkbox and two profiles. This also affects changed citizenship and tax classifications. Remember the choice key and resolve its current answer, and scope genuinely manual checkbox overrides to the appropriate identity.

13. **[P2] Old asynchronous results can replace the current document's or identity's plan.** `src/omaform/ui/app.py:580-604`, `src/omaform/ui/app.py:339-342`.

    Source inspection: start a fill that waits on vault unlock, then change the identity or open another document. A second worker can finish first; the old worker later calls `_plan_ready`, which unconditionally assigns its result. There is no document, identity or request-generation check. The worker also reads mutable `self.document` instead of capturing it when scheduled. A pending model read has the same issue: `_model_done` installs its reading on whichever form is now open. With overlapping blank IDs, saving the current form can write stale values or apply another form's reading. Capture the request inputs and discard obsolete completions before applying any state.

14. **[P2] Applying memory or a model reading can block GTK for five minutes.** `src/omaform/ui/app.py:592-602`, `src/omaform/ui/app.py:545-569`.

    Source inspection: let the base matcher produce no sensitive fill, but let memory or a cached reading request a vault key for an unmatched blank. `_plan_ready` runs on the GTK main loop and calls `memory.apply`/`llm.apply` there. Their `profile.get` invokes `_unlock_blocking`, which schedules its passphrase dialog with `GLib.idle_add` and waits for up to 300 seconds. The main loop cannot present the dialog while it is blocked waiting for that dialog's answer. Perform all potentially unlocking resolution in a worker, or make unlock asynchronous through the entire callback chain.

15. **[P2] Filling multiple DOCX blanks in one paragraph corrupts later labels and answers.** `src/omaform/adapters/docx.py:512-520`; mutation: `src/omaform/adapters/docx.py:303-322`.

    Discovery records offsets in the original paragraph, but writes are applied in mapping order to an already modified paragraph. Reproduced with `Name: ________ Email: ________`, filling in document order with `Alex Rivera` and `alex@example.com`. Saved text is `Name: Alex Rivera Emaialex@example.com___`. Signature replacement can likewise shift offsets relative to text fills in the same paragraph. Group edits by original paragraph and apply them from right to left, including image replacements, or retain stable node/span targets.

16. **[P2] Valid relationship XML can silently lose an inserted DOCX signature.** `src/omaform/adapters/docx.py:404-416`.

    Relationship and content-type changes are literal string replacements of `</Relationships>` and `</Types>`. A legal empty relationship part serialized as `<Relationships xmlns="..."/>` has no matching closing string. Reproduced: the saved document refers to `rIdOmaform1` and contains the PNG, but its relationship part contains no such ID, so the picture cannot be resolved. Namespace-prefixed roots have the same problem. IDs and media names also restart at one without checking existing parts when signing a previously signed document. Edit these XML documents structurally and allocate unused IDs and names.

17. **[P2] Unauthenticated KDF costs can cause excessive work before tampering is detected.** `src/omaform/vault.py:140-151`, `src/omaform/vault.py:168-174`.

    `Kdf.from_json` converts the file's costs to integers without resource bounds, and unlock invokes Argon2 before verifying AEAD tags. A modified envelope with an enormous time cost and an otherwise usable memory cost can occupy the process indefinitely; oversized memory costs can cause severe allocation pressure. Safely verified by replacing only the native hash function with a spy: costs of `2147483648` reach it unchanged. I did not execute that expensive derivation. Calibration's `MAX_TIME_COST` does not constrain costs read from disk. Validate accepted memory, parallelism, salt length and time limits before running Argon2. This is a denial-of-service issue, not evidence of successful decryption or an authentication bypass.

18. **[P2] Legacy vault secrets disappear from lazy discovery before migration can run.** `src/omaform/vault.py:265-270`; callers: `src/omaform/cli.py:143-158`, `src/omaform/ui/app.py:514-522`.

    A version-one vault stores clear key names such as `ssn`, but `keys_for("me")` only accepts names prefixed with `me/`. Reproduced with the existing version-one test helper: `key_names()` returns `["ssn"]`, while `keys_for("me")` returns `[]`. Both normal fill paths therefore see no secrets and attach no lazy unlocker, so migration inside `unlock` never runs. Directly unlocking still migrates correctly; the existing test covers only that direct path. Normalize legacy names for inspection while continuing to authenticate against their original representation during unlock.

19. **[P2] Cyclic PDF field parents hang page assembly and redaction.** `src/omaform/pages.py:104-105`, `src/omaform/redact.py:135-136`.

    Both walks follow `/Parent` until it disappears, without tracking visited objects or enforcing a depth limit. A malformed widget whose `/Parent` points to itself never terminates. Reproduced `_prune_form` in a subprocess with such an in-memory PDF and terminated it after a one-second timeout. The identical page-assembly walk has the same defect. In the UI these save operations run synchronously, so this also freezes the window. Reject cycles and excessive ancestry depth with a controlled document error.

20. **[P3] Two tests do not verify the behavior their names/comments promise.** `tests/test_choices_memory.py:135-137`, `tests/test_docx.py:128-134`.

    The cross-identity memory assertion ends in `or True`, making it true for every returned value. It cannot detect incorrect cross-identity replay. `test_fingerprint_is_structural` creates a filled output but never discovers or compares that output; it only checks that the original fingerprint starts with `docx-` and has a certain length. A constant string with that prefix and length passes. Assert the intended identity-specific outcome explicitly and compare fingerprints for both equivalent and meaningfully changed form structures.

Validation and review limits:

- Initial sandbox run of `./.venv/bin/python -m pytest -q`: **255 passed, 10 failed**. Nine GTK flow failures were caused by inaccessible display initialization; the LibreOffice conversion also failed in the sandbox. These were not classified as product regressions.
- Rerun with display/process access: **266 passed, 17 warnings**, 78.12 seconds. The checkout changed during the review. Final collection at `130c05b` contains **267 tests**; both tests added by that commit were subsequently run explicitly and **2 passed**. Thus there is no remaining observed test failure, but the final 267-test checkout was not run as a single full-suite invocation. Warnings concern GI version declarations and deprecated GI/Pillow interfaces.
- Additional isolated reproductions confirmed the source corruption, retained redacted field, visibly missed rotated redaction, prompt-value exposure, unmasked model correction, colliding fingerprints with an employer-field fill, unreachable veto, checkbox ownership failure, fuzzy group fill, memory preference/identity errors, DOCX offset corruption, missing image relationship, legacy inspection gap and cyclic-parent hang. The KDF bound check used a spy rather than a resource-exhaustion attempt.
- Vault inspection and the passing vault tests support fresh body/wrap nonces, AAD binding of version/KDF/key list, rejection of wrong passphrases and altered ciphertext, and successful direct version-one migration. This is not a cryptographic audit. The clear key-name list is authenticated only upon unlock; inspecting it while locked cannot itself verify that it has not been edited.
- I found no confirmed external-entity exploit, scan pixel-loop nontermination, or shell-metacharacter injection in this pass. That is not proof of robustness against arbitrary input. DOCX discovery reads the entire ZIP member and parses it without application-level size/depth limits; malformed missing-body XML also raises an uncaught `IndexError` in `_Walk.run` (reproduced), which the CLI's error boundary does not handle. Scan/redaction rendering and OCR subprocesses lack timeouts. Large ZIP, XML, PDF and image fuzzing, and external tool resource exhaustion, were not performed.
- PDF image soft masks, comb appearances, flattening, repeated writes from one discovery, basic redaction, ordinary matcher behavior and the existing UI flows were covered by source inspection and the existing passing tests. The UI race, main-loop unlock wait and preview cleanup findings are source-inspection findings, not claims of completed interactive stress tests. No actual model, email, notification delivery, or real keyring write was used for reproduction.

---

## Status after fixes

Fixed in the commit after 130c05b. Each finding has a regression test in `tests/test_review_round1.py`
unless noted. Suite: 289 passed.

| # | Finding | Fix |
| --- | --- | --- |
| 1 | Locked DOCX to its own PDF name corrupts the source | Every destination is computed and checked before anything opens for writing; the intermediate .docx lives in a temporary folder |
| 2 | Redacted child field survives under a kept parent | `_prune_form` prunes the whole field tree node by node, keeping only kept widgets and their ancestors |
| 3 | Rotated-page redaction misses | Regions, the render and Poppler text share the page as shown; the rebuilt page takes the shown size with no rotation or crop |
| 4 | Filled answers reach the prompt as label context | `llm.scrub` removes every value already in the document, every value in hand, and anything shaped like an SSN, EIN, email or phone |
| 5 | Model corrections leave stale keys, unmasked | `Entry.resolve_to` updates key, value and offers together; `sensitive_used` is derived from what is filled; the CLI masks anything of unknown origin |
| 6 | Fingerprints collide | Fingerprint covers page count, sizes, rotation, content stream digests, field names, and every blank's id, kind, place and label |
| 7 | Sentence veto unreachable | Restored inside the label loop, for authoritative labels only |
| 8 | Checkboxes bypass ownership | `_tick_choices` skips boxes in another party's section or block |
| 9 | Grouped fields write fuzzy matches | A split run is written only on an exact match with no read-only or scanned member; otherwise it dissolves |
| 10 | DOCX previews left in /tmp | The render folder is per file and deleted on opening another file and on window close |
| 11 | Memory overrides a new SSN/EIN preference | Memory skips keys the planner ruled out and spreads split numbers across their run; memory runs before the model |
| 12 | Checkbox memory follows the form across identities | Only hand ticks are remembered, per identity; ticks from stored answers are re-derived |
| 13 | Stale async results replace the current plan | Plan requests carry a generation and the document; older answers are dropped; model results are tied to their form. No display test |
| 14 | Unlock can block GTK for five minutes | Memory and the model are applied in the plan worker, off the main loop. No display test |
| 15 | Two DOCX blanks in one paragraph corrupt | Span edits are applied right to left per paragraph; a picture is inserted at its offset, not the paragraph end |
| 16 | Empty relationships part loses the signature | Relationships and content types are edited as XML; ids and media names are allocated unused |
| 17 | Unbounded KDF costs from the file | `Kdf.from_json` bounds time, memory, parallelism and salt length before Argon2 runs |
| 18 | Legacy vault keys invisible before unlock | Version 1 key names are reported under the legacy identity, so a fill finds them and unlock migrates |
| 19 | Cyclic field parents hang | `redact.field_root` refuses cycles and depth over 64; pages assembly uses it |
| 20 | Two tautological tests | The cross-identity assertion checks cleared blanks and hand ticks; the DOCX fingerprint test compares a copy and a changed form |

Also from the limits section: a Word file with no body, a DOCTYPE or entity declaration, invalid XML or a body over 64 MB is refused with a clear error; deep nesting is caught; tesseract and pdftoppm have timeouts; the CLI reports pikepdf and zipfile errors instead of a traceback.
