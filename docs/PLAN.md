# Omaform

A form filler for Linux that remembers who you are.

Open a document, press one key, and every blank that asks for your name, address,
phone, email, SSN or signature is already filled. Works on proper PDF forms, on flat
PDFs with no form fields at all, on scans, and on Word documents. Nothing leaves the
machine.

Name is a placeholder and is one string change away from anything else. It pairs with
Compy: `compy` for images, `omaform` for paperwork.

---

## 1. Why this exists

Filling out a W-9, an I-9 or a W-4 means typing the same twelve facts about yourself
again, drawing a signature with a trackpad, and doing it again next month for a
different client. Existing Linux options each fail on one axis:

| Tool | Fails at |
| --- | --- |
| Evince, Papers, Zathura | fill AcroForms at best, no saved identity, no signature, nothing for flat PDFs |
| Okular | can annotate and sign, but no profile, and annotations are not text in a box |
| LibreOffice Draw | mangles layout, one document at a time, no profile |
| Xournal++ | good manual stamping, zero automation |
| Browser viewers | no saved identity beyond Chrome autofill, no signature placement |

Nothing on Linux does the thing that matters: know your details, find the blanks, and
put the right value in each one.

### What this is not trying to beat

Evince is a good viewer, and Omaform renders through the same library, poppler, so it
starts out equally fast and accurate on screen. But Evince has years of polish in text
selection, search, annotation and accessibility that Omaform will not match for a long
time, and it should not pretend otherwise. Taking over the `application/pdf` handler is
therefore offered on first run, never silently assumed, and the README says plainly that
Evince remains the better choice for reading long documents.

The real benchmark is not Evince. It is Adobe Acrobat's Fill and Sign, and the Preview
workflow on macOS. Those are the tools people actually reach for to fill a form, and
neither of them maps a saved identity onto an arbitrary form's labels the way the
template plus model pipeline here is designed to.

---

## 2. What it does

### Tier A: real form fields
Most official forms, the IRS W-9, W-4 and the USCIS I-9 included, ship as PDFs with
AcroForm text fields carrying names like `f1_04[0]` or `Name_First`. Read the field
tree, match each field to a profile key, write the values, save. This alone covers the
three forms that prompted the project.

### Tier B: flat PDFs with no fields
Most PDFs people email you. The blanks are still findable without any OCR, because the
page's own drawing instructions describe them:

- runs of underscore glyphs, `______`, are a blank whose box is the run's bounding box
- thin horizontal filled rectangles or stroked lines with text ending to their left
- empty cells in a ruled table, found from the intersecting rule lines
- small squares next to short text are checkboxes

For each candidate blank, take the nearest text to its left, then above, as the label.
Match the label to a profile key. Where the guess is wrong or missing, click anywhere
on the page to place a box by hand.

### Tier C: scans
No text layer, so run tesseract, which is already installed, to get words with bounding
boxes. Same label-to-left, label-above logic on the OCR output. Lower confidence, so
these always show for review before saving.

### Templates, the feature that compounds

*Done, in the form of memory: a form is remembered when it is saved, with nothing to
press, and fills the same way next time. Structure only, never values.*
Fingerprint each document: for Tier A, the sorted list of field names; for Tier B and C,
a hash of the normalized page text. Store the resulting field-to-profile-key map, and
any boxes placed by hand, under that fingerprint. The next copy of the same form fills
itself completely, with no guessing and no clicking.

Templates hold coordinates and key names only, never values, so they are safe to share.
Ship a starter pack for W-9, W-4, I-9, ACH authorization and a standard NDA cover page,
and take community templates as pull requests. Over time the repo becomes the reason
people install it.

### Signature
Draw it once with the mouse, trackpad or a tablet, or import a PNG or a photo of a
signature on paper, with background removal. Stored as a transparent PNG. Place it by
click and drag, or automatically onto any field whose label matches signature, sign
here, or applicant signature. Date fields next to a signature autofill to today.

---

## 3. Formats, and why the core is format-agnostic

