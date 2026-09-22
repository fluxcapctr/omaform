"""Regressions for Astra's second review (docs/reviews/REVIEW_RESULTS_2.md)."""

import json
import subprocess
import zipfile
from pathlib import Path

import pikepdf
import pytest

from omaform import llm, memory, plan as planning, redact, vault as vaulting
from omaform.adapters import for_path
from omaform.adapters import docx as docx_adapter
from omaform.model import Blank, BlankKind, Document, LabelContext
from omaform.profile import Profile


# 2 -------------------------------------------------------------------------

def test_2_a_cropped_page_is_blacked_out_where_the_word_is_shown(tmp_path):
    import cairo
    src = tmp_path / "crop.pdf"
    surface = cairo.PDFSurface(str(src), 400, 400)
    ctx = cairo.Context(surface)
    ctx.select_font_face("Helvetica")
    ctx.set_font_size(14)
    ctx.move_to(120, 150)
    ctx.show_text("SECRET")
    surface.finish()
    with pikepdf.open(str(src), allow_overwriting_input=True) as pdf:
        pdf.pages[0].obj.CropBox = pikepdf.Array([100, 100, 300, 300])
        pdf.save(str(src))
    for rotation in (0, 90, 180, 270):
        rotated = tmp_path / f"crop{rotation}.pdf"
        with pikepdf.open(str(src)) as pdf:
            pdf.pages[0].obj.Rotate = rotation
            pdf.save(str(rotated))
        hits = redact.find_text(str(rotated), "SECRET")
        assert hits, rotation
        out = tmp_path / f"out{rotation}.pdf"
        redact.apply(str(rotated), hits, str(out))
        subprocess.run(["pdftoppm", "-r", "72", "-png", str(out), str(tmp_path / f"s{rotation}")],
                       check=True)
        from PIL import Image
        [png] = list(tmp_path.glob(f"s{rotation}*.png"))
        image = Image.open(png).convert("L")
        h = image.size[1]
        x0, y0, x1, y1 = hits[0].rect
        inside = [image.getpixel((x, y)) for x in range(int(x0) + 2, int(x1) - 1)
                  for y in range(int(h - y1) + 2, int(h - y0) - 1)]
        assert max(inside) < 60, f"text still visible at rotation {rotation}"
        assert sum(1 for v in image.getdata() if v < 60) < 0.25 * image.size[0] * h, \
            "only the word, not the page"


# 3 -------------------------------------------------------------------------

def test_3_calculation_order_loses_retired_fields(tmp_path):
    from test_review_round1 import _two_page_parent_form
    src = _two_page_parent_form(tmp_path / "form.pdf")
    with pikepdf.open(src, allow_overwriting_input=True) as pdf:
        [parent] = pdf.Root.AcroForm.Fields
        pdf.Root.AcroForm.CO = pikepdf.Array([parent.Kids[0], parent.Kids[1]])
        pdf.Root.AcroForm.XFA = pikepdf.String("<xfa>123-45-6789</xfa>")
        pdf.save(src)
    out = tmp_path / "out.pdf"
    redact.apply(src, [redact.Region(1, (0, 0, 612, 792))], str(out))
    with pikepdf.open(str(out)) as pdf:
        assert [str(f.T) for f in pdf.Root.AcroForm.CO] == ["k1"]
        assert "/XFA" not in pdf.Root.AcroForm
    assert b"123-45-6789" not in out.read_bytes()


# 4 -------------------------------------------------------------------------

def _xobject_form(path, heading):
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(612, 792))
    page = pdf.pages[0]
    font = pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font,
                                                Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))
    form = pikepdf.Stream(pdf, f"BT /F1 12 Tf 72 740 Td ({heading}) Tj ET".encode())
    form.Type, form.Subtype = pikepdf.Name.XObject, pikepdf.Name.Form
    form.BBox = [0, 0, 612, 792]
    form.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    page.obj.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Fm0=pdf.make_indirect(form)))
    page.obj.Contents = pdf.make_indirect(pikepdf.Stream(pdf, b"/Fm0 Do"))
    field = pdf.make_indirect(pikepdf.Dictionary(
        Type=pikepdf.Name.Annot, Subtype=pikepdf.Name.Widget, FT=pikepdf.Name.Tx,
        T=pikepdf.String("f1"), TU=pikepdf.String("Name"), Rect=[100, 700, 300, 720]))
    page.obj.Annots = pikepdf.Array([field])
    pdf.Root.AcroForm = pikepdf.Dictionary(Fields=pikepdf.Array([field]))
    pdf.save(path)
    return str(path)


