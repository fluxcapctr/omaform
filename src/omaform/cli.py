"""Command line entry point.

The CLI exists before the GTK app on purpose: it keeps the interesting logic
testable without a display, and it is the only interface the test suite needs.
"""

from __future__ import annotations

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

from . import llm
from . import memory
from . import pages
from . import plan as planning
from . import redact
from . import watch as watching
from . import vault as vaulting
from .adapters import for_path
from .profile import (BY_KEY, CHOICES, SCHEMA, SENSITIVE_KEYS, Identity, Library,
                      Profile, data_dir)


def _mask(value: str) -> str:
    """Sensitive values are never printed in full, not even by the owner's own
    terminal, because terminals scroll back and sessions get shared."""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def _vault() -> vaulting.Vault:
    return vaulting.Vault(data_dir() / "vault.enc")


def _askpass(prompt: str) -> str | None:
    """Ask for a passphrase through $OMAFORM_ASKPASS, the way ssh and sudo do.

    Needed because the desktop launcher has no terminal. The helper is run with
    the prompt as its only argument and is expected to print the passphrase on
    stdout; `packaging/omaform-askpass` is a three-line zenity wrapper.
    """
    helper = os.environ.get("OMAFORM_ASKPASS")
    if not helper:
        return None
    try:
        done = subprocess.run([helper, prompt], capture_output=True, text=True,
                              timeout=300)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"askpass helper failed: {exc}", file=sys.stderr)
        return None
    if done.returncode != 0:
        return None  # the user cancelled
    return done.stdout.rstrip("\n") or None


def _ask_passphrase(prompt: str = "Vault passphrase: ") -> str:
    if not sys.stdin.isatty():
        phrase = _askpass(prompt)
        if phrase:
            return phrase
        raise SystemExit(
            "the vault is locked and there is no terminal to ask on. Either run "
            "this interactively, set $OMAFORM_ASKPASS to a helper that can ask, "
            "enable the keyring with `omaform vault keyring on`, or pass the "
            "value for one run with --once.")
    try:
        return getpass.getpass(prompt)
    except EOFError:
        # Ctrl-D, or input that ran out. A traceback here tells the user
        # nothing and looks like a crash.
        raise SystemExit("\nno passphrase given") from None


def _ask_value(prompt: str) -> str:
    """Prompt for a secret value with no echo, as for a passphrase."""
    if not sys.stdin.isatty():
        raise SystemExit("no terminal to read the value on; use KEY=VALUE if you "
                         "really want it on the command line")
    try:
        return getpass.getpass(prompt)
    except EOFError:
        raise SystemExit("\nnothing entered") from None


def _unlock(vault: vaulting.Vault, *, allow_keyring: bool = True) -> None:
    """Unlock, preferring a stored keyring passphrase over asking.

    Three attempts, because a mistyped passphrase costs a second of Argon2 and
    starting the whole command again is worse.
    """
    if vault.unlocked:
        return
    if allow_keyring:
        stored = vaulting.keyring_lookup()
        if stored:
            try:
                vault.unlock(stored)
                return
            except vaulting.BadPassphrase:
                print("the passphrase in your keyring no longer opens the vault",
                      file=sys.stderr)

    for attempt in range(3):
        try:
            vault.unlock(_ask_passphrase())
            return
        except vaulting.BadPassphrase as exc:
            remaining = 2 - attempt
            print(f"{exc}" + (f", {remaining} tries left" if remaining else ""),
                  file=sys.stderr)
    raise SystemExit(1)


def _identity(args: argparse.Namespace) -> Identity:
    """Whose details are filling this form."""
    library = Library()
    wanted = getattr(args, "profile", None)
    identity = library.resolve(wanted)
    if identity is None:
        if wanted:
            known = ", ".join(i.label for i in library.all()) or "none yet"
            raise SystemExit(f"no identity called {wanted!r}. Known: {known}")
        raise SystemExit(
            "no identities yet. Create one with:\n"
            "  omaform profile new \"Your Name\"\n"
            "  omaform profile set full_name=\"Your Name\" city=... ")
    return identity