Word documents are a first-class target, not an afterthought, and that decision has to
land before any code is written. Retrofitting a second format into a core shaped around
PDF is painful; starting with an adapter boundary costs nothing.

### The internal model

A document is a list of **blanks**. Each blank carries:

- an id, stable across reopens of the same file
- label context: the text before it, the text above it, the section heading it sits under
- a kind: text, checkbox, radio group, signature, date
- constraints: available width or character count, which usefully bounds the value
- a writer that knows how to put a value back into that blank's native format

An adapter implements exactly two operations, `discover` and `write`. Everything of
value sits above that boundary and is shared across every format: the profile, the
vault, label matching, the LLM mapping layer, the template system, the review UI. Adding
a format means writing one adapter, not touching any of the interesting logic.

### DOCX

Easier than PDF in the part that actually matters. A `.docx` is a zip of XML, and the
body is a text stream in reading order, so the label for a blank is literally the text
preceding it. There is no coordinate math at all, no rule-line inference, no nearest-box
search. Four flavors of Word form, in ascending order of how common they are:

1. **Content controls**, `w:sdt` elements with a tag or alias like "Full Name". The
   semantic equivalent of AcroForm fields, and just as easy.
2. **Legacy form fields**, `FORMTEXT` in `w:fldChar` runs, from Word's old Developer
   tab. Same idea in older markup.
3. **Table cells**, a label in one cell and an empty cell beside or below it. More
   reliable than the PDF equivalent, because OOXML states the table structure outright
   instead of leaving it to be inferred from intersecting rule lines.
4. **Underscore runs in body text**, `Name: ________`. Replace the run with the value,
   preserving the character formatting around it. This is the overwhelming majority of
   the random forms people email you.

Harder than PDF in exactly one way: there is no fixed layout, so there is nothing to
render. Faithful `.docx` rendering is a LibreOffice-sized project and will not be
attempted. Instead, two view modes:

- a **field list view**, labels and values laid out as a form, which is a better
  interface for filling than a page view anyway
- a **preview render**, produced by `soffice --headless --convert-to pdf` and displayed
  through the poppler view that already exists for PDFs

The preview is read-only and disposable. Edits always go to the `.docx` itself, never to
the converted artifact. Signatures are inserted as an inline image at the placeholder
run, sized to the width the underscore run implies.

Output is a filled `.docx`, which preserves editability and is usually what the sender
wants, or a PDF export through the same LibreOffice path. The original is never
overwritten by default.

### Other formats, in priority order

| Format | Approach | Priority |
| --- | --- | --- |
| `.odt` | also zip plus XML, same four flavors, `python-odfpy` | cheap once DOCX works |
| photos of forms, `.jpg` `.png` | straight into the tesseract path, exports as PDF | worth having, people photograph paper forms |
| `.xlsx` | `openpyxl`, blanks are empty cells beside label cells | easy, rarely needed |
| `.doc`, `.rtf` | convert to DOCX with `soffice`, fill, convert back, warn about fidelity loss | low |
| Google Docs, web forms, anything in a browser | not a file parsing problem at all: the global quick fill keybind with `wtype` handles these, and handles them well | covered in Phase 9 |

## 4. The LLM layer

Underscore runs and nearby-label heuristics get you a long way on ruled government
forms. They fall over on the random stuff: a vendor onboarding sheet laid out in three
columns, a contract with signature blocks buried in prose, a form where the label is
"Print name of authorized representative" and the profile key is `full_name`. That gap
is a language problem, not a geometry problem, and a language model closes it.

### The split that makes this work

**Geometry comes from the PDF. Semantics come from the model. Never the other way
around.**

The content stream and the OCR output already know exactly where every blank, rule and
word sits, to the pixel. Language models are unreliable at coordinates and will happily
invent a plausible bounding box. So the model is never asked where anything is. It is
handed a list of already-located blanks with their surrounding text and asked only one
question: which of my profile keys belongs in each one.

