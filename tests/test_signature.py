"""Strokes become a trimmed, transparent PNG; the planner never types one."""

import io

import cairo
import pytest

from omaform import signature


def surface_of(png: bytes) -> cairo.ImageSurface:
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def alpha_at(surface: cairo.ImageSurface, x: int, y: int) -> int:
    surface.flush()
    stride = surface.get_stride()
    data = surface.get_data()
    return data[y * stride + x * 4 + 3]


def test_nothing_drawn_gives_nothing():
    assert signature.render_png([]) is None
    assert signature.render_png([[]]) is None


def test_a_stroke_renders_ink_on_a_transparent_ground():
    png = signature.render_png([[(10, 10), (60, 40), (120, 12)]])
    assert png and png.startswith(b"\x89PNG")
    surface = surface_of(png)
    assert alpha_at(surface, 0, 0) == 0, "the corner must be transparent"
    w, h = surface.get_width(), surface.get_height()
    inked = any(alpha_at(surface, x, y) > 0
                for x in range(0, w, 3) for y in range(0, h, 3))
    assert inked


def test_output_is_trimmed_to_the_ink_not_the_canvas():
    # A short stroke far from the origin must not produce a huge mostly-empty image.
    png = signature.render_png([[(500, 180), (540, 190)]], scale=1, margin=10)
    surface = surface_of(png)
    assert surface.get_width() <= 40 + 2 * 10 + 4
    assert surface.get_height() <= 10 + 2 * 10 + 4