def build_profile(identity: Identity, once: dict[str, str],
                  announce=lambda msg: None) -> Profile:
    """One identity's details, with the vault attached but deliberately shut.

    The unlocker runs only if something actually asks for a sensitive value, so
    filling a form that needs none never prompts. Shared with the desktop app,
    which passes its own `announce` and its own unlock.
    """
    profile = Profile(dict(identity.values), dict(once))

    vault = _vault()
    if vault.exists:
        # Anything given with --once wins outright, and needs no unlock.
        keys = frozenset(vault.keys_for(identity.slug)) - set(once)
        if keys:
            def unlock_now() -> dict[str, str]:
                announce(f"this form needs {', '.join(sorted(keys))} from your vault")
                _unlock(vault)
                return vault.values_for(identity.slug)

            profile.vault_keys = keys
            profile.unlocker = unlock_now
    return profile


def _load_profile(args: argparse.Namespace) -> Profile:
    identity = _identity(args)
    args.identity = identity
    if getattr(args, "verbose", False) or getattr(args, "profile", None):
        print(f"filling as {identity.label}", file=sys.stderr)
    return build_profile(identity, _parse_once(args.once),
                         lambda msg: print(msg, file=sys.stderr))


def _preference(args: argparse.Namespace) -> set[str]:
    """What the identity always does, plus anything asked for on this run."""
    identity = getattr(args, "identity", None)
    chosen = identity.preference() if identity else set()
    return chosen | set(args.prefer or [])


