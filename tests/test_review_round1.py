"""Regressions for Astra's first review (docs/reviews/REVIEW_RESULTS.md).

One test per finding where it can be reproduced without a display; the
window findings (13, 14) are covered by the flow tests and by the structure
of rebuild_plan. Values here are made up.
"""

import subprocess
import zipfile

import pikepdf
import pytest

from omaform import llm, memory, plan as planning, redact, vault as vaulting
from omaform.adapters import for_path
from omaform.adapters import docx as docx_adapter
from omaform.matching import Match
from omaform.model import Blank, BlankKind, Document, LabelContext
from omaform.profile import Profile


def text_of(path, page=None) -> str:
    args = ["pdftotext"] + (["-f", str(page), "-l", str(page)] if page else []) + [str(path), "-"]
    return subprocess.run(args, capture_output=True, text=True).stdout


# 1 -------------------------------------------------------------------------

def test_1_locking_a_word_form_to_its_own_pdf_name_leaves_the_source_alone(word_form, tmp_path):
    from pathlib import Path
    before = Path(word_form).read_bytes()
    adapter = for_path(word_form)
    doc = adapter.discover(word_form)
    try:
        adapter.write(doc, {}, str(Path(word_form).with_suffix(".pdf")), lock_form=True)
    except RuntimeError:
        pass  # no LibreOffice here; what matters is the source
    assert Path(word_form).read_bytes() == before
    assert zipfile.ZipFile(word_form).testzip() is None
    assert sorted(p.name for p in Path(word_form).parent.iterdir()
                  if p.suffix == ".docx") == ["application.docx"], "no sibling .docx left"


# 2 -------------------------------------------------------------------------

def _two_page_parent_form(path):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    pdf.add_blank_page(page_size=(612, 792))
    parent = pdf.make_indirect(pikepdf.Dictionary(T=pikepdf.String("id"), FT=pikepdf.Name.Tx))
    kids = []
    for i, secret in enumerate(("123-45-6789", "")):
        kid = pdf.make_indirect(pikepdf.Dictionary(
            Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, Parent=parent,
            T=pikepdf.String(f"k{i}"), Rect=[100, 700, 300, 720],
            V=pikepdf.String(secret)))
        pdf.pages[i].obj.Annots = pikepdf.Array([kid])
        kids.append(kid)
    parent.Kids = pikepdf.Array(kids)
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([parent]))
    pdf.save(path)
    return str(path)


def test_2_a_blacked_out_page_takes_its_fields_from_under_a_surviving_parent(tmp_path):
    src = _two_page_parent_form(tmp_path / "form.pdf")
    out = tmp_path / "out.pdf"
    redact.apply(src, [redact.Region(1, (0, 0, 612, 792))], str(out))
    raw = out.read_bytes()
    pdf = pikepdf.open(str(out))
    [parent] = pdf.Root.AcroForm.Fields
    assert len(parent.Kids) == 1 and str(parent.Kids[0].T) == "k1"
    assert b"123-45-6789" not in raw
    with pikepdf.open(str(out)) as again:
        for obj in again.objects:
            if isinstance(obj, pikepdf.Dictionary) and "/V" in obj:
                assert "123-45-6789" not in str(obj.V)


# 3 -------------------------------------------------------------------------

