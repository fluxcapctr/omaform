"""The W-4's signature caption hugs a parenthetical, and blanks know their place."""

import pikepdf

from omaform import plan as planning, signature
from omaform.adapters import for_path
from omaform.adapters.pdf import fit_signature, placed_by_hand
from omaform.model import BlankKind
from omaform.profile import Profile


def printed(doc):
    return [b for b in doc.blanks if isinstance(b.native, dict) and b.native.get("synthetic")]


def test_the_w4_gets_its_signature_line_despite_the_parenthetical(w4):
    """'Employee's signature (This form is not valid unless you sign it.)  Date'."""
    doc = for_path(w4).discover(w4)
    found = printed(doc)
    assert [(b.page, b.kind) for b in found] == [(1, BlankKind.SIGNATURE), (1, BlankKind.DATE)]
    sig, date = found
    assert "employee" in sig.label.left.lower()
    assert "sign it" in sig.label.left.lower(), "the parenthetical is part of the label"
    assert 321 < sig.rect[0] < 340, "signing space starts after the closing bracket"
    assert sig.rect[2] < 463 < date.rect[0], "and ends before the printed Date"


def test_an_employers_signature_line_would_be_left_alone(w4):
    """The possessive before 'signature' reaches the matcher for exactly this."""
    from omaform.model import Blank, LabelContext
    from omaform.matching import match_blank

    blank = Blank(id="b", kind=BlankKind.SIGNATURE, width_pt=200,
                  label=LabelContext(left="Employer's signature (required)"))
    assert match_blank(blank, Profile({}, {"signature": "x"})) is None
    mine = Blank(id="m", kind=BlankKind.SIGNATURE, width_pt=200,
                 label=LabelContext(left="Employee's signature (This form is not valid "
                                         "unless you sign it.)"))
    assert match_blank(mine, Profile({}, {"signature": "x"})).key == "signature"


def test_signing_a_w4_places_the_image_and_the_date(w4):
    doc = for_path(w4).discover(w4)
    png = signature.render_png([[(0, 0), (60, 25)]])
    result = planning.build(doc, Profile({"first_name": "Alex", "last_name": "Rivera"},
                                         {"ssn": "123-45-6789",
                                          "signature": signature.encode(png)}),
                            prefer={"ssn"})
    sig, date = printed(doc)
    assert result.images() == {sig.id: png}
    assert date.id in result.values()


def test_every_blank_knows_where_it_is(w9):
    doc = for_path(w9).discover(w9)
    assert all(b.rect is not None for b in doc.blanks)
    x0, y0, x1, y1 = doc.blanks[0].rect
    assert x0 < x1 and y0 < y1


def test_signature_fit_is_a_multiple_of_a_field_but_exact_for_a_line():
    x, y, w, h = fit_signature((100, 400, 400, 413), aspect=4.0)
    assert h == 13 * 2.6 and w == h * 4 and (x, y) == (103, 401)
    x, y, w, h = fit_signature((100, 400, 400, 422), aspect=4.0, exact=True)
    assert h == 22 and w == 88
    # never wider than the box, however wide the image
    _, _, w, h = fit_signature((100, 400, 160, 413), aspect=20.0)
    assert w <= 60 and h == w / 20


def test_a_blank_placed_by_hand_is_written_where_it_was_put(w4, tmp_path):
    doc = for_path(w4).discover(w4)
    hand = placed_by_hand(2, BlankKind.SIGNATURE, x=120.0, y=300.0)
    doc.blanks.append(hand)
    png = signature.render_png([[(0, 0), (60, 25)]])
    for_path(w4).write(doc, {}, str(tmp_path / "out.pdf"), images={hand.id: png})
    written = pikepdf.open(str(tmp_path / "out.pdf"))  # bound: a temporary is collected mid-expression
    page = written.pages[1]
    ours = [k for k in page.Resources.XObject.keys() if str(k).startswith("/OmaSig")]
    assert len(ours) == 1
    content = b"".join(s.read_bytes() for s in
                       (page.Contents if isinstance(page.Contents, pikepdf.Array)
                        else [page.Contents]))
    assert b"123.00 301.00 cm" in content, "bottom-left is the point that was clicked, inset"


def test_text_and_a_tick_placed_by_hand_are_written(w4, tmp_path):
    doc = for_path(w4).discover(w4)
    text = placed_by_hand(1, BlankKind.TEXT, x=100.0, y=500.0)
    tick = placed_by_hand(1, BlankKind.CHECKBOX, x=300.0, y=500.0)
    doc.blanks += [text, tick]
    for_path(w4).write(doc, {text.id: "hello there", tick.id: "checked"},
                       str(tmp_path / "out.pdf"))
    import subprocess
    shown = subprocess.run(["pdftotext", "-layout", "-f", "1", "-l", "1",
                            str(tmp_path / "out.pdf"), "-"],
                           capture_output=True, text=True, check=True).stdout
    assert "hello there" in " ".join(shown.split())
    written = pikepdf.open(str(tmp_path / "out.pdf"))
    page = written.pages[0]
    content = b"".join(s.read_bytes() for s in
                       (page.Contents if isinstance(page.Contents, pikepdf.Array)
                        else [page.Contents]))
    assert b" l S Q" in content, "the tick is a drawn path"
