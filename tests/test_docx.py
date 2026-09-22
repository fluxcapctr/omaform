"""A Word form: content controls, legacy fields, table cells, underscores."""

import subprocess
import zipfile
from xml.dom import minidom

import pytest

from omaform import plan as planning
from omaform.adapters import for_path
from omaform.adapters import docx as docx_adapter
from omaform.model import BlankKind
from omaform.profile import Profile


def labels(doc) -> dict[str, str]:
    return {b.label.best(): b.id for b in doc.blanks}


def body_text(path) -> str:
    with zipfile.ZipFile(path) as z:
        dom = minidom.parseString(z.read("word/document.xml"))
    return docx_adapter._text(dom)


def test_all_four_flavours_are_found(word_form):
    doc = for_path(word_form).discover(word_form)
    found = labels(doc)
    assert doc.fmt == "docx"
    for wanted in ("Name", "Email address", "Phone", "City", "Street address", "State",
                   "I am a U.S. citizen", "I agree to the terms", "Signature", "Date"):
        assert wanted in found, f"{wanted!r} missing from {sorted(found)}"
    kinds = {b.label.best(): b.kind for b in doc.blanks}
    assert kinds["I am a U.S. citizen"] is BlankKind.CHECKBOX
    assert kinds["I agree to the terms"] is BlankKind.CHECKBOX
    assert kinds["Phone"] is BlankKind.TEXT
    assert all(b.label.section == "APPLICATION FOR SERVICE" for b in doc.blanks)
    # A content control's alias is the form's own word for it.
    assert next(b for b in doc.blanks if b.label.best() == "Phone").label.authoritative


def test_the_planner_fills_a_word_form(word_form):
    doc = for_path(word_form).discover(word_form)
    profile = Profile({"full_name": "Alex Rivera", "email": "alex@example.com",
                       "phone": "555 010 0100", "city": "Springfield", "state": "OR",
                       "address1": "1200 Maple Avenue"})
    result = planning.build(doc, profile)
    got = {e.blank.label.best(): e.value for e in result.filled}
    assert got.get("Name") == "Alex Rivera"
    assert got.get("Email address") == "alex@example.com"
    assert got.get("Phone") == "555 010 0100"
    assert got.get("City") == "Springfield"
    assert got.get("State") == "OR"
    assert got.get("Street address") == "1200 Maple Avenue"
    assert "Date" in got


def test_values_land_in_the_document(word_form, tmp_path):
    adapter = for_path(word_form)
    doc = adapter.discover(word_form)
    ids = labels(doc)
    out = tmp_path / "filled.docx"
    adapter.write(doc, {ids["Name"]: "Alex Rivera", ids["Phone"]: "555 010 0100",
                        ids["City"]: "Springfield", ids["State"]: "OR",
                        ids["I am a U.S. citizen"]: "checked",
                        ids["I agree to the terms"]: "checked"}, str(out))
    text = body_text(out)
    assert "Name: Alex Rivera" in text
    assert "____" not in text.split("Email")[0], "the underscores under Name are gone"
    assert "Email address: ____" in text, "an unfilled line keeps its underscores"
    assert "555 010 0100" in text and "Click here" not in text
    assert "City: Springfield" in text
    assert "OR" in text
    assert "☒" in text, "the content-control box shows ticked"
    with zipfile.ZipFile(out) as z:
        xml = z.read("word/document.xml").decode()
    assert 'w14:val="1"' in xml
    assert '<w:checked w:val="1"/>' in xml
    assert 'mc:Ignorable="w14"' in xml and "xmlns:w14=" in xml, \
        "Word rejects a file whose Ignorable names an undeclared prefix"


def test_writing_twice_from_one_discovery_is_the_same(word_form, tmp_path):
    adapter = for_path(word_form)
    doc = adapter.discover(word_form)
    ids = labels(doc)
    a, b = tmp_path / "a.docx", tmp_path / "b.docx"
    adapter.write(doc, {ids["Name"]: "One"}, str(a))
    adapter.write(doc, {ids["Name"]: "Two"}, str(b))
    assert "Name: One" in body_text(a) and "Name: Two" in body_text(b)
    assert "One" not in body_text(b)


def test_a_signature_becomes_an_inline_picture(word_form, tmp_path):
    from omaform import signature as signing
    adapter = for_path(word_form)
    doc = adapter.discover(word_form)
    ids = labels(doc)
    png = signing.render([[(0, 0), (40, 10), (80, 0)]], width=120, height=40) \
        if hasattr(signing, "render") else None
    if png is None:
        import cairo
        import io
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 120, 40)
        ctx = cairo.Context(surface)
        ctx.move_to(5, 30); ctx.line_to(60, 5); ctx.line_to(115, 30); ctx.stroke()
        buf = io.BytesIO(); surface.write_to_png(buf); png = buf.getvalue()
    out = tmp_path / "signed.docx"
    adapter.write(doc, {}, str(out), images={ids["Signature"]: png})
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        xml = z.read("word/document.xml").decode()
        rels = z.read("word/_rels/document.xml.rels").decode()
        types = z.read("[Content_Types].xml").decode()
    assert "word/media/omaform1.png" in names
    assert "<w:drawing" in xml and 'r:embed="rIdOmaform1"' in xml
    assert "media/omaform1.png" in rels and 'Extension="png"' in types
    assert "Signature: ____" not in body_text(out)


def test_source_is_never_overwritten(word_form):
    adapter = for_path(word_form)
    doc = adapter.discover(word_form)
    with pytest.raises(ValueError):
        adapter.write(doc, {}, word_form)


def test_fingerprint_is_structural(word_form, tmp_path):
    import shutil
    from conftest import DOCX_DOCUMENT, make_docx
    adapter = for_path(word_form)
    a = adapter.discover(word_form).fingerprint
    copy = tmp_path / "copy.docx"
    shutil.copy(word_form, copy)
    assert adapter.discover(str(copy)).fingerprint == a, "the same form is the same form"
    changed = make_docx(tmp_path / "changed.docx",
                        DOCX_DOCUMENT.replace("Email address", "Employer email"))
    assert adapter.discover(changed).fingerprint != a, "a changed label is a changed form"


@pytest.mark.skipif(docx_adapter.soffice() is None, reason="needs LibreOffice")
def test_libreoffice_opens_what_we_wrote(word_form, tmp_path):
    adapter = for_path(word_form)
    doc = adapter.discover(word_form)
    ids = labels(doc)
    out = tmp_path / "filled.docx"
    adapter.write(doc, {ids["Name"]: "Alex Rivera", ids["I agree to the terms"]: "checked"},
                  str(out))
    pdf = docx_adapter.to_pdf(str(out), str(tmp_path / "filled.pdf"))
    text = subprocess.run(["pdftotext", pdf, "-"], capture_output=True, text=True).stdout
    assert "Alex Rivera" in text and "APPLICATION FOR SERVICE" in text


def test_cli_fills_a_word_form(word_form, tmp_path, monkeypatch, capsys):
    from omaform.cli import main
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path / "home"))
    from omaform.profile import Library
    Library(tmp_path / "home").create("Alex Rivera", {"full_name": "Alex Rivera",
                                                      "email": "alex@example.com"})
    out = tmp_path / "out.docx"
    assert main(["fill", word_form, "-o", str(out)]) == 0
    assert "Name: Alex Rivera" in body_text(out)
    assert "alex@example.com" in body_text(out)