def test_3_text_on_a_rotated_page_is_blacked_out(tmp_path):
    import cairo
    src = tmp_path / "rot.pdf"
    surface = cairo.PDFSurface(str(src), 200, 300)
    ctx = cairo.Context(surface)
    ctx.select_font_face("Helvetica")
    ctx.set_font_size(14)
    ctx.move_to(40, 100)
    ctx.show_text("SECRET")
    ctx.move_to(40, 200)
    ctx.show_text("KEEPME")
    surface.finish()
    with pikepdf.open(str(src), allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.Rotate = 90
        pdf.save(str(src))
    hits = redact.find_text(str(src), "SECRET")
    assert hits
    out = tmp_path / "out.pdf"
    redact.apply(str(src), hits, str(out))
    # The page is a picture of itself as shown. Where SECRET was is solid
    # black; where KEEPME was still has ink and paper both.
    keep = redact.find_text(str(src), "KEEPME")
    subprocess.run(["pdftoppm", "-r", "72", "-png", str(out), str(tmp_path / "shot")], check=True)
    from PIL import Image
    [png] = list(tmp_path.glob("shot*.png"))
    image = Image.open(png).convert("L")
    w, h = image.size

    def pixels(region):
        x0, y0, x1, y1 = region.rect
        return [image.getpixel((x, y)) for x in range(int(x0) + 2, int(x1) - 1)
                for y in range(int(h - y1) + 2, int(h - y0) - 1)]

    assert max(pixels(hits[0])) < 60, "SECRET is under solid black"
    shown = pixels(keep[0])
    assert min(shown) < 100 and max(shown) > 200, "KEEPME is still there, not blacked out"
    with pikepdf.open(str(out)) as pdf:
        page = pdf.pages[0]
        assert "/Rotate" not in page.obj
        box = [float(v) for v in page.mediabox]
        assert (round(box[2]), round(box[3])) == (300, 200), "shown size, not the unrotated box"


# 4 -------------------------------------------------------------------------

def test_4_answers_already_in_a_document_never_reach_the_prompt(word_form, tmp_path):
    adapter = for_path(word_form)
    doc = adapter.discover(word_form)
    ids = {b.label.best(): b.id for b in doc.blanks}
    filled = tmp_path / "filled.docx"
    adapter.write(doc, {ids["Email address"]: "alex@example.com",
                        ids["Name"]: "Alex Rivera"}, str(filled))
    again = adapter.discover(str(filled))
    prompt = llm.build_prompt(again, "person", {}, ["Alex Rivera"])
    assert "alex@example.com" not in prompt
    assert "Alex Rivera" not in prompt
    assert "Email address" in prompt, "the question itself still goes"


def test_4_answer_shapes_are_scrubbed_even_when_not_known():
    text = "SSN 123-45-6789, EIN 12-3456789, call (512) 555-0100 or a@b.co"
    out = llm.scrub(text, [])
    for secret in ("123-45-6789", "12-3456789", "555-0100", "a@b.co"):
        assert secret not in out


# 5 -------------------------------------------------------------------------

def _one_blank(label="Name", kind=BlankKind.TEXT, **kw):
    return Blank(id="b1", kind=kind, label=LabelContext(left=label, **kw), rect=(0, 0, 200, 12),
                 width_pt=200)


def test_5_a_model_correction_updates_the_key_and_masks_it():
    from omaform.cli import _show
    blank = _one_blank("Full name")
    doc = Document(path="x.pdf", fmt="pdf", blanks=[blank])
    profile = Profile({"full_name": "Alex Rivera"}, {"ssn": "123-45-6789"})
    result = planning.build(doc, profile)
    entry = result.entries[0]
    assert entry.match.key == "full_name"
    llm.apply(llm.Reading(mapping={"b1": "ssn"}), doc, result, profile)
    assert entry.match.key == "ssn" and entry.value == "123-45-6789"
    assert "ssn" in result.sensitive_used
    shown = _show(entry, False)
    assert "123-45-6789" not in shown


def test_5_a_model_fill_of_an_unmatched_blank_can_be_shown():
    from omaform.cli import _show
    blank = _one_blank("Something unusual")
    doc = Document(path="x.pdf", fmt="pdf", blanks=[blank])
    profile = Profile({"full_name": "Alex Rivera"})
    result = planning.build(doc, profile)
    assert result.entries[0].match is None
    llm.apply(llm.Reading(mapping={"b1": "full_name"}), doc, result, profile)
    assert result.entries[0].match.key == "full_name"
    assert "Alex Rivera" in _show(result.entries[0], False)


# 6 -------------------------------------------------------------------------

def _one_field_pdf(path, tooltip):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    field = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Tx,
        T=pikepdf.String("f1"), TU=pikepdf.String(tooltip), Rect=[100, 700, 300, 720]))
    pdf.pages[0].obj.Annots = pikepdf.Array([field])
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
    pdf.save(path)
    return str(path)