Input to the model, per blank: an id, the text to its left, the text above it, the
section heading it falls under, and the box dimensions, which usefully imply the
expected content, since a 40 point wide box is not going to hold a street address.

### Your data does not go to the model

The model receives **label text and profile key names**. It never receives a single
profile value. It is told that `ssn`, `full_name` and `bank_routing` are available keys;
it is not told what they contain. The mapping comes back, and the values are inserted
locally afterwards.

So the worst case for a cloud backend is that a third party sees the text of a blank
form that was very likely published on irs.gov anyway. It never sees your Social
Security number, because there is no point in the pipeline where it would need to.

### Two backends

**Local, the default.** Ollama is already installed with `qwen3.6:35b-a3b`,
`gpt-oss:20b` and `qwen3-coder:30b` pulled. Label-to-key mapping with constrained JSON
output is well within reach of a 20B model, and this is a localhost HTTP call, so the
no-network-egress guarantee stays intact. Zero cost, zero latency concerns, works on a
plane.

**Cloud, opt-in.** A Claude API backend for forms the local model gets wrong, off by
default, in a separate module, with the README stating exactly which bytes leave the
machine. Chosen per document, not globally, so you can throw one confusing form at it
without changing the default posture.

The backend is an interface with three implementations from the start: heuristics only,
local model, cloud model. Each returns the same mapping structure, so they are
comparable and testable against the same fixtures.

### It runs once per form, not once per fill

The mapping is cached as a template keyed on the document fingerprint. The model runs
the first time you see a given form and never again, including on every future copy of
that same form from the same client. That makes inference cost and latency a non-issue,
and it means the app still works fully offline on anything it has seen before.

### What the model handles beyond simple mapping

- **Which name is which.** A form with "Name", "Business name" and "Name as shown on
  your income tax return" needs three different profile keys, and only prose
  understanding gets that right.
- **Conditional logic.** "Complete Part III only if you answered Yes to question 4."
  The model reads the instruction and marks the dependent blanks as conditional so they
  are skipped or flagged rather than filled wrongly.
- **Checkbox and radio semantics.** Which option corresponds to which answer, including
  mutually exclusive groups, which is exactly where I-9 style forms get fiddly.
- **Cross-references.** "Enter the amount from line 7." Not fillable from a profile at
  all, so the right behavior is to recognize it and leave it alone rather than guess.
- **Derived values.** Today's date, a state abbreviation where the box is two
  characters wide, a phone number reformatted to match the form's own example.

### Review before trust

Model-proposed fills are highlighted differently from template-confirmed ones and shown
for review before the first save of a new form. Confirm once and the mapping is promoted
to a trusted template that fills silently thereafter. Vault-tier fields keep their
confirm step regardless of how confident the model is.

## 5. Storing your information

Two tiers, because an address and a Social Security number do not deserve the same
treatment.

### Tier 1, profile: plaintext JSON, mode 0600
`~/.local/share/omaform/profile.json`

Name, address, phone, email, employer, job title, EIN, website. Annoying to retype,
not damaging to leak. Plain JSON so it is greppable, diffable and editable in a text
editor, which matters when the app has a bug.

### Tier 2, vault: encrypted, never plaintext on disk
`~/.local/share/omaform/vault.age`

SSN, bank account and routing numbers, driver's license, passport number, date of
birth, and the signature image. Encrypted with XChaCha20-Poly1305 under a key derived
from a master passphrase with Argon2id. Salt and parameters stored beside the
ciphertext. The derived key lives in memory only, is `mlock`ed so it cannot be paged
out, and is zeroed on lock. Auto-lock after 15 minutes idle, and immediately on session
lock, which is detectable over the logind D-Bus `Lock` signal.

### Why encrypt at all, given the disk is already encrypted

This machine's root and home are on LUKS, and the swapfile lives inside that encrypted
volume, so a powered-off stolen drive already yields nothing. Full-disk encryption is
doing most of the work here. The vault tier exists for the four threats FDE does not
cover:

1. **Anything running in your session.** A malicious npm dependency, a browser
   extension, a curl-pipe-bash install script: all of it runs as you and can read
   `~/.local/share`. FDE is unlocked and irrelevant at that point.
2. **Backups.** The restic backups run to an external SanDisk every six hours. A
   plaintext SSN in home means an SSN on a drive that lives outside the encrypted
   machine. The vault file stays ciphertext inside the backup.
3. **Accidents.** Screen sharing, a misdirected `git add`, a pasted config, a support
   log. Encrypted at rest means a mistake exposes ciphertext.
4. **Other people's machines.** This ships publicly. Plenty of users will not have
   FDE, some will sync home to Dropbox, and a few will run it on a shared box. The
   default has to be safe for them, not just for the developer.

### Passphrase source, user's choice

- **Default: typed passphrase**, remembered for 15 minutes. Strongest. One prompt per
  session, in practice.
- **Convenient: gnome-keyring**, which is already running here as
  `org.freedesktop.secrets`. The passphrase is stored via libsecret and unlocks with
  the login keyring, so there is no prompt at all. Weaker in exactly the way Chrome's
  saved passwords are weaker: any process in your session can ask the keyring for it.
  Offered with that tradeoff stated plainly in the UI, not buried.
- **Later: 1Password CLI** as a vault backend, since `op` integration is already an
  Omarchy install option. Pluggable backend interface from day one so this is additive.

### Handling rules for sensitive fields

- never filled silently: a sensitive value shows masked, with one confirm, unless you
  explicitly opt a specific field into silent autofill
- never written to logs: redaction happens in the logging layer, so a value cannot
  reach a log file even from a stack trace
- never put on the clipboard: typed directly with `wtype`, because clipboard managers
  keep history and Omarchy runs one
- no crash reporter, no telemetry, no update check, no cloud OCR. **Zero network code
  in the binary.** Auditable with `grep`, and stated as a guarantee in the README
- filled output PDFs do contain the values in plaintext, unavoidably. So: strip
  document metadata on export, default the save path next to the source file, warn once
  the first time a document containing a vault field is exported, and keep no thumbnail
  cache of filled documents by default

---

## 6. How it lives in Omarchy

Omarchy has no dock. Five integration points, all of them standard, none of them a
fork of anything:

**1. The launcher.** A `omaform.desktop` file in `/usr/share/applications` makes it
appear in the app launcher, searchable by name, and in Nautilus's Open With menu.
`omarchy-refresh-applications` picks it up on install. This is how Compy already works.

**2. Default PDF handler.** Register `MimeType=application/pdf` and offer, on first
run, to `xdg-mime default omaform.desktop application/pdf`. Currently
`org.gnome.Evince.desktop` holds it. Once set, a PDF opened from anywhere, a file
manager, a chat app, a browser download, lands in Omaform already filled. This is the
single highest-leverage integration and it is one command.

**3. A Hyprland keybind** for the quick fill overlay, using the existing
`omarchy-launch-or-focus omaform` pattern so a second press focuses the open window
instead of starting a new one. Suggested in the README as an opt-in snippet for
`~/.config/hypr/bindings.conf`, never written to the user's config by the installer.

**4. An Omarchy menu plugin.** Omarchy already has a menu plugin system and helpers
like `omarchy-menu-file`. Ship a plugin exposing "Fill a PDF", which picks a file from
`~/Downloads` and `~/Documents` and opens it filled. Reachable from the system menu
without touching the launcher.

**5. Global quick fill, the sleeper feature.** A keybind opens a small picker listing
your profile fields. Choose one and it types the value into whatever window has focus,
using `wtype`, which is installed. This works in Gmail, in Google Forms, in a web
checkout, in a native app, anywhere. Roughly fifty lines of code, and likely more
useful day to day than the PDF app itself. Sensitive fields require the vault to be
unlocked and show a confirm.

No bar widget. There is no state worth showing on the bar, and a widget would be
clutter.

---

## 7. Gmail and the browser

