"""Black-outs remove, they do not cover."""

import subprocess

import pikepdf
import pytest

from omaform import redact
from omaform.cli import main


def text_of(path) -> str:
    return subprocess.run(["pdftotext", "-layout", str(path), "-"],
                          capture_output=True, text=True, check=True).stdout


def test_find_text_ignores_punctuation_and_case(w9):
    hits = redact.find_text(w9, "social-security NUMBER")
    assert hits and hits[0].page == 1
    x0, y0, x1, y1 = hits[0].rect
    assert x1 > x0 and y1 > y0


def test_the_words_are_gone_and_the_rest_stays(w9, tmp_path):
    out = tmp_path / "out.pdf"
    regions = redact.find_text(w9, "Social security number")
    touched = redact.apply(w9, regions, str(out))
    assert touched == len({r.page for r in regions})
    text = text_of(out)
    assert "Social security number" not in text
    assert "Specific Instructions" in text   # an untouched page keeps its text
    assert len(pikepdf.open(str(out)).pages) == 6


def test_a_blacked_out_page_has_no_fields_or_text_layer(w9, tmp_path):
    out = tmp_path / "out.pdf"
    redact.apply(w9, [redact.Region(1, (0, 0, 612, 792))], str(out))
    pdf = pikepdf.open(str(out))
    assert "/Annots" not in pdf.pages[0].obj
    assert "/AcroForm" not in pdf.Root      # every W-9 field was on page 1
    assert text_of(out).split("\f")[0].strip() == ""


def test_region_parse():
    r = redact.Region.parse("2: 300,100, 10,50")
    assert r == redact.Region(2, (10.0, 50.0, 300.0, 100.0))
    with pytest.raises(ValueError):
        redact.Region.parse("nope")


def test_source_is_never_overwritten(w9):
    with pytest.raises(ValueError):
        redact.apply(w9, [redact.Region(1, (0, 0, 10, 10))], w9)


def test_cli_redact(w9, tmp_path, capsys):
    out = tmp_path / "cli.pdf"
    assert main(["redact", str(out), w9, "--text", "Employer identification number",
                 "--box", "6:0,0,612,792"]) == 0
    assert "wrote" in capsys.readouterr().out
    assert "Employer identification number" not in text_of(out)
