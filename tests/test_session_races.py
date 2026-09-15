"""A live session must never be reported as logged out.

Reported from the UI: signed out mid-answer and bounced to the login page,
with nothing expired. One `sqlite3.Connection` was shared by every request
thread and by the turn worker with `check_same_thread=False` and no
serialization -- and while the model streams, the UI concurrently calls
/api/context, /api/suggest and /api/session on that same connection.

Concurrent `execute` on one connection interleaves: the cursor a reader is
holding is invalidated by another thread's statement, `fetchone()` comes back
empty, and `get` reads that as "no such session" -- a 401, which the UI
correctly treats as a dead session.

Measured before the lock, six readers against four writers for six seconds:
57,517 spurious logouts and 14,957 `InterfaceError: bad parameter or other
API misuse`. After: zero of each.
"""

import threading
import time
from pathlib import Path

import pytest

from oncallbot.chat.auth import AuthError, Sessions
from oncallbot.config import load_config


@pytest.fixture
def cfg():
    c = load_config(Path("config.example.yaml"))
    c.auth.enabled = True
    return c


@pytest.fixture
def store(tmp_path: Path):
    s = Sessions(tmp_path / "sessions.sqlite3")
    yield s
    s.close()


def test_a_live_session_survives_concurrent_use(store, cfg):
    """The regression, in miniature: readers and writers at the same time."""
    sid, state = store.begin("hr@1mg.com")
    store.claim(sid, state, "raviraj.singh@1mg.com")

    logouts: list[str] = []
    errors: list[str] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                store.get(sid, cfg)
            except AuthError as exc:
                logouts.append(exc.reason)
            except Exception as exc:  # noqa: BLE001 - any of these is the bug
                errors.append(f"{type(exc).__name__}: {exc}")

    def writer(n: int):
        while not stop.is_set():
            try:
                store.set_engine(sid, "local", f"gemma3:{n}")
                store.engine_for(sid)
                store.set_group(sid, "hr@1mg.com")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=reader) for _ in range(6)]
    threads += [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not logouts, f"a valid session was reported logged out: {logouts[:3]}"
    assert not errors, f"the connection was used unsafely: {errors[:3]}"


def test_the_session_is_still_usable_afterwards(store, cfg):
    """Serializing must not leave the row half-written."""
    sid, state = store.begin("hr@1mg.com")
    store.claim(sid, state, "raviraj.singh@1mg.com")

    def hammer():
        for _ in range(50):
            store.set_engine(sid, "local", "gemma3:latest")

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    user = store.get(sid, cfg)
    assert user.email == "raviraj.singh@1mg.com"
    assert store.engine_for(sid) == ("local", "gemma3:latest")


def test_an_expired_session_is_still_rejected(store, cfg):
    """The lock must not turn the expiry check off."""
    sid, state = store.begin("hr@1mg.com")
    store.claim(sid, state, "raviraj.singh@1mg.com")
    store._run(
        "UPDATE sessions SET last_seen_at = ? WHERE id = ?",
        (time.time() - cfg.auth.idle_hours * 3600 - 60, sid),
    )
    with pytest.raises(AuthError):
        store.get(sid, cfg)


def test_an_unknown_session_is_still_rejected(store, cfg):
    with pytest.raises(AuthError):
        store.get("no-such-session", cfg)