def test_6_forms_that_share_field_names_do_not_share_memory(tmp_path):
    a = for_path("a.pdf").discover(_one_field_pdf(tmp_path / "a.pdf", "Name"))
    b = for_path("b.pdf").discover(_one_field_pdf(tmp_path / "b.pdf", "Employer name"))
    assert a.fingerprint != b.fingerprint


def test_6_flat_pdfs_do_not_all_share_one_fingerprint(tmp_path, flat_form):
    import shutil
    other = tmp_path / "blank.pdf"
    pdf = pikepdf.new()
    pdf.add_blank_page()
    pdf.save(str(other))
    assert for_path(flat_form).discover(flat_form).fingerprint != \
        for_path(str(other)).discover(str(other)).fingerprint
    copy = tmp_path / "copy.pdf"
    shutil.copy(flat_form, copy)
    assert for_path(str(copy)).discover(str(copy)).fingerprint == \
        for_path(flat_form).discover(flat_form).fingerprint


# 7 -------------------------------------------------------------------------

def test_7_a_tooltip_naming_the_employer_later_in_its_sentence_is_not_yours():
    blank = Blank(id="b1", kind=BlankKind.TEXT, rect=(0, 0, 200, 12), width_pt=200,
                  label=LabelContext(inside="Enter the full legal name of the employer",
                                     authoritative=True))
    doc = Document(path="x.pdf", fmt="pdf", blanks=[blank])
    result = planning.build(doc, Profile({"full_name": "Alex Rivera"}))
    assert not result.entries[0].filled


def test_7_an_aside_about_a_preparer_in_a_later_sentence_does_not_block_yours():
    blank = Blank(id="b1", kind=BlankKind.TEXT, rect=(0, 0, 200, 12), width_pt=200,
                  label=LabelContext(inside="Enter your full name exactly as it appears on your "
                                            "identity document today. A preparer or "
                                            "translator may assist you.",
                                     authoritative=True))
    doc = Document(path="x.pdf", fmt="pdf", blanks=[blank])
    result = planning.build(doc, Profile({"full_name": "Alex Rivera"}))
    assert result.entries[0].value == "Alex Rivera"


# 8 -------------------------------------------------------------------------

def test_8_a_box_in_office_use_only_is_never_ticked():
    blank = Blank(id="b1", kind=BlankKind.CHECKBOX, rect=(0, 0, 10, 10),
                  label=LabelContext(right="Single or Married filing separately",
                                     section="Office use only"))
    doc = Document(path="x.pdf", fmt="pdf", blanks=[blank])
    result = planning.build(doc, Profile({"filing_status": "single"}))
    assert result.entries[0].value != "checked"


# 9 -------------------------------------------------------------------------

def _ssn_run(label):
    return [Blank(id=f"g{i}", kind=BlankKind.TEXT, rect=(100 + 40 * i, 0, 130 + 40 * i, 12),
                  width_pt=30, max_chars=n, group="ssn-run", group_index=i,
                  label=LabelContext(above=label))
            for i, n in enumerate((3, 2, 4))]


def test_9_a_misspelt_split_number_is_not_written():
    doc = Document(path="x.pdf", fmt="pdf", blanks=_ssn_run("Social securty number"))
    result = planning.build(doc, Profile({}, {"ssn": "123-45-6789"}))
    assert not any(e.filled for e in result.entries)


def test_9_a_read_only_box_dissolves_the_run():
    blanks = _ssn_run("Social security number")
    blanks[1].readonly = True
    doc = Document(path="x.pdf", fmt="pdf", blanks=blanks)
    result = planning.build(doc, Profile({}, {"ssn": "123-45-6789"}))
    assert not any(e.filled and e.blank.readonly for e in result.entries)


# 10: covered by the window code; the folder is dropped on load and close.

# 11 ------------------------------------------------------------------------