def test_4_a_heading_drawn_through_an_xobject_changes_the_fingerprint(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path / "home"))
    a = _xobject_form(tmp_path / "a.pdf", "Section 1. Employee Information")
    b = _xobject_form(tmp_path / "b.pdf", "Section 2. Employer Information")
    da, db = for_path(a).discover(a), for_path(b).discover(b)
    assert da.fingerprint != db.fingerprint


def test_4_memory_never_fills_what_is_someone_elses(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    blank = Blank(id="b1", kind=BlankKind.TEXT, rect=(0, 0, 200, 12), width_pt=200,
                  label=LabelContext(left="Name", section="Section 2. Employer Information"))
    doc = Document(path="x.pdf", fmt="pdf", blanks=[blank], fingerprint="pdf-test")
    remembered = memory.Memory(keys={"b1": "full_name"})
    result = planning.build(doc, Profile({"full_name": "Alex Rivera"}))
    memory.apply(remembered, result, Profile({"full_name": "Alex Rivera"}), "alex")
    assert not result.entries[0].filled


def test_4_word_forms_under_different_headings_differ(tmp_path):
    from conftest import _MC, _W, _W14, _p, make_docx

    def form(name, heading):
        body = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W} {_W14} {_MC} '
                f'mc:Ignorable="w14"><w:body>{_p(heading, "Heading1")}'
                f'{_p("Name: ________")}</w:body></w:document>')
        return make_docx(tmp_path / name, body)

    a = form("a.docx", "APPLICANT INFORMATION")
    b = form("b.docx", "EMPLOYER INFORMATION")
    assert for_path(a).discover(a).fingerprint != for_path(b).discover(b).fingerprint


# 5, 6 ----------------------------------------------------------------------

def test_5_answers_after_a_colon_in_a_word_form_are_cut(tmp_path):
    from conftest import _MC, _W, _W14, _p, _sdt_text, make_docx
    body = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W} {_W14} {_MC} '
            f'mc:Ignorable="w14"><w:body>{_p("Date of birth: 01/02/1990")}'
            f'{_p("Apartment: A")}'
            f'<w:p><w:r><w:t xml:space="preserve">Phone: </w:t></w:r>{_sdt_text("Phone")}</w:p>'
            '</w:body></w:document>')
    src = make_docx(tmp_path / "done.docx", body)
    doc = for_path(src).discover(src)
    prompt = llm.build_prompt(doc, "person", {})
    assert "01/02/1990" not in prompt and "1990" not in prompt
    assert "Apartment: A" not in prompt


def test_5_split_numbers_single_letters_unicode_and_ids():
    assert "6789" not in llm.scrub("123-45- and 6789", ["123-45-6789"])
    assert "123" not in llm.scrub("123-45-", [])
    assert llm.scrub("Apartment: A", ["A"]) == "Apartment: [answer]"
    decomposed = "José Rivera"
    assert "Rivera" not in llm.scrub(f"Name {decomposed}", ["José Rivera"])
    blank = Blank(id="p1:123-45-6789", kind=BlankKind.TEXT, label=LabelContext(left="SSN"))
    doc = Document(path="x.pdf", fmt="pdf", blanks=[blank])
    prompt = llm.build_prompt(doc, "person", {})
    assert "123-45-6789" not in prompt and '"id": "q1"' in prompt


def test_6_model_replies_are_scrubbed_before_they_are_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    blank = Blank(id="b1", kind=BlankKind.TEXT, label=LabelContext(left="SSN"))
    doc = Document(path="x.pdf", fmt="pdf", blanks=[blank], fingerprint="pdf-scrub")
    answer = json.dumps({"blanks": [{"id": "q1", "key": "ssn", "why": "SSN 123-45-6789"}],
                         "questions": ["Is 123-45-6789 correct?"]})
    reading = llm.parse_answer(answer, doc, "t", ["123-45-6789"])
    llm.remember(doc, reading)
    stored = next((tmp_path).rglob("*.json")).read_text()
    assert "123-45-6789" not in stored and "6789" not in stored
    # And a cache written before this fix is scrubbed when it is read.
    path = next((tmp_path).rglob("*.json"))
    data = json.loads(stored)
    data["questions"] = ["Is 987-65-4321 right?"]
    path.write_text(json.dumps(data))
    assert "987-65-4321" not in json.dumps(llm.remembered(doc).to_json())


# 7 -------------------------------------------------------------------------

