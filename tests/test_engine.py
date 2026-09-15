"""Choosing which model answers, per session.

The rule that matters: only offer what would actually work, and say plainly
where a choice is not safe for the job.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oncallbot.chat.auth import COOKIE, Sessions
from oncallbot.chat.server import available_engines, create_app
from oncallbot.config import load_config

from conftest import ui


def _config(tmp_path: Path, extra: str = "") -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "gmail:\n  account: bot@1mg.com\n  support_address: hr@1mg.com\n"
        f"store:\n  path: {tmp_path / 'x.db'}\n"
        "categories: [other]\n"
        "auth:\n"
        f"  tokens_dir: {tmp_path / 'tokens'}\n"
        f"  stores_dir: {tmp_path / 'users'}\n" + extra
    )
    return cfg


def _client(tmp_path: Path, extra: str = "") -> TestClient:
    c = TestClient(create_app(_config(tmp_path, extra)))
    sessions = Sessions(tmp_path / "sessions.sqlite3")
    sid, state = sessions.begin("hr@1mg.com")
    sessions.claim(sid, state, "tester@1mg.com")
    c.cookies.set(COOKIE, sid)
    c.app.state.sessions = sessions
    return c


# --- what is offered --------------------------------------------------------


def test_a_backend_without_its_prerequisite_is_offered_as_unavailable(
    tmp_path: Path, monkeypatch
):
    """Not hidden: "why can I not pick that" deserves an answer."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _n: None)
    monkeypatch.setattr("oncallbot.chat.server._local_models", lambda c: [])

    engines = available_engines(load_config(_config(tmp_path)))
    by_id = {e["id"]: e for e in engines}

    assert by_id["claude_cli"]["available"] is False
    assert "not on PATH" in by_id["claude_cli"]["why"]
    assert by_id["anthropic_api"]["available"] is False
    assert "ANTHROPIC_API_KEY" in by_id["anthropic_api"]["why"]


def test_local_models_come_from_asking_the_server(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "oncallbot.chat.server._local_models",
        lambda c: ["phi4-mini:latest", "llama3.2:latest"],
    )
    engines = available_engines(load_config(_config(tmp_path)))
    local = [e for e in engines if e["id"] == "local"]

    assert [e["model"] for e in local] == ["phi4-mini:latest", "llama3.2:latest"]
    assert all(e["available"] for e in local)
    assert all("Local · " in e["label"] for e in local)


def test_a_local_choice_carries_the_warning_that_matters(tmp_path: Path, monkeypatch):
    """Measured, not assumed: on a real thread phi4-mini routed correctly and
    then put a redaction placeholder in order_ids and two patient names in one
    patient_id. Those ids drive Diagnose and the copy chips."""
    monkeypatch.setattr(
        "oncallbot.chat.server._local_models", lambda c: ["phi4-mini:latest"]
    )
    local = [
        e for e in available_engines(load_config(_config(tmp_path)))
        if e["id"] == "local"
    ]
    assert "good for routing" in local[0]["note"]
    assert "mis-extracting ids" in local[0]["note"]


def test_no_local_server_means_no_local_options(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("oncallbot.chat.server._local_models", lambda c: [])
    assert not [
        e for e in available_engines(load_config(_config(tmp_path)))
        if e["id"] == "local"
    ]


def test_probing_a_dead_local_server_is_not_an_error(tmp_path: Path):
    from oncallbot.chat import server as srv

    cfg = load_config(_config(tmp_path, "local:\n  base_url: http://127.0.0.1:9\n"))
    assert srv._local_models(cfg) == []


# --- choosing ---------------------------------------------------------------


def test_the_choice_applies_to_this_session_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "oncallbot.chat.server._local_models", lambda c: ["phi4-mini:latest"]
    )
    client = _client(tmp_path)

    assert client.get("/api/context").json()["engine"]["backend"] == "claude_cli"

    r = client.post("/api/session",
                    json={"backend": "local", "model": "phi4-mini:latest"})
    assert r.status_code == 200

    engine = client.get("/api/context").json()["engine"]
    assert engine == {"backend": "local", "model": "phi4-mini:latest"}

    # config.yaml is untouched: another session still gets the default.
    assert load_config(_config(tmp_path)).summarizer.backend == "claude_cli"


def test_an_unknown_backend_is_refused(tmp_path: Path):
    client = _client(tmp_path)
    r = client.post("/api/session", json={"backend": "gpt-guess"})
    assert r.status_code == 422
    assert "Unknown backend" in r.json()["detail"]
    assert "claude_cli" in r.json()["detail"], "it lists what is valid"


