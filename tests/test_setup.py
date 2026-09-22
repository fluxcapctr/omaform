"""Setup: the doctor, settings, recent forms and the agent's status."""

import json
import shutil

from omaform import doctor, llm
from omaform.cli import main
from omaform.profile import Library


def test_settings_round_trip_and_keep_the_default(tmp_path):
    lib = Library(tmp_path)
    alex = lib.create("Alex Rivera")
    lib.set_setting("llm_backend", "ollama")
    lib.set_setting("onboarded", True)
    assert lib.setting("llm_backend") == "ollama" and lib.setting("onboarded") is True
    assert lib.default_slug() == alex.slug
    assert lib.setting("missing", "x") == "x"


def test_doctor_json_says_what_it_found(tmp_path, monkeypatch):
    monkeypatch.setenv("OMAFORM_HOME", str(tmp_path))
    data = json.loads(doctor.report(as_json=True))
    names = {c["name"] for c in data["checks"]}
    assert {"GTK 4 and libadwaita", "Poppler", "Omarchy agent", "Vault"} <= names
    assert isinstance(data["ready"], bool) and "version" in data
    assert all("sudo" not in c["fix"] for c in data["checks"]), \
        "fixes are printed for the person to run, as themselves or as root"


def test_agent_status_explains_each_case(monkeypatch):
    monkeypatch.setattr(llm, "default_agent", lambda: None)
    assert not llm.agent_status()["usable"]
    assert "omarchy default agent" in llm.agent_status()["why"]
    monkeypatch.setattr(llm, "default_agent", lambda: "muse")
    assert "no mode" in llm.agent_status()["why"]
    monkeypatch.setattr(llm, "default_agent", lambda: "claude")
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    status = llm.agent_status()
    assert not status["usable"] and "not installed" in status["why"]
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/usr/bin/" + name)
    status = llm.agent_status()
    assert status["usable"] and status["label"] == "Claude Code" and status["remote"]


def test_recent_lists_forms_newest_first_and_skips_filled_copies(w9, tmp_path, capsys):
    import os
    import time
    older = tmp_path / "older.pdf"
    shutil.copy(w9, older)
    os.utime(older, (time.time() - 100, time.time() - 100))
    shutil.copy(w9, tmp_path / "newer.pdf")
    shutil.copy(w9, tmp_path / "newer-filled.pdf")
    (tmp_path / "notes.txt").write_text("not a form")
    assert main(["recent", str(tmp_path), "--json"]) == 0
    items = json.loads(capsys.readouterr().out)
    assert [i["name"] for i in items] == ["newer.pdf", "older.pdf"]
    assert all(i["blanks"] > 3 for i in items)
