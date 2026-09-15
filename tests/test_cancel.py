"""Stopping a turn.

The UI half is easy; the half that matters is that the work actually stops.
Without it, Stop would only stop the browser listening while the machine kept
paying for an answer nobody will read — and the one-turn-per-session guard
would then refuse the user's next question until the abandoned turn finished.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oncallbot.chat.auth import COOKIE, Sessions
from oncallbot.chat.server import create_app
from oncallbot.streaming import CANCEL, Cancelled, cancelled

from conftest import ui

USER = "tester@1mg.com"


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "gmail:\n  account: bot@1mg.com\n  support_address: hr@1mg.com\n"
        f"store:\n  path: {tmp_path / 'x.db'}\n"
        "categories: [other]\n"
        "auth:\n"
        f"  tokens_dir: {tmp_path / 'tokens'}\n"
        f"  stores_dir: {tmp_path / 'users'}\n"
    )
    return cfg


def _client(tmp_path: Path) -> TestClient:
    c = TestClient(create_app(_config(tmp_path)))
    sessions = Sessions(tmp_path / "sessions.sqlite3")
    sid, state = sessions.begin("hr@1mg.com")
    sessions.claim(sid, state, USER)
    c.cookies.set(COOKIE, sid)
    c.app.state.sessions = sessions
    return c


# --- the token -------------------------------------------------------------


def test_cancelled_is_false_outside_a_turn():
    assert cancelled() is False


def test_the_token_is_per_thread(tmp_path: Path):
    """Two turns in flight must not be able to cancel each other."""
    seen: dict[str, bool] = {}

    def worker(name: str, ev: threading.Event | None) -> None:
        CANCEL.set(ev)
        seen[name] = cancelled()

    mine = threading.Event()
    mine.set()
    a = threading.Thread(target=worker, args=("stopped", mine))
    b = threading.Thread(target=worker, args=("running", threading.Event()))
    for t in (a, b):
        t.start()
    for t in (a, b):
        t.join()

    assert seen == {"stopped": True, "running": False}


def test_stream_claude_kills_the_subprocess_when_stopped(monkeypatch):
    """The whole point: the model call ends, it does not run to completion."""
    import oncallbot.streaming as st

    killed: list[bool] = []
    ev = threading.Event()

    class FakeProc:
        returncode = 0

        def __init__(self, cmd, **kw):
            self.stdin = _Sink()
            # Endless output: only a kill ends this.
            self.stdout = self._lines()
            self.stderr = _Sink()

        def _lines(self):
            while True:
                yield (
                    '{"type":"stream_event","event":{"type":"content_block_delta",'
                    '"delta":{"text":"tick "}}}'
                )

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            killed.append(True)

    class _Sink:
        def write(self, _s): pass
        def close(self): pass
        def read(self): return ""

    monkeypatch.setattr(st.shutil, "which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr(st.subprocess, "Popen", FakeProc)

    CANCEL.set(ev)
    try:
        chunks = []
        with pytest.raises(Cancelled):
            for chunk in st.stream_claude("p", "s"):
                chunks.append(chunk)
                if len(chunks) == 3:
                    ev.set()   # the reader goes away
        assert chunks == ["tick ", "tick ", "tick "], "stops where it was told to"
        assert killed == [True], "the subprocess is killed, not left running"
    finally:
        CANCEL.set(None)


# --- through the HTTP layer -------------------------------------------------


def test_a_disconnect_cancels_the_worker(tmp_path: Path, monkeypatch):
    """Closing the stream early must fire the token the model call polls."""
    import oncallbot.chat.server as srv

    saw: dict[str, object] = {}
    started = threading.Event()

    def slow_work():
        yield ("status", "working")
        started.set()
        for _ in range(200):        # ~2s of work if nobody stops it
            if cancelled():
                saw["cancelled"] = True
                return
            time.sleep(0.01)
        saw["cancelled"] = False

    gen = srv._sse_worker(slow_work)
    assert "status" in next(gen)     # start it, read one event
    started.wait(timeout=2)
    gen.close()                      # the reader goes away

    for _ in range(100):
        if "cancelled" in saw:
            break
        time.sleep(0.01)
    assert saw.get("cancelled") is True


def test_a_stopped_turn_reports_no_error(tmp_path: Path):
    """"Stopped" is not a failure, so it must not render as one."""
    import oncallbot.chat.server as srv

    def work():
        yield ("status", "working")
        raise Cancelled("stopped")

    body = "".join(srv._sse_worker(work))
    assert "event: error" not in body
    assert "event: done" in body


def test_a_real_error_still_reports(tmp_path: Path):
    import oncallbot.chat.server as srv

    def work():
        yield ("status", "working")
        raise RuntimeError("the model fell over")

    body = "".join(srv._sse_worker(work))
    assert "event: error" in body
    assert "the model fell over" in body


def test_an_abandoned_turn_frees_its_slot(tmp_path: Path, monkeypatch):
    """Otherwise the user's next question is refused with a 409.

    The release runs in the stream's `finally`, so it fires whether the stream
    ended, raised, or was closed by the reader going away — all three of which
    look the same from the slot's point of view.
    """
    client = _client(tmp_path)
    sid = client.cookies[COOKIE]

    def dies_midway(*_a, **_k):
        yield 'event: status\ndata: "working"\n\n'
        raise RuntimeError("connection lost")

    monkeypatch.setattr("oncallbot.chat.server._stream", dies_midway)

    with pytest.raises(RuntimeError):
        with client.stream("POST", "/api/chat", json={"message": "hi"}) as r:
            list(r.iter_lines())

    assert sid not in client.app.state.turns, "the slot must not be held by a dead turn"

    # And the next question is accepted rather than 409'd.
    monkeypatch.setattr(
        "oncallbot.chat.server._stream",
        lambda *a, **k: iter(['event: done\ndata: {}\n\n']),
    )
    assert client.post("/api/chat", json={"message": "next"}).status_code == 200


def test_the_reader_going_away_frees_the_slot(tmp_path: Path):
    """The Stop case, at the level TestClient can actually observe."""
    import oncallbot.chat.server as srv

    released = threading.Event()

    def work():
        yield ("status", "working")
        for _ in range(300):
            if cancelled():
                released.set()
                return
            time.sleep(0.01)

    gen = srv._sse_worker(work)
    next(gen)
    gen.close()
    assert released.wait(timeout=3), "the worker must see the cancel and stop"


# --- the UI ----------------------------------------------------------------


def test_the_ui_offers_stop_and_means_it(tmp_path: Path):
    html = ui(_client(tmp_path))

    # One owner of what is running, and aborting is what stops the server too.
    assert "let inFlight = null;" in html
    assert "function stopInFlight()" in html
    assert "new AbortController()" in html
    assert "signal: ctl.signal" in html

    # The send button turns into Stop rather than going dead.
    assert "function setSendMode(mode)" in html
    assert "send.textContent = mode === 'stop' ? 'Stop' : 'Send';" in html
    assert "if(busy){ stopInFlight(); return; }" in html

    # Esc works too, and does not steal Esc from the modal.
    assert "if(e.key !== 'Escape' || !backdrop.hidden) return;" in html

    # Asking something new mid-answer stops the old turn and runs the new one.
    assert "function askOrQueue(message)" in html
    assert "askOrQueue(input.value)" in html

    # A stopped turn is not context for a follow-up.
    assert "turnRecord.reply = '(stopped by the user)';" in html
    assert "turnRecord.rows = [];" in html

    # The cards can be stopped as well.
    assert "streamSummary(card, btn, slot, ctl.signal)" in html
    assert "streamDiagnosis(card, dxBtn, slot, ctl.signal)" in html
    assert html.count("_stop = () => ctl.abort();") == 2


def test_the_summarizer_subprocess_is_killed_when_stopped(monkeypatch):
    """A window is a model call per thread; run() could not be interrupted.

    Measured before this: killing the reader left `claude` running to
    completion, so Stop stopped only the listening.
    """
    import oncallbot.summarizer as sm

    ev = threading.Event()
    killed: list[bool] = []

    class Proc:
        returncode = 0

        def communicate(self, input=None, timeout=None):  # noqa: A002
            # Never returns on its own; only a kill ends it.
            while not killed:
                time.sleep(0.01)
            return ("", "")

        def poll(self):
            return 0 if killed else None

        def kill(self):
            killed.append(True)

    monkeypatch.setattr(sm.shutil, "which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr(sm.subprocess, "Popen", lambda cmd, **kw: Proc())

    CANCEL.set(ev)
    try:
        threading.Timer(0.3, ev.set).start()
        with pytest.raises(Cancelled):
            sm._communicate_cancellably(Proc(), "prompt", 60, "t1")
        assert killed == [True]
    finally:
        CANCEL.set(None)


def test_the_summarize_loop_stops_between_threads(monkeypatch):
    """Thirty threads is thirty model calls; the rest is work nobody wants."""
    from oncallbot.config import Config
    from oncallbot.models import EmailMessage, EmailThread
    from oncallbot.pipeline import summarize_threads_stream
    from oncallbot.store import Store

    def th(i):
        m = EmailMessage(id=f"m{i}", thread_id=f"t{i}", date=None, sender="a@b.com",
                         to="", cc="", subject=f"s{i}", body_text="b", snippet="")
        return EmailThread(id=f"t{i}", subject=f"s{i}", messages=[m])

    ev = threading.Event()
    summarized: list[str] = []

    class Slow:
        def summarize(self, thread):
            from oncallbot.models import IssueSummary

            summarized.append(thread.id)
            ev.set()   # stopped after the first one
            return IssueSummary(
                thread_id=thread.id, subject="s", reporter="", last_message_at="",
                summary="", issue="i", category="other", severity="p2",
            )

    import tempfile

    CANCEL.set(ev)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            with Store(Path(tmp) / "s.db") as store:
                with pytest.raises(Cancelled):
                    list(summarize_threads_stream(
                        Config(), [th(1), th(2), th(3)], Slow(), store, total=3
                    ))
        assert summarized == ["t1"], "it must not carry on through the window"
    finally:
        CANCEL.set(None)


def test_a_superseded_turn_does_not_free_the_newer_turns_slot(tmp_path: Path):
    """Two turns unwinding out of order must not leave the slot wrong."""
    import oncallbot.chat.server as srv

    app = srv.create_app(_config(tmp_path))
    sid = "s1"
    older, newer = srv._Turn(), srv._Turn()

    app.state.turns[sid] = newer                      # the newer turn holds it
    list(app.state.release_turn(sid, older, iter(["x"])))   # the older finishes

    assert app.state.turns.get(sid) is newer, "the older turn stole the slot"
    assert older.done.is_set(), "and it still signals that it stopped"


def test_claiming_cancels_the_previous_turn_and_waits_briefly(tmp_path: Path, monkeypatch):
    import oncallbot.chat.server as srv

    app = srv.create_app(_config(tmp_path))
    monkeypatch.setattr(srv, "TURN_HANDOVER_SECONDS", 0.05)
    sid = "s1"
    first, second = srv._Turn(), srv._Turn()

    app.state.claim_turn(sid, first)
    assert app.state.turns[sid] is first
    assert not first.cancel.is_set()

    app.state.claim_turn(sid, second)
    assert first.cancel.is_set(), "the turn in flight is told to stop"
    assert app.state.turns[sid] is second, "and the new turn owns the slot"


def test_a_turn_frees_its_own_slot_when_it_ends(tmp_path: Path):
    import oncallbot.chat.server as srv

    app = srv.create_app(_config(tmp_path))
    sid, turn = "s1", srv._Turn()
    app.state.claim_turn(sid, turn)
    list(app.state.release_turn(sid, turn, iter(["x"])))
    assert sid not in app.state.turns