def _parse_once(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            raise SystemExit(f"--once wants key=value, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if key not in BY_KEY:
            raise SystemExit(f"unknown profile key {key!r}; see `omaform profile keys`")
        out[key] = value
    return out


def _show(entry: planning.Entry, verbose: bool) -> str:
    blank = entry.blank
    key = entry.match.key if entry.match else "(by hand)"
    spec_of = BY_KEY.get(entry.match.key) if entry.match else None
    label = blank.label.best() or "(no label found)"
    if entry.match and entry.match.source not in ("native", "kind"):
        label = getattr(blank.label, entry.match.source, "") or label
    if entry.image is not None:
        head = f"  ✓ {label[:52]:54} {key:16} = (drawn signature)"
    elif entry.value == "checked" and spec_of is not None and spec_of.sensitive:
        # Which box is ticked is the answer, so the box's own words go too.
        head = f"  ✓ {'(a sensitive choice)':54} {key:16} = ***"
    elif entry.value == "checked":
        head = f"  ✓ {label[:52]:54} {key:16} = ☑ {entry.note}"
    elif entry.value:
        spec = BY_KEY.get(entry.match.key) if entry.match else None
        # Unknown provenance is masked: better a hidden name than a shown SSN.
        shown = entry.value if (spec is not None and not spec.sensitive) else _mask(entry.value)
        head = f"  ✓ {label[:52]:54} {key:16} = {shown}"
    elif entry.suggested_key:
        head = f"  ? {label[:52]:54} {entry.note}"
    else:
        head = f"  · {label[:52]:54} {entry.note}"
    if verbose:
        head += f"\n      id={blank.id} {blank.kind.value} {blank.width_pt:.0f}pt"
        if entry.match:
            head += f" score={entry.match.score:.2f} via {entry.match.source}"
    return head


def _consult_model(args: argparse.Namespace, doc, result, profile) -> None:
    """--ask-model: have a model read the form, or reuse what it said before."""
    backend = getattr(args, "ask_model", None)
    if not backend:
        return
    identity = getattr(args, "identity", None)
    reading = llm.remembered(doc)
    if reading is not None:
        print(f"using the model's earlier reading of this form ({reading.backend})",
              file=sys.stderr)
    else:
        print(f"asking the model ({backend})... this can take a minute", file=sys.stderr)
        reading = llm.read_form(doc, identity.kind if identity else "person",
                                {e.blank.id: e.match.key for e in result.filled if e.match},
                                backend, known_values=profile.values_in_hand())
    changed, notes = llm.apply(reading, doc, result, profile)
    print(f"the model changed {len(changed)} blanks", file=sys.stderr)
    for entry in changed:
        mark = "+" if entry.filled else "-"
        print(f"  {mark} {entry.blank.label.best()[:44]:46} {entry.note[:70]}",
              file=sys.stderr)
    for note in notes:
        print(f"  ? {note}", file=sys.stderr)


def _recall(args: argparse.Namespace, doc, result, profile) -> None:
    """A form saved before fills the way it was saved, unless told to forget."""
    if getattr(args, "fresh", False):
        return
    remembered = memory.recall(doc)
    if remembered is None:
        return
    identity = getattr(args, "identity", None)
    slug = identity.slug if identity else ""
    added = memory.restore_placements(remembered, doc)
    if added:
        result_fresh = planning.build(doc, profile, _preference(args))
        result.entries[:] = result_fresh.entries
        result.ruled_out, result.conflicts = result_fresh.ruled_out, result_fresh.conflicts
    changed = memory.apply(remembered, result, profile, slug)
    if changed or added:
        print(f"as last time: {len(changed)} blanks", file=sys.stderr)


def _report_conflicts(result: planning.Plan) -> None:
    for group in result.conflicts:
        keys = sorted(group)
        print(f"\nthis form wants {' or '.join(keys)}, and your profile has both. "
              f"Left blank on purpose: filling both is wrong on a signed form.")
        print(f"  choose with: --prefer {keys[0]}")


def cmd_inspect(args: argparse.Namespace) -> int:
    profile = _load_profile(args)
    doc = for_path(args.file).discover(args.file)
    result = planning.build(doc, profile, _preference(args))
    # Memory first, the model after: the model's decisions win over last time's.
    _recall(args, doc, result, profile)
    _consult_model(args, doc, result, profile)

    print(f"{args.file}: {doc.fmt}, {doc.page_count} pages, "
          f"{len(doc.blanks)} blanks, fingerprint {doc.fingerprint}")
    for entry in result.entries:
        print(_show(entry, args.verbose))
    print(f"\n{len(result.filled)} of {len(doc.fillable())} fillable blanks would be filled")
    _report_conflicts(result)
    return 0


def cmd_fill(args: argparse.Namespace) -> int:
    profile = _load_profile(args)
    doc = for_path(args.file).discover(args.file)
    result = planning.build(doc, profile, _preference(args))
    # Memory first, the model after: the model's decisions win over last time's.
    _recall(args, doc, result, profile)
    _consult_model(args, doc, result, profile)

    src = Path(args.file)
    out = Path(args.output) if args.output else src.with_name(f"{src.stem}-filled{src.suffix}")
    if out.resolve() == src.resolve():
        raise SystemExit("refusing to overwrite the original; pass -o")

    for entry in result.entries:
        if entry.value or args.verbose:
            print(_show(entry, args.verbose))

    _report_conflicts(result)
    if args.dry_run:
        print(f"\ndry run: {len(result.filled)} blanks would be written")
        return 0

    for_path(args.file).write(doc, result.values(), str(out), images=result.images(),
                              lock_form=bool(getattr(args, "lock", False)))
    if getattr(args, "lock", False) and doc.fmt == "docx":
        out = out.with_suffix(".pdf")
    print(f"\nwrote {len(result.filled)} values to {out}"
          + (" and locked it: no one can edit it now" if getattr(args, "lock", False) else ""))
    identity = getattr(args, "identity", None)
    memory.remember(doc, result, identity.slug if identity else "")
    print("remembered how this form was filled", file=sys.stderr)
    if result.sensitive_used:
        keys = ", ".join(sorted(result.sensitive_used))
        print(f"note: {out.name} now contains {keys} in plain text. "
              f"It is a document, not a vault: mind where it goes.")
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    library = Library()

    if args.action == "keys":
        for spec in SCHEMA:
            flag = " (sensitive)" if spec.sensitive else ""
            print(f"  {spec.key:16} {spec.title}{flag}")
        return 0

    if args.action == "new":
        label = " ".join(args.pairs).strip()
        if not label:
            raise SystemExit('what should it be called? omaform profile new "Me"')
        identity = library.create(label, kind=args.kind)
        tax = ", ".join(sorted(identity.preference())) or "none"
        print(f"  created {identity.label} ({identity.slug}), a {identity.kind}; "
              f"tax number: {tax}")
        print(f"  omaform profile set --profile {identity.slug} full_name=...")
        return 0

    if args.action == "list":
        identities = library.all()
        if not identities:
            print(f"no identities yet in {library.profiles_dir}")
            print('create one with: omaform profile new "Your Name"')
            return 0
        default = library.default_slug()
        vault = _vault()
        stored = set(vault.key_names()) if vault.exists else set()
        for identity in identities:
            mark = "*" if identity.slug == default else " "
            tax = ", ".join(sorted(identity.preference())) or "none"
            print(f"{mark} {identity.label}  ({identity.slug})  "
                  f"{identity.kind}, uses {tax}")
            for spec in SCHEMA:
                value = identity.values.get(spec.key)
                if value:
                    print(f"      {spec.key:16} {value}")
            for spec in SCHEMA:
                if f"{identity.slug}/{spec.key}" in stored:
                    print(f"      {spec.key:16} (in the vault)")
            profile = Profile(identity.values)
            for spec in SCHEMA:
                if spec.composite and spec.key not in identity.values \
                        and profile.get(spec.key):
                    print(f"      {spec.key:16} {profile.get(spec.key)}   (built from parts)")
        print("\n* is the one used when --profile is not given")
        return 0

    if args.action == "default":
        identity = library.resolve(" ".join(args.pairs) or None)
        if identity is None:
            raise SystemExit("which identity? see `omaform profile list`")
        library.set_default(identity.slug)
        print(f"  {identity.label} is now the default")
        return 0

    if args.action == "kind":
        identity = _identity(args)
        changed = library.set_kind(identity.slug, args.kind)
        if changed is None:
            raise SystemExit("kind must be person or business")
        tax = ", ".join(sorted(changed.preference())) or "none"
        print(f"  {changed.label} is a {changed.kind}; tax number: {tax}")
        return 0

    if args.action == "rename":
        if len(args.pairs) < 2:
            raise SystemExit('omaform profile rename <slug> "New label"')
        identity = library.rename(args.pairs[0], " ".join(args.pairs[1:]))
        if identity is None:
            raise SystemExit(f"no identity called {args.pairs[0]!r}")
        print(f"  now called {identity.label}")
        return 0

    if args.action == "delete":
        identity = library.resolve(" ".join(args.pairs) or None)
        if identity is None:
            raise SystemExit("which identity? see `omaform profile list`")
        library.delete(identity.slug)
        print(f"  deleted {identity.label}")
        vault = _vault()
        if vault.exists and vault.keys_for(identity.slug):
            print(f"  it also had {', '.join(vault.keys_for(identity.slug))} in the "
                  f"vault; remove with `omaform vault unset --profile "
                  f"{identity.slug} ...`")
        return 0

    if args.action == "set":
        identity = _identity(args)
        for item in args.pairs:
            if "=" not in item:
                raise SystemExit(f"want key=value, got {item!r}")
            key, value = item.split("=", 1)
            try:
                library.set_value(identity.slug, key.strip(), value)
            except PermissionError as exc:
                print(f"refused: {exc}", file=sys.stderr)
                return 2
            except KeyError as exc:
                print(f"{exc}; see `omaform profile keys`", file=sys.stderr)
                return 2
            print(f"  {identity.label}: {key.strip()} set")
        return 0

    raise SystemExit(f"unknown profile action {args.action}")


def cmd_vault(args: argparse.Namespace) -> int:
    vault = _vault()

    if args.action == "init":
        if vault.exists:
            print(f"a vault already exists at {vault.path}", file=sys.stderr)
            return 2
        first = _ask_passphrase("Choose a vault passphrase: ")
        if first != _ask_passphrase("Again: "):
            print("those did not match", file=sys.stderr)
            return 2
        print("deriving a key (this is meant to be slow)...", file=sys.stderr)
        vault.create(first)
        print(f"created {vault.path}")
        print("add values with: omaform vault set ssn")
        return 0

    if args.action == "keys":
        for spec in SCHEMA:
            if spec.sensitive:
                print(f"  {spec.key:16} {spec.title}")
        return 0

    if args.action == "list":
        if not vault.exists:
            print(f"no vault yet; create one with `omaform vault init`")
            return 0
        library = Library()
        print(f"{vault.path}")
        if not vault.key_names():
            print("  (empty)")
        for slug in vault.identities():
            found = library.get(slug)
            print(f"  {found.label if found else slug}  ({slug})")
            for key in vault.keys_for(slug):
                print(f"      {key:16} {BY_KEY[key].title if key in BY_KEY else key}")
        print("\nvalues are never printed; the vault stays locked for this")
        return 0

    if args.action == "keyring":
        mode = (args.values[0] if args.values else "status").lower()
        if not vaulting.keyring_available():
            print("secret-tool is not installed, so no keyring is available",
                  file=sys.stderr)
            return 2
        if mode == "status":
            print("keyring passphrase: " +
                  ("stored" if vaulting.keyring_lookup() else "not stored"))
            return 0
        if mode == "on":
            if not vault.exists:
                print("create a vault first", file=sys.stderr)
                return 2
            phrase = _ask_passphrase()
            try:
                vault.unlock(phrase)
            except vaulting.BadPassphrase as exc:
                print(exc, file=sys.stderr)
                return 2
            if not vaulting.keyring_store(phrase):
                print("the keyring refused to store it", file=sys.stderr)
                return 2
            print("stored. The vault will now open without asking.")
            print("Worth knowing: while your session is unlocked, anything running")
            print("as you can ask the keyring for this, the same way it can read")
            print("your browser's saved passwords.")
            return 0
        if mode == "off":
            print("removed" if vaulting.keyring_clear() else "nothing was stored")
            return 0
        print(f"unknown keyring mode {mode!r}; want on, off or status", file=sys.stderr)
        return 2

    if not vault.exists:
        print("no vault yet; create one with `omaform vault init`", file=sys.stderr)
        return 2

    if args.action == "passwd":
        _unlock(vault, allow_keyring=False)
        first = _ask_passphrase("New passphrase: ")
        if first != _ask_passphrase("Again: "):
            print("those did not match", file=sys.stderr)
            return 2
        vault.change_passphrase(first)
        print("changed. The data key was re-wrapped; the contents were not touched.")
        if vaulting.keyring_lookup():
            print("your keyring still holds the old one; run "
                  "`omaform vault keyring on` to update it")
        return 0

    if args.action == "set":
        if not args.values:
            print("which key? try: omaform vault set ssn", file=sys.stderr)
            return 2
        identity = _identity(args)
        _unlock(vault)
        for item in args.values:
            if "=" in item:
                key, value = item.split("=", 1)
                print(f"note: {key} came from the command line, so it is now in "
                      f"your shell history. `omaform vault set {key}` prompts instead.",
                      file=sys.stderr)
            else:
                key, value = item, ""
            key = key.strip()
            if key not in SENSITIVE_KEYS:
                print(f"{key!r} is not a sensitive key; use `omaform profile set` "
                      f"for it, or see `omaform vault keys`", file=sys.stderr)
                return 2
            if BY_KEY[key].image:
                print(f"{key} is drawn, not typed: open the window (`omaform ui`), "
                      f"Identities tab, and press Draw", file=sys.stderr)
                return 2
            if not value:
                if key in CHOICES:
                    print(f"  one of: {', '.join(CHOICES[key])}", file=sys.stderr)
                value = _ask_value(f"{BY_KEY[key].title}: ")
            if not value:
                print(f"  {key} not set: nothing entered", file=sys.stderr)
                continue
            if key in CHOICES and value not in CHOICES[key]:
                print(f"  {key} must be one of: {', '.join(CHOICES[key])}", file=sys.stderr)
                return 2
            vault.set(identity.slug, key, value)
            print(f"  {identity.label}: {key} stored")
        return 0

    if args.action == "unset":
        if not args.values:
            print("which key?", file=sys.stderr)
            return 2
        identity = _identity(args)
        _unlock(vault)
        for key in args.values:
            gone = vault.unset(identity.slug, key.strip())
            print(f"  {identity.label}: {key} {'removed' if gone else 'was not set'}")
        return 0

    raise SystemExit(f"unknown vault action {args.action}")


def cmd_forget(args: argparse.Namespace) -> int:
    doc = for_path(args.file).discover(args.file)
    memory.forget(doc)
    llm.forget(doc)
    print(f"forgot {Path(args.file).name}")
    return 0


def cmd_pages(args: argparse.Namespace) -> int:
    """omaform pages out.pdf a.pdf#1-2 b.pdf c.pdf#3@90 ..."""
    if args.images:
        count = pages.images_to_pdf(args.sources, args.out)
        print(f"{count} images became {args.out}")
        return 0
    sequence: list[pages.Source] = []
    for spec in args.sources:
        sequence += pages.parse_spec(spec)
    if any(os.path.abspath(s.path) == os.path.abspath(args.out) for s in sequence):
        raise SystemExit("the output must be a new file; a source is never overwritten")
    count = pages.assemble(sequence, args.out)
    print(f"wrote {count} pages to {args.out}")
    return 0


def cmd_redact(args: argparse.Namespace) -> int:
    """omaform redact out.pdf in.pdf --text 123-45-6789 --box 1:100,600,300,620"""
    regions: list[redact.Region] = []
    for spec in args.box or []:
        regions.append(redact.Region.parse(spec))
    for needle in args.text or []:
        hits = redact.find_text(args.source, needle)
        if not hits:
            print(f"not found on any page: {needle!r}", file=sys.stderr)
        regions += hits
    if not regions:
        raise SystemExit("nothing to black out: give --text or --box")
    if os.path.abspath(args.source) == os.path.abspath(args.out):
        raise SystemExit("the output must be a new file; a source is never overwritten")
    count = redact.apply(args.source, regions, args.out)
    touched = sorted({r.page for r in regions})
    print(f"blacked out {len(regions)} region{'s' if len(regions) != 1 else ''} on "
          f"page{'s' if count != 1 else ''} {', '.join(map(str, touched))}; wrote {args.out}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """omaform watch [FOLDER]: a notice with a Fill button when a form lands."""
    folder = args.folder or os.path.join(os.path.expanduser("~"), "Downloads")
    print(f"watching {folder} for forms; Ctrl-C to stop", flush=True)
    watching.Watcher(folder).run()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """omaform doctor [--json]: what is installed, and how to add what is not."""
    from . import doctor
    print(doctor.report(as_json=args.json))
    return 0


def cmd_recent(args: argparse.Namespace) -> int:
    """omaform recent [--json]: the newest forms in Downloads, for the bar widget."""
    import json
    folder = Path(args.folder or os.path.join(os.path.expanduser("~"), "Downloads"))
    files = []
    if folder.is_dir():
        files = sorted((f for f in folder.iterdir()
                        if f.suffix.lower() in (".pdf", ".docx") and f.is_file()
                        and "-filled" not in f.stem),
                       key=lambda f: f.stat().st_mtime, reverse=True)[: args.limit]
    items = [{"path": str(f), "name": f.name, "blanks": watching.is_form(str(f))}
             for f in files]
    items = [i for i in items if i["blanks"]]
    if args.json:
        print(json.dumps(items))
    else:
        for item in items:
            print(f"{item['blanks']:4} blanks  {item['name']}")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Open the window. Imported here so the CLI does not need GTK to run."""
    from .ui.app import main as ui_main

    argv = ["omaform-ui"]
    if args.file:
        argv.append(args.file)
    return ui_main(argv)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omaform", description="Fill forms from a saved profile.")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    once_help = ("a value for this run only, never written to disk; the way to "
                 "supply sensitive fields until the encrypted vault lands")

    p_fill = sub.add_parser("fill", help="fill a form and save a copy")
    p_fill.add_argument("file")
    p_fill.add_argument("-o", "--output")
    p_fill.add_argument("--once", action="append", default=[], metavar="KEY=VALUE",
                        help=once_help)
    p_fill.add_argument("--prefer", action="append", default=[], metavar="KEY",
                        help="which key to use where a form offers either/or, "
                             "as with the W-9's SSN or EIN")
    p_fill.add_argument("-p", "--profile", metavar="NAME",
                        help="whose details to fill with; the default identity "
                             "otherwise. See `omaform profile list`")
    p_fill.add_argument("--ask-model", choices=("ollama", "agent"), metavar="BACKEND",
                        help="have a model read the form for conditions the matcher "
                             "cannot: ollama (local) or agent (Omarchy's default agent). "
                             "Only the form's questions and your key names are sent.")
    p_fill.add_argument("--lock", action="store_true",
                        help="burn the fill into the page so no one can edit it (a Word "
                             "file is saved as a PDF). Not for a form someone else "
                             "still has a section to fill")
    p_fill.add_argument("--fresh", action="store_true",
                        help="ignore how this form was filled before")
    p_fill.add_argument("-n", "--dry-run", action="store_true")
    p_fill.set_defaults(func=cmd_fill)

    p_inspect = sub.add_parser("inspect", help="show every blank and what would go in it")
    p_inspect.add_argument("file")
    p_inspect.add_argument("--once", action="append", default=[], metavar="KEY=VALUE",
                           help=once_help)
    p_inspect.add_argument("--prefer", action="append", default=[], metavar="KEY")
    p_inspect.add_argument("-p", "--profile", metavar="NAME")
    p_inspect.add_argument("--ask-model", choices=("ollama", "agent"), metavar="BACKEND")
    p_inspect.add_argument("--fresh", action="store_true")

    p_forget = sub.add_parser("forget", help="forget how a form was filled before")
    p_forget.add_argument("file")
    p_forget.set_defaults(func=cmd_forget)
    p_inspect.set_defaults(func=cmd_inspect)

    p_profile = sub.add_parser("profile", help="read and write your saved details")
    p_profile.add_argument("action", choices=("list", "new", "set", "keys",
                                              "default", "kind", "rename",
                                              "delete"))
    p_profile.add_argument("pairs", nargs="*", metavar="KEY=VALUE")
    p_profile.add_argument("-p", "--profile", metavar="NAME",
                           help="which identity to change; the default otherwise")
    p_profile.add_argument("-k", "--kind", choices=("person", "business"),
                           default="person",
                           help="for `new`: a business files under an EIN, a "
                                "person under a Social Security number")
    p_profile.set_defaults(func=cmd_profile)

    p_vault = sub.add_parser(
        "vault", help="encrypted storage for SSN, EIN, bank and licence details")
    p_vault.add_argument("action", choices=("init", "set", "unset", "list", "keys",
                                            "passwd", "keyring"))
    p_vault.add_argument("-p", "--profile", metavar="NAME",
                         help="whose secret this is; the default identity otherwise")
    p_vault.add_argument("values", nargs="*", metavar="KEY",
                         help="a key to set or remove; values are prompted for, "
                              "not passed here, so they stay out of shell history")
    p_vault.set_defaults(func=cmd_vault)

    p_pages = sub.add_parser(
        "pages", help="merge, split, reorder or rotate pages into a new file")
    p_pages.add_argument("out", help="the new file to write")
    p_pages.add_argument("sources", nargs="+", metavar="FILE[#PAGES][@ROT]",
                         help="a PDF, optionally with pages like 1-3,5 and a rotation "
                              "like @90; or image files with --images")
    p_pages.add_argument("--images", action="store_true",
                         help="the sources are photos or scans, one page each")
    p_pages.set_defaults(func=cmd_pages)

    p_redact = sub.add_parser(
        "redact", help="black out text or a region for good, into a new file")
    p_redact.add_argument("out", help="the new file to write")
    p_redact.add_argument("source", help="the PDF to read")
    p_redact.add_argument("--text", action="append", metavar="TEXT",
                          help="black out every place these words appear; may repeat")
    p_redact.add_argument("--box", action="append", metavar="PAGE:x0,y0,x1,y1",
                          help="black out a rectangle, in points from the bottom left; "
                               "may repeat")
    p_redact.set_defaults(func=cmd_redact)

    p_watch = sub.add_parser(
        "watch", help="offer to fill any form that lands in Downloads")
    p_watch.add_argument("folder", nargs="?", help="folder to watch (default ~/Downloads)")
    p_watch.set_defaults(func=cmd_watch)

    p_recent = sub.add_parser("recent", help="the newest forms in Downloads")
    p_recent.add_argument("folder", nargs="?", help="folder to look in (default ~/Downloads)")
    p_recent.add_argument("--limit", type=int, default=6)
    p_recent.add_argument("--json", action="store_true")
    p_recent.set_defaults(func=cmd_recent)

    p_doctor = sub.add_parser("doctor", help="check what is installed and what is missing")
    p_doctor.add_argument("--json", action="store_true", help="for the Omarchy bar widget")
    p_doctor.set_defaults(func=cmd_doctor)

    p_ui = sub.add_parser("ui", help="open the window")
    p_ui.add_argument("file", nargs="?")
    p_ui.set_defaults(func=cmd_ui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # -v is useful after the subcommand too, which argparse will not do alone.
    if not getattr(args, "verbose", False):
        args.verbose = "-v" in (argv or sys.argv[1:]) or "--verbose" in (argv or sys.argv[1:])
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print(file=sys.stderr)
        return 130
    except vaulting.VaultError as exc:
        # Wrong passphrase, damaged file, unusable parameters. All of these are
        # things the user can act on, so none of them deserve a traceback.
        print(f"vault: {exc}", file=sys.stderr)
        return 2
    except llm.ModelError as exc:
        print(f"model: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"omaform: {exc}", file=sys.stderr)
        return 2
    except (BrokenPipeError, OSError) as exc:
        print(f"omaform: {exc}", file=sys.stderr)
        return 1
    except (subprocess.SubprocessError, RecursionError) as exc:
        print(f"omaform: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001, pikepdf and zipfile errors from a bad file
        if type(exc).__module__.split(".")[0] in ("pikepdf", "zipfile"):
            print(f"omaform: could not read the file: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