Gmail's attachment preview is Google's own JavaScript canvas in the browser. No native
Linux application can reach into it. Two honest paths, shipped in this order:

**Path 1, Downloads watcher.** An inotify watch on `~/Downloads`. A new PDF fires a
notification: "Fill this form?" Click it and Omaform opens with the blanks already
filled. Download from Gmail, save, drag the result back into a reply. Two clicks, no
browser code, works in every browser including the Omarchy web apps. Cheap and robust.

**Path 2, browser extension.** A Firefox and Chrome extension that adds a Fill button
to Gmail's attachment chips, hands the file to the native app over a native messaging
host, and offers to attach the finished PDF back to the open reply. This is the
genuinely seamless version, worth roughly a weekend, and it is deliberately last
because it is the piece most likely to break when Gmail's markup changes.

---

## 8. Stack

Python 3 with PyGObject, GTK4 and libadwaita. Every dependency is in the Arch official
repositories, already installed, or both:

| Job | Library | Status |
| --- | --- | --- |
| Render pages | `poppler-glib` via PyGObject | installed |
| Read and write PDF structure, AcroForms | `python-pikepdf` (qpdf) | in extra |
| Text overlay and flattening | `python-reportlab` | in extra |
| OCR for scans | `tesseract` | installed |
| Encryption | `python-cryptography`, `python-argon2-cffi` | in extra |
| UI | `gtk4`, `libadwaita`, `python-gobject` | installed |
| Typing into other windows | `wtype` | installed |
| Label-to-key mapping, local | `ollama` with `qwen3.6:35b-a3b` or `gpt-oss:20b` | installed, models pulled |
| Label-to-key mapping, cloud | Claude API, opt-in module | optional |
| Read and write DOCX | `python-docx` plus `python-lxml` for `w:sdt` | in extra |
| Read and write ODT | `python-odfpy` | in extra |
| Spreadsheet forms | `python-openpyxl` | in extra |
| DOCX preview render, legacy conversion | `libreoffice-fresh` headless | installed |

Python, not Rust, despite Compy being Rust. Blank detection is a long iterative loop of
heuristics against real-world messy PDFs, and that loop is much faster in Python. The
PDF library ecosystem is also better there. A ten page form will not be slow. Revisit
only if profiling says otherwise.

Architecture: a `omaform-core` library with no UI imports at all, holding the profile
store, vault, field detection, matching and writing. A `omaform` CLI on top of it, so
`omaform fill w9.pdf -o w9-filled.pdf` works headless and everything is testable without
a display. The GTK app is a third consumer of the same core. This keeps the interesting
logic under unit test and makes a future Rust port a rewrite of the UI only.

---

## 9. Build order

Each phase ends with something usable and gets its own commit.

**Phase 1, core and CLI. Done.** The format-agnostic document and blank model, the
adapter interface, then the PDF adapter for AcroForm fields. Profile store,
label-to-key matching, fill and save. CLI only. All three fixture forms fill from the
command line, under 76 tests.

Four things the real forms taught us, none of which were in this plan when it was
written:

1. **Field names are meaningless and tooltips are usually absent.** The W-9 calls its
   business-name box `f1_02[0]` with no `/TU` at all. Reading labels from the page's
   own geometry is therefore the primary path in Phase 1, not something deferred to
   Phase 6, and it is the same machinery those later phases will use. USCIS is the
   exception: it tags forms for accessibility, and a real tooltip is authoritative and
   *replaces* the geometric guess rather than sitting beside it.
2. **Whose blank is this** turned out to be as important as what it asks for. An I-9's
   employer and preparer sections ask the same questions about a different person, and
   answering them with your own details is a legal misstatement, not a cosmetic bug.
   Detected from a leading qualifier in the label and from the section heading above
   it, with per-key exemptions, because an EIN is an "employer identification number"
   about you.
3. **Either/or fields must never be filled as both.** The W-9 prints a literal "or"
   between its SSN row and its EIN row. When the profile can satisfy both, Omaform fills
   neither and asks, via `--prefer`.