def test_the_choice_survives_a_restart(tmp_path: Path, monkeypatch):
    """It is a preference, not a credential, so it is on disk with the session."""
    monkeypatch.setattr(
        "oncallbot.chat.server._local_models", lambda c: ["phi4-mini:latest"]
    )
    client = _client(tmp_path)
    sid = client.cookies[COOKIE]
    client.post("/api/session", json={"backend": "local", "model": "phi4-mini:latest"})

    fresh = TestClient(create_app(_config(tmp_path)))
    fresh.cookies.set(COOKIE, sid)
    assert fresh.get("/api/context").json()["engine"]["model"] == "phi4-mini:latest"


def test_a_sessions_file_from_before_the_picker_is_migrated(tmp_path: Path):
    import sqlite3
    import time

    path = tmp_path / "sessions.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_email TEXT NOT NULL, "
        "group_email TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, "
        "last_seen_at REAL NOT NULL, pending_state TEXT NOT NULL DEFAULT '');"
    )
    old.execute("INSERT INTO sessions VALUES ('s1', 'a@1mg.com', '', ?, ?, '')",
                (time.time(), time.time()))
    old.commit()
    old.close()

    s = Sessions(path)
    assert s.engine_for("s1") == ("", "")
    s.set_engine("s1", "local", "phi4-mini:latest")
    assert s.engine_for("s1") == ("local", "phi4-mini:latest")


def test_single_user_mode_can_choose_too(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "oncallbot.chat.server._local_models", lambda c: ["phi4-mini:latest"]
    )
    client = TestClient(create_app(_config(tmp_path, "  enabled: false\n")))
    client.post("/api/session", json={"backend": "local", "model": "phi4-mini:latest"})
    assert client.get("/api/context").json()["engine"]["model"] == "phi4-mini:latest"


# --- the picker -------------------------------------------------------------


def test_the_composer_has_the_picker(tmp_path: Path):
    html = ui(_client(tmp_path))
    assert '<select id="engine"' in html
    assert "function renderEngines(list, current)" in html
    assert "o.disabled = !e.available;" in html, "an unavailable option is not selectable"
    assert "o.title = e.why || e.note" in html, "and says why"


def test_the_choice_is_remembered_and_reconciled(tmp_path: Path):
    html = ui(_client(tmp_path))
    assert "localStorage.setItem(ENGINE_KEY" in html
    # A remembered model that has since gone must not leave the client and
    # the server disagreeing about which one is answering.
    assert "if (pick && engineValue(pick) !== currentValue) applyEngine(pick, true);" in html


def test_the_choice_sticks_where_there_is_no_session_row(tmp_path: Path):
    """Single-user mode has a session id but no row: a plain UPDATE matched
    nothing and the choice was silently dropped."""
    s = Sessions(tmp_path / "sessions.sqlite3")
    assert s.engine_for("local") == ("", "")
    s.set_engine("local", "local", "phi4-mini:latest")
    assert s.engine_for("local") == ("local", "phi4-mini:latest")


def test_the_upsert_does_not_create_a_usable_session(tmp_path: Path):
    """The row it inserts has no user, so it cannot be mistaken for a login."""
    from oncallbot.chat.auth import AuthError

    s = Sessions(tmp_path / "sessions.sqlite3")
    s.set_engine("local", "local", "phi4-mini:latest")
    with pytest.raises(AuthError):
        s.get("local", load_config(_config(tmp_path)))


# --- the per-model note ----------------------------------------------------


def test_a_measured_model_gets_its_own_note():
    """One blanket warning claimed gemma3 mis-extracts ids. It does not: on a
    real thread it returned the patient, order and record ids correctly."""
    from oncallbot.chat.server import _local_note

    assert "mis-extracting" in _local_note("phi4-mini:latest")
    assert "mis-extracting" not in _local_note("gemma3:latest")
    assert "measured good" in _local_note("gemma3:latest")


def test_an_unmeasured_model_says_so_rather_than_reassuring():
    from oncallbot.chat.server import _local_note

    note = _local_note("some-new-model:7b")
    assert "not measured" in note


def test_the_note_ignores_the_tag():
    """gemma3:latest and gemma3:12b are the same measurement."""
    from oncallbot.chat.server import _local_note

    assert _local_note("gemma3:12b") == _local_note("gemma3:latest")
    assert _local_note("GEMMA3") == _local_note("gemma3:latest")