def test_a_single_tap_is_a_dot_not_nothing():
    png = signature.render_png([[(30, 30)]], scale=1)
    assert png is not None
    surface = surface_of(png)
    assert alpha_at(surface, surface.get_width() // 2, surface.get_height() // 2) > 0


def test_export_scale_multiplies_pixels():
    one = surface_of(signature.render_png([[(0, 0), (100, 0)]], scale=1))
    three = surface_of(signature.render_png([[(0, 0), (100, 0)]], scale=3))
    assert three.get_width() == pytest.approx(one.get_width() * 3, abs=3)


def test_encode_decode_round_trip_and_rejects_non_png():
    png = signature.render_png([[(0, 0), (10, 10)]])
    assert signature.decode(signature.encode(png)) == png
    assert signature.decode("not base64 at all!") is None
    assert signature.decode(signature.encode(b"plain text")) is None


def test_the_vault_keeps_a_signature_per_identity(tmp_path):
    import os
    from omaform.vault import Kdf, Vault

    v = Vault(tmp_path / "v.enc")
    v.create("pw", kdf=Kdf(salt=os.urandom(16), time_cost=1, memory_cost=32,
                            parallelism=1))
    png = signature.render_png([[(0, 0), (40, 20)]])
    v.set("me", "signature", signature.encode(png))
    v.lock()
    assert Vault(v.path).keys_for("me") == ["signature"]
    v.unlock("pw")
    assert signature.decode(v.get("me", "signature")) == png


def _i9_plan(with_signature: bool):
    from omaform import plan as planning
    from omaform.adapters import for_path
    from omaform.profile import Profile

    path = "tests/fixtures/i-9.pdf"
    once = {"ssn": "123-45-6789"}
    if with_signature:
        once["signature"] = signature.encode(
            signature.render_png([[(0, 0), (60, 25), (120, 5), (180, 30)]]))
    profile = Profile({"full_name": "Eric Stevens"}, once)
    doc = for_path(path).discover(path)
    return doc, planning.build(doc, profile, prefer={"ssn"})


def test_a_signature_is_placed_as_an_image_never_typed():
    """The I-9 asks for the signature in a plain text field. A base64 blob in
    there is not a signature, so it goes on the page as an image instead."""
    _doc, result = _i9_plan(with_signature=True)
    for entry in result.filled:
        if entry.value is not None:
            assert entry.match.key != "signature"
            assert len(entry.value) < 100, "a blob leaked into a text field"
    placed = [e for e in result.filled if e.image is not None]
    assert len(placed) == 1, "exactly the employee's own signature line"
    assert placed[0].match.key == "signature"
    assert "signature" not in result.values(), "images are not text values"
    assert set(result.images()) == {placed[0].blank.id}
    assert "signature" not in result.missing()
    assert "signature" in result.sensitive_used


def test_only_your_own_signature_line_is_signed():
    """The I-9 has employer and preparer signature lines too, on pages 2 to 4.

    Judged by the widget, not by scanning its tooltip for party words: the
    employee's own tooltip mentions the preparer in an aside, and that aside
    is precisely what the sentence-aware veto is there to ignore.
    """
    _doc, result = _i9_plan(with_signature=True)
    placed = [e for e in result.entries if e.image is not None]
    assert [e.blank.label.native_name for e in placed] == ["Signature of Employee"]
    assert all(e.blank.page == 1 for e in placed)


def test_a_form_that_wants_a_signature_you_have_not_drawn_says_so():
    _doc, result = _i9_plan(with_signature=False)
    assert "signature" in result.missing()


def test_the_image_lands_on_the_page_and_the_widget_is_retired(tmp_path):
    import pikepdf
    from omaform.adapters import for_path

    doc, result = _i9_plan(with_signature=True)
    [blank_id] = result.images()
    page_no = int(blank_id.split(":", 1)[0][1:])
    out = tmp_path / "signed.pdf"
    source = pikepdf.open(doc.path)  # bound, or the temporary is collected mid-expression
    widgets_before = len(source.pages[page_no - 1].Annots)

    for_path(doc.path).write(doc, result.values(), str(out), images=result.images())

    after = pikepdf.open(str(out))
    page = after.pages[page_no - 1]
    xobjects = page.Resources.get("/XObject", {})
    ours = [k for k in xobjects.keys() if str(k).startswith("/OmaSig")]
    assert len(ours) == 1
    image = xobjects[ours[0]]
    assert image.Subtype == pikepdf.Name.Image and "/SMask" in image, \
        "transparency must survive, or the signature sits in a white box"
    contents = page.Contents if isinstance(page.Contents, pikepdf.Array) else [page.Contents]
    content = b"".join(s.read_bytes() for s in contents)
    assert ours[0].encode() in content and b" Do" in content
    assert len(page.Annots) == widgets_before - 1, "the signature widget stays put"
    assert after.Root.AcroForm.NeedAppearances is True, "text fields still render"


def test_writing_twice_does_not_stamp_twice(tmp_path):
    """write() must work on a fresh copy: the UI can press Fill again."""
    import pikepdf
    from omaform.adapters import for_path

    doc, result = _i9_plan(with_signature=True)
    [blank_id] = result.images()
    page_no = int(blank_id.split(":", 1)[0][1:])
    for name in ("one.pdf", "two.pdf"):
        for_path(doc.path).write(doc, result.values(), str(tmp_path / name),
                                 images=result.images())
    second = pikepdf.open(str(tmp_path / "two.pdf"))
    page = second.pages[page_no - 1]
    ours = [k for k in page.Resources.XObject.keys() if str(k).startswith("/OmaSig")]
    assert len(ours) == 1


def test_the_signature_is_visible_where_the_line_was(tmp_path):
    """Render the signed page and look for ink inside the field's rectangle."""
    import subprocess

    from PIL import Image
    from omaform.adapters import for_path

    doc, result = _i9_plan(with_signature=True)
    [blank_id] = result.images()
    page_no = int(blank_id.split(":", 1)[0][1:])
    blank = doc.by_id(blank_id)
    x0, y0, x1, y1 = (float(v) for v in blank.native.Rect)
    page_h = float(doc.native.pages[page_no - 1].MediaBox[3])

    out = tmp_path / "signed.pdf"
    for_path(doc.path).write(doc, result.values(), str(out), images=result.images())
    subprocess.run(["pdftoppm", "-f", str(page_no), "-l", str(page_no), "-r", "72",
                    "-png", str(out), str(tmp_path / "page")], check=True)
    [png] = list(tmp_path.glob("page*.png"))
    image = Image.open(png).convert("L")
    # 72dpi: one point is one pixel, and y flips from PDF space to image space.
    region = image.crop((int(x0), int(page_h - y1 - 40), int(x1), int(page_h - y0)))
    dark = sum(1 for v in region.getdata() if v < 90)
    assert dark > 40, "no ink where the signature should be"