4. **Labels wrap, and label association works on lines rather than words.** Word-level
   association drags prose in from neighbouring columns; line clustering anchored on a
   fixed word, with runs that refuse to cross a column gap, does not.

**Phase 2, the app. Half done.** The GTK4 window exists, follows the Omarchy theme,
opens a form, chooses an identity, shows what will be filled and where, and writes it.
The identity editor lives in it, so the terminal is no longer the only way to keep
details current.

The page view is not built yet, and it turned out to be less urgent than this plan
assumed. The question you have in front of a form is "is it putting the right thing in
the right place", and a list answering that in the form's own words is both quicker to
read than a rendered page and far cheaper to build. Rendering earns its place when
blanks have to be *placed* by hand, which is Phase 6's problem, not this one's.

The page view arrived after all, and for a concrete reason: the W-4's signature line
was missed, and a list cannot show you *where* a miss is or let you fix it. The form is
now rendered through poppler with the fill drawn over it, taking the signature's size
from the same function the writer uses so that what is shown is what is saved. Values
are adjusted in the list beside the page and the page follows. A right-click on the
page signs or dates at that point; the click becomes the bottom-left of the mark, so a
click on a printed line signs on that line.

Click-to-edit is in too, and it makes Omaform a filler for any PDF rather than only
the forms it recognises: every field is outlined on the page, a click on a checkbox
toggles it and a click on a text box asks for its text, a right-click anywhere places a
signature, a date, typed text or a tick, and a row's X clears what was filled. Edits by
hand are kept apart from the plan and laid back over it after every replan, so they
survive switching identities.

Still to do here: tab between boxes, save-as, appearance stream generation so a filled
form can be flattened, and the vault's idle timer and lock-on-session-lock, which were
deferred from Phase 4 because they need a long-lived process and now have one.

**Phase 3, signature. Capture done; placement to do.** The draw-to-capture pad exists,
and a signature is stored per identity in the vault as a trimmed transparent PNG at
three times screen resolution. Two things the real forms settled:

1. **A signature is an image key and must never reach the text path.** The I-9 asks
   for the employee's signature in a plain 323 by 13 point text field. Matched as text,
   a stored signature would have been written into it as base64. Image keys are now
   acknowledged by the planner and never typed.
2. **An image's length is not a value's length.** The capacity penalty compared the
   base64 encoding against the box width and scored the signature out of every
   signature line. Image keys are exempt.

Placement is done too. The image goes into the page content as an XObject with a
soft mask over the matched field's rectangle, at a height that is a multiple of the
field's within a range, and the widget is retired rather than left underneath, because
USCIS paints its fields with a pale blue background that would cover the ink. The
writer re-opens the source file for every write, since the first version mutated the
opened document and a second press of Fill would have stamped the signature twice.

Placement surfaced three matcher gaps that the capacity penalty had been hiding by
suppressing the signature key altogether:

3. **"Signature Date" is a date.** The word leads the label, so the signature key
   claimed it. Dates are now in the signature key's veto list.
4. **A supplement is somebody's section.** The I-9's Supplement A is the preparer's,
   and the section regex only knew "Part" and "Section". It knows "Supplement" now, so
   every blank under that heading is left alone.
5. **The party can be named in the sentence, not the opening.** Supplement B's line
   opens with a long section name and only says "Employer" in its second sentence, past
   any fixed window; the employee's own line mentions the preparer in a third sentence
   that is an aside. A tooltip has no column bleed, so the veto now also scans the
   sentence the matched alias lives in, which catches the first and ignores the second.

A first slice of Phase 6 arrived early with it: **printed signature lines**. The
W-9's "Sign Here" block is a label, an arrow and a rule with no field behind any of it.
The adapter now finds a "Signature" label in the page text, requires empty paper to its
right so that instructions reading "Signature requirements. The..." do not qualify, and
makes a signature blank there plus a date blank beside the printed "Date". The writer
draws image and text straight into the page content for these.