def test_11_memory_does_not_bring_back_a_ruled_out_ssn(w9, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    both = Profile({"full_name": "Alex Rivera"}, {"ssn": "123-45-6789", "ein": "12-3456789"})
    doc = for_path(w9).discover(w9)
    memory.remember(doc, planning.build(doc, both, prefer={"ssn"}), "alex")
    fresh = for_path(w9).discover(w9)
    result = planning.build(fresh, both, prefer={"ein"})
    memory.apply(memory.recall(fresh), result, both, "alex")
    values = [e.value for e in result.entries if e.value]
    assert "123-45-6789" not in values
    assert not any(e.match and e.match.key == "ssn" and e.filled for e in result.entries)


# 12 ------------------------------------------------------------------------

def test_12_one_identitys_filing_status_does_not_follow_the_form(w4, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    single = Profile({"full_name": "Alex Rivera", "filing_status": "single"})
    married = Profile({"full_name": "Sam Rivera", "filing_status": "married_jointly"})
    doc = for_path(w4).discover(w4)
    memory.remember(doc, planning.build(doc, single), "alex")
    fresh = for_path(w4).discover(w4)
    result = planning.build(fresh, married)
    memory.apply(memory.recall(fresh), result, married, "alex")  # same slug, new answer
    ticked = [e for e in result.entries if e.value == "checked"]
    assert all("ingle" not in e.blank.label.best() for e in ticked), \
        [e.blank.label.best() for e in ticked]


# 15, 16 --------------------------------------------------------------------

def test_15_two_lines_in_one_paragraph_both_fill(tmp_path):
    from conftest import _MC, _W, _W14, _p, make_docx
    body = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W} {_W14} {_MC} '
            f'mc:Ignorable="w14"><w:body>{_p("Name: ________ Email: ________")}'
            '</w:body></w:document>')
    src = make_docx(tmp_path / "two.docx", body)
    adapter = for_path(src)
    doc = adapter.discover(src)
    ids = {b.label.best(): b.id for b in doc.blanks}
    out = tmp_path / "out.docx"
    adapter.write(doc, {ids["Name"]: "Alex Rivera", ids["Email"]: "alex@example.com"}, str(out))
    from xml.dom import minidom
    dom = minidom.parseString(zipfile.ZipFile(out).read("word/document.xml"))
    assert docx_adapter._text(dom) == "Name: Alex Rivera Email: alex@example.com"


def test_16_a_signature_is_linked_even_from_an_empty_relationships_part(word_form, tmp_path):
    import io
    import cairo
    src = tmp_path / "empty-rels.docx"
    with zipfile.ZipFile(word_form) as zin, zipfile.ZipFile(src, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/_rels/document.xml.rels":
                data = (b'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns='
                        b'"http://schemas.openxmlformats.org/package/2006/relationships"/>')
            zout.writestr(item, data)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 60, 20)
    png = io.BytesIO()
    surface.write_to_png(png)
    adapter = for_path(str(src))
    doc = adapter.discover(str(src))
    sig = next(b.id for b in doc.blanks if b.label.best() == "Signature")
    out = tmp_path / "signed.docx"
    adapter.write(doc, {}, str(out), images={sig: png.getvalue()})
    rels = zipfile.ZipFile(out).read("word/_rels/document.xml.rels").decode()
    assert 'Id="rIdOmaform1"' in rels
    _every_picture_resolves(out)


def _every_picture_resolves(path):
    import re
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode()
        rels = z.read("word/_rels/document.xml.rels").decode()
        names = set(z.namelist())
    embeds = re.findall(r'r:embed="([^"]+)"', xml)
    assert embeds
    for rid in embeds:
        m = re.search(rf'Id="{rid}"[^>]*Target="([^"]+)"', rels) or \
            re.search(rf'Target="([^"]+)"[^>]*Id="{rid}"', rels)
        assert m, f"{rid} has no relationship"
        assert "word/" + m.group(1) in names, f"{rid} points at a missing part"
    return embeds


