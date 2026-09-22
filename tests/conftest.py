import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def w9() -> str:
    path = FIXTURES / "fw9.pdf"
    if not path.exists():
        pytest.skip("fixtures missing; run tests/fixtures/fetch.sh")
    return str(path)


@pytest.fixture
def w4() -> str:
    path = FIXTURES / "fw4.pdf"
    if not path.exists():
        pytest.skip("fixtures missing; run tests/fixtures/fetch.sh")
    return str(path)


@pytest.fixture
def i9() -> str:
    path = FIXTURES / "i-9.pdf"
    if not path.exists():
        pytest.skip("fixtures missing; run tests/fixtures/fetch.sh")
    return str(path)


@pytest.fixture
def library(tmp_path, monkeypatch):
    """A Library rooted in a temp directory, never the user's real one."""
    from omaform.profile import Library

    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    return Library(tmp_path)


@pytest.fixture
def sample_profile():
    from omaform.profile import Profile

    return Profile({
        "full_name": "Eric Stevens",
        "business_name": "Eric Stevens Design",
        "address1": "1200 Maple Avenue Apt 4",
        "city": "Springfield", "state": "OR", "zip": "97403",
        "phone": "512-555-0142", "email": "e@example.com",
    }, {"ssn": "123-45-6789", "ein": "12-3456789"})


import cairo  # noqa: E402

W, H = 612, 792


@pytest.fixture
def flat_form(tmp_path):
    """Drawn with cairo: a Word-style form as most people are sent."""
    path = tmp_path / "flat.pdf"
    surface = cairo.PDFSurface(str(path), W, H)
    ctx = cairo.Context(surface)
    ctx.select_font_face("Helvetica")
    ctx.set_font_size(11)
    ctx.set_source_rgb(0, 0, 0)

    def text(x, y, s):
        ctx.move_to(x, y)
        ctx.show_text(s)

    def rule(x0, x1, y):
        ctx.set_line_width(0.8)
        ctx.move_to(x0, y)
        ctx.line_to(x1, y)
        ctx.stroke()

    text(72, 80, "Vendor Information Sheet")
    text(72, 130, "Name:")
    rule(115, 340, 133)
    text(72, 170, "Email address:")
    rule(160, 340, 173)
    text(72, 210, "Phone: ____________________")
    text(360, 130, "Company:")
    rule(415, 560, 133)
    # a separator across the page, which is not a blank
    rule(72, 560, 260)
    text(72, 300, "Notes")
    surface.finish()
    return str(path)




# -- a Word form with all four kinds of blank ---------------------------------

import zipfile  # noqa: E402

_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_W14 = 'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"'
_MC = 'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"'


def _p(text: str, style: str = "") -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def _sdt_text(alias: str) -> str:
    return (f'<w:sdt><w:sdtPr><w:alias w:val="{alias}"/><w:tag w:val="{alias.lower()}"/>'
            '<w:showingPlcHdr/></w:sdtPr><w:sdtContent><w:r><w:t>Click here to enter text.</w:t>'
            '</w:r></w:sdtContent></w:sdt>')


def _sdt_box(alias: str) -> str:
    return (f'<w:sdt><w:sdtPr><w:alias w:val="{alias}"/><w14:checkbox><w14:checked w14:val="0"/>'
            '</w14:checkbox></w:sdtPr><w:sdtContent><w:r><w:t>☐</w:t></w:r></w:sdtContent></w:sdt>')


def _legacy_text(label: str) -> str:
    return (f'<w:p><w:r><w:t xml:space="preserve">{label}: </w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="Text1"/>'
            '<w:textInput/></w:ffData></w:fldChar></w:r>'
            '<w:r><w:instrText xml:space="preserve"> FORMTEXT </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            '<w:r><w:t>     </w:t></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>')


def _legacy_box(label: str) -> str:
    return ('<w:p><w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="Check1"/>'
            '<w:checkBox><w:sizeAuto/><w:default w:val="0"/></w:checkBox></w:ffData></w:fldChar></w:r>'
            '<w:r><w:instrText xml:space="preserve"> FORMCHECKBOX </w:instrText></w:r>'
            '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
            f'<w:r><w:t xml:space="preserve"> {label}</w:t></w:r></w:p>')


def _cell(text: str) -> str:
    return f'<w:tc><w:tcPr><w:tcW w:w="4000" w:type="dxa"/></w:tcPr>{_p(text) if text else "<w:p/>"}</w:tc>'


DOCX_BODY = "".join([
    _p("APPLICATION FOR SERVICE", "Heading1"),
    _p("Name: ____________________"),
    _p("Email address: ______________________"),
    "<w:p><w:r><w:t xml:space=\"preserve\">Phone: </w:t></w:r>" + _sdt_text("Phone") + "</w:p>",
    _legacy_text("City"),
    "<w:tbl><w:tblPr/><w:tblGrid><w:gridCol w:w=\"4000\"/><w:gridCol w:w=\"4000\"/></w:tblGrid>"
    f"<w:tr>{_cell('Street address')}{_cell('')}</w:tr>"
    f"<w:tr>{_cell('State')}{_cell('')}</w:tr>"
    "</w:tbl>",
    "<w:p>" + _sdt_box("I am a U.S. citizen") + "<w:r><w:t xml:space=\"preserve\"> I am a U.S. citizen</w:t></w:r></w:p>",
    _legacy_box("I agree to the terms"),
    _p("Signature: ______________________   Date: ____________"),
])

DOCX_DOCUMENT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:document {_W} {_W14} {_MC} mc:Ignorable="w14"><w:body>{DOCX_BODY}'
    '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr></w:body></w:document>')

DOCX_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '</Types>')

DOCX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
    '</Relationships>')

DOCX_DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '</Relationships>')


def make_docx(path, body_xml: str = DOCX_DOCUMENT) -> str:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", DOCX_TYPES)
        z.writestr("_rels/.rels", DOCX_RELS)
        z.writestr("word/document.xml", body_xml)
        z.writestr("word/_rels/document.xml.rels", DOCX_DOC_RELS)
    return str(path)


@pytest.fixture
def word_form(tmp_path):
    return make_docx(tmp_path / "application.docx")