def test_7_a_sensitive_tick_is_masked_on_the_command_line(i9):
    from omaform.cli import _show
    doc = for_path(i9).discover(i9)
    result = planning.build(doc, Profile({"full_name": "Alex Rivera"},
                                         {"citizenship": "citizen"}))
    ticked = [e for e in result.entries if e.value == "checked"
              and e.match and e.match.key == "citizenship"]
    assert ticked
    shown = _show(ticked[0], False)
    assert "citizen" not in shown.split("citizenship", 1)[1].lower()
    assert "United States" not in shown and "☑" not in shown


# 10 ------------------------------------------------------------------------

def test_10_a_signature_in_a_run_of_several_text_nodes_lands_on_its_line(tmp_path):
    import io
    import cairo
    from conftest import _MC, _W, _W14, make_docx
    para = ('<w:p><w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">Signature: </w:t>'
            '<w:t>________</w:t><w:tab/><w:t xml:space="preserve"> Date: ________</w:t></w:r></w:p>')
    body = (f'<?xml version="1.0" encoding="UTF-8"?><w:document {_W} {_W14} {_MC} '
            f'mc:Ignorable="w14"><w:body>{para}</w:body></w:document>')
    src = make_docx(tmp_path / "runs.docx", body)
    adapter = for_path(src)
    doc = adapter.discover(src)
    ids = {b.label.best(): b.id for b in doc.blanks}
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 60, 20)
    png = io.BytesIO()
    surface.write_to_png(png)
    out = tmp_path / "out.docx"
    adapter.write(doc, {ids["Date"]: "09/22/2026"}, str(out),
                  images={ids["Signature"]: png.getvalue()})
    from xml.dom import minidom
    dom = minidom.parseString(zipfile.ZipFile(out).read("word/document.xml"))
    order = []
    for r in dom.getElementsByTagNameNS(docx_adapter.W, "r"):
        if r.getElementsByTagNameNS(docx_adapter.W, "drawing"):
            order.append("[IMG]")
        else:
            order.append(docx_adapter._text(r))
    joined = "".join(order)
    assert joined.index("[IMG]") < joined.index("Date"), joined
    assert joined.startswith("Signature: "), joined
    assert dom.getElementsByTagNameNS(docx_adapter.W, "tab").length == 1, "the tab is kept once"
    assert all(r.getElementsByTagNameNS(docx_adapter.W, "b").length
               for r in dom.getElementsByTagNameNS(docx_adapter.W, "r")
               if not r.getElementsByTagNameNS(docx_adapter.W, "drawing")), "formatting kept"


# 11 ------------------------------------------------------------------------

def test_11_a_recalled_clear_survives_saving_again(w9, tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    profile = Profile({"full_name": "Alex Rivera"})

    def open_form():
        doc = for_path(w9).discover(w9)
        result = planning.build(doc, profile)
        kept = memory.recall(doc)
        if kept:
            memory.apply(kept, result, profile, "alex")
        return doc, result

    doc, result = open_form()
    name = next(e for e in result.filled if e.match and e.match.key == "full_name")
    name.value, name.cleared = None, True
    memory.remember(doc, result, "alex")
    for _ in range(2):   # save again untouched, twice
        doc, result = open_form()
        memory.remember(doc, result, "alex")
    doc, result = open_form()
    again = next(e for e in result.entries if e.blank.id == name.blank.id)
    assert not again.filled


# 13 ------------------------------------------------------------------------

def test_13_a_vault_cannot_be_written_with_settings_it_would_refuse(tmp_path):
    bad = vaulting.Kdf(salt=b"\x00" * 8, time_cost=1, memory_cost=32, parallelism=1)
    with pytest.raises(vaulting.VaultError):
        vaulting.Vault(tmp_path / "v.json").create("pw", {"me": {"ssn": "123-45-6789"}}, kdf=bad)
    assert not (tmp_path / "v.json").exists()


def test_utf16_entity_declarations_are_refused(tmp_path):
    from conftest import make_docx
    xml = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE d [<!ENTITY a "aa">]><d>&a;</d>'
    src = tmp_path / "u16.docx"
    make_docx(src)
    with zipfile.ZipFile(src) as zin:
        items = [(i, zin.read(i.filename)) for i in zin.infolist()]
    with zipfile.ZipFile(src, "w") as zout:
        for item, data in items:
            if item.filename == "word/document.xml":
                data = xml.encode("utf-16")
            zout.writestr(item, data)
    with pytest.raises(ValueError):
        for_path(str(src)).discover(str(src))