def test_16_signing_a_signed_document_again_allocates_new_parts(tmp_path):
    """A picture content control keeps its place after signing, so the same
    control can be signed twice and both pictures must resolve."""
    import io
    import cairo
    from conftest import _MC, _W, _W14, make_docx
    picture = ('<w:p><w:sdt><w:sdtPr><w:alias w:val="Signature"/><w:picture/></w:sdtPr>'
               '<w:sdtContent><w:r><w:t>sign</w:t></w:r></w:sdtContent></w:sdt></w:p>')
    body = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W} {_W14} {_MC} '
            f'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
            f'mc:Ignorable="w14"><w:body>{picture}</w:body></w:document>')
    src = make_docx(tmp_path / "pic.docx", body)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 60, 20)
    png = io.BytesIO()
    surface.write_to_png(png)
    adapter = for_path(src)
    doc = adapter.discover(src)
    [sig] = [b for b in doc.blanks if b.kind is BlankKind.SIGNATURE]
    once = tmp_path / "once.docx"
    adapter.write(doc, {}, str(once), images={sig.id: png.getvalue()})
    doc2 = adapter.discover(str(once))
    [sig2] = [b for b in doc2.blanks if b.kind is BlankKind.SIGNATURE]
    assert sig2.label.best() == "Signature"
    twice = tmp_path / "twice.docx"
    adapter.write(doc2, {}, str(twice), images={sig2.id: png.getvalue()})
    assert "word/media/omaform2.png" in zipfile.ZipFile(twice).namelist()
    assert _every_picture_resolves(twice) == ["rIdOmaform2"], "the old picture was replaced"


# 17, 18 --------------------------------------------------------------------

def test_17_hostile_kdf_costs_are_refused_before_argon2_runs():
    good = vaulting.Kdf(salt=b"\x00" * 16).to_json()
    for field, value in (("time_cost", 2 ** 31), ("memory_cost", 2 ** 31),
                         ("parallelism", 10 ** 6)):
        with pytest.raises(vaulting.VaultError):
            vaulting.Kdf.from_json({**good, field: value})
    with pytest.raises(vaulting.VaultError):
        vaulting.Kdf.from_json({**good, "salt": "AA=="})


def test_18_a_version_one_vaults_keys_are_found_under_their_identity(tmp_path):
    from test_vault import write_version_1_vault
    path = tmp_path / "vault.json"
    write_version_1_vault(path, "correct horse", {"ssn": "123-45-6789"})
    v = vaulting.Vault(path)
    assert v.keys_for("me") == ["ssn"], "found before unlocking, so a fill asks for it"
    v.unlock("correct horse")
    assert v.values_for("me") == {"ssn": "123-45-6789"}


# 19 ------------------------------------------------------------------------

def test_19_a_field_that_is_its_own_parent_is_refused_not_looped(tmp_path):
    pdf = pikepdf.new()
    pdf.add_blank_page()
    widget = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, Rect=[0, 0, 10, 10]))
    widget.Parent = widget
    pdf.pages[0].obj.Annots = pikepdf.Array([widget])
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([widget]))
    with pytest.raises(ValueError):
        redact._prune_form(pdf)
    from omaform import pages
    src = tmp_path / "cyclic.pdf"
    pdf.save(str(src))
    with pytest.raises(ValueError):
        pages.assemble(pages.sequence(str(src)), str(tmp_path / "out.pdf"))


# DOCX odd input ------------------------------------------------------------

def test_a_word_file_with_no_body_or_a_doctype_is_refused_cleanly(tmp_path):
    from conftest import make_docx
    no_body = make_docx(tmp_path / "nobody.docx",
                        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
                        'openxmlformats.org/wordprocessingml/2006/main"/>')
    with pytest.raises(ValueError):
        for_path(no_body).discover(no_body)
    bomb = make_docx(tmp_path / "bomb.docx",
                     '<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaa">]><d>&a;</d>')
    with pytest.raises(ValueError):
        for_path(bomb).discover(bomb)