Three more things the W-9 forced, none of them about signatures:

6. **Checkbox labels are on the right.** Every other field is labelled to its left or
   above; a checkbox is labelled after it. The labeler now reads rightward for
   checkbox-sized blanks, which is what turned line 3a's boxes from noise into
   "C corporation", "S corporation" and the rest.
7. **A checkbox takes a state, never a string.** Matched as text, the LLC box took the
   letter meant for the code box beside it, because both carry the same sentence.
   Checkboxes are excluded from text matching and decided by the tax-classification
   choice alone.
8. **Dot leaders are sparse by design.** The leftward label run treated the jump from
   "Partnership)" onto its first leader as a column break, leaving only dots, which
   clean to nothing. A gap onto a leader may now be a leader's width.

Placement by hand is done, by right-click on the page. The W-4 also taught the
printed-line detector two things: a parenthetical hugging the label is part of the
label ("Employee's signature (This form is not valid unless you sign it.)"), and the
possessive before "signature" must reach the matcher, so that an employer's line is
left alone.

Still to do: PNG and photo import with background removal, and page rotation, which is
not yet accounted for.

**Phase 4, vault. Done, ahead of Phases 2 and 3.** Taken early because it was the
limitation the README had to apologise for, and because it needs no window. Argon2id
wrapping a random data key, both layers ChaCha20-Poly1305, the cleartext key-name list
bound into the associated data, cost calibrated per machine, keyring backing as an
option with its weakness stated at the point of use.

Three things worth recording:

1. **Argon2id cost cannot be hardcoded.** This machine does 64 MiB at t=3 in 17ms,
   about 60 guesses a second. Calibration measures twice, because the cost is not
   linear in time_cost at the low end where filling 256 MiB dominates, and lands high
   rather than low because calibration runs warm and a real unlock starts cold. High is
   the safe direction for a security parameter.
2. **The cleartext key-name list has to be authenticated.** It is in the clear so that
   `inspect` can say a form wants your SSN without a passphrase. Unauthenticated,
   anyone could delete `ssn` from it and Omaform would believe you have not got one, and
   silently leave the box empty.
3. **Lazy unlock forced the planner into two passes.** Deciding which keys a document
   asks for needs only labels; fetching values needs the vault. Resolving `--prefer`
   before fetching means a preference already expressed never costs a passphrase
   prompt for a value about to be discarded.

XChaCha20 in the original sketch became ChaCha20-Poly1305: the extended nonce buys
nothing here, because a fresh nonce is generated per save under a stable data key, and
it avoids a libsodium dependency for the sake of it.

Still outstanding from this phase, both of which want a long-lived process: the
15-minute unlock window and auto-lock on session lock. In the CLI each invocation is
its own process, so the choice is a prompt per run or the keyring; in the GTK app an
in-process timer and a logind signal are the natural implementation, so they move to
Phase 2.

**Phase 5, DOCX. Done, after 6 and 7 rather than before.** One adapter, standard
library only: zipfile and minidom, since ElementTree drops namespace declarations it
did not use and Word then refuses the file for an `mc:Ignorable` prefix it cannot find.
All four flavours: `w:sdt` with alias or tag (authoritative label, checkbox via
`w14:checkbox`), legacy `FORMTEXT` and `FORMCHECKBOX` runs, empty table cells beside
or below a labelled cell, and underscore runs in body text with the paragraph's words
before them. Values go into the run structure in place; a signature becomes an inline
`w:drawing` with its own relationship and media part. Lock when saving means a PDF
through headless LibreOffice, which is also how the window gets a page to show, in a
thread, read-only. Black-outs are a PDF thing and the button hides.

**Phase 6, flat PDFs. Done.** Content-stream rules (with the CTM applied) and
underscore runs become blanks when a label sits to their left or above, and only on
documents with no fields of their own, since the instruction pages of a real form are
full of rules that are not blanks. Written into the page content on save. Manual box
placement was already there from Phase 3. Remembered by fingerprint like any form.

**Phase 7, scans. Done.** A fieldless page with fewer than five words in its text
layer is treated as a picture. tesseract gives the words with boxes (confidence 40 and
up), the rules come from runs of dark pixels in the same render, and from there the
page is a flat PDF. Every blank found this way is offered, not written, even on an
exact match: the labels were read, not given.

**Redaction. Done.** True black-out, on the page view by dragging and on the command
line by text or box. A page with a black-out is rendered at 300 dpi with the region
painted over and rebuilt as that image alone; its widgets leave the form's field list,
and the structure tree and XMP go with them. Untouched pages stay untouched. Regions are
never remembered: what has to go from a form is a fact about the occasion.

**Forms in and out. Done.** Open With from the browser's download bar was there
from the desktop file; images of forms are listed now too. `omaform watch` and a user
service watch Downloads through a Gio file monitor, wait for the file to stop growing,
read its blanks, and put up one notification with a Fill button only when there are
blanks to fill. Out: the saved copy is a drag source (a file list, so it drops into a
compose window or a folder), goes on the clipboard with Copy, and Email starts a
message through xdg-email with the file attached where the client can take it and on
the clipboard where it cannot.

**Keyboard filling. Done.** The page view's drawing area takes focus. Tab and
Shift+Tab walk the fields in reading order (page, 6pt row band from the top, then
left to right), typed characters go into the active box through the same override
path a dialog uses, with the list rebuilt only on leaving the box, and Space or Enter
flips a checkbox. The click dialog stays for its Clear button.

**Phase 8, LLM mapping. First version done, ahead of Phases 6 and 7.** Asked for
after a conversation about whether the program reasons (it does not). Never automatic:
a button, and a CLI flag. Two backends, both found rather than configured: Ollama on
the machine, preferring Omarchy's bundled `omarchy` model, and Omarchy's default agent
in its headless mode, since `omarchy agent` itself opens a terminal a program cannot
read back from. The prompt carries the form's questions and profile key names only,
which a test pins by asserting that no stored value appears in it.

Two things settled by running it on the real W-9:

1. **The model never touches a checkbox.** Boxes are ticked from stored facts it is not
   shown, and it ticked "Individual" for an S corporation on exactly that basis. It may
   add or clear text blanks; boxes are not its to decide.
2. **It is slow enough to need saying.** The bundled local model took 31 seconds cold on
   a trivial prompt and 48 on the W-9, so the dialog warns and the reading is kept per
   form and reused.

Still to do: a benchmark of the three backends on a fixture set of messy forms, and a
way to answer the model's questions in the window so a conditional section can be
filled once the answer is known.

**Phase 9, OS integration. Started early, partially done.** The `.desktop` entry, the
icon, and a zenity shim exist now, so Omaform is in the launcher and in a PDF's Open
With menu before the real window is written. A launcher entry that opens a terminal
would have been worse than none.

That work added one thing not in this plan: `$OMAFORM_ASKPASS`, the ssh and sudo
convention for asking for a passphrase when there is no terminal. The desktop entry
cannot prompt on a tty, and the vault has to stay usable from it.

Still to do in this phase: claiming the `application/pdf` default with a first-run
opt-in, which waits on Omaform being able to *view* a PDF and not only fill one; the
Omarchy menu plugin; the Downloads watcher; and the global quick fill overlay with
`wtype`.

**Phase 10, release.** AUR `omaform-git` PKGBUILD modeled on Compy's, `install.sh` and
`get.sh`, README with the security model stated up front, template pack, screenshots,
tagged release. Public at `github.com/fluxcapctr/omaform`.

**Phase 11, browser extension.** Gmail attachment button over native messaging, for
Firefox and Chrome.

---

## 10. Open questions

1. Name. Omaform, or something else.
2. Should the first run import anything from Chrome or Firefox autofill data, or is
   typing the profile once cleaner and less surprising.
3. Checkbox and radio semantics on I-9 style forms need real fixtures to design
   against, so that work is scoped in Phase 6 rather than guessed at now.
