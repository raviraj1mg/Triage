"""Browser login: sessions, cookies, and what a 401 tells the UI.

The rules being pinned here are the ones a reviewer should not have to infer:
no credential reaches the page, a callback cannot be replayed, and a missing
admin token is a different failure from a dead session.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oncallbot.chat.auth import COOKIE, AuthError, Sessions, user_slug
from oncallbot.chat.server import create_app
from oncallbot.config import AuthConfig, load_config
from oncallbot.store import Store

from conftest import ui

USER = "tester@1mg.com"


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


def _app(tmp_path: Path, extra: str = "") -> TestClient:
    return TestClient(create_app(_config(tmp_path, extra)))


def _sessions(tmp_path: Path) -> Sessions:
    return Sessions(tmp_path / "sessions.sqlite3")


def _logged_in(tmp_path: Path, client: TestClient, email: str = USER) -> Sessions:
    s = _sessions(tmp_path)
    sid, state = s.begin("hr@1mg.com")
    s.claim(sid, state, email)
    client.cookies.set(COOKIE, sid)
    client.app.state.sessions = s
    return s


# --- the domain gate -------------------------------------------------------


def test_only_the_allowed_domain_gets_in():
    a = AuthConfig()
    assert a.permits("raviraj.singh@1mg.com")
    assert not a.permits("someone@gmail.com")
    assert not a.permits("1mg.com")           # not an address
    assert not a.permits("")


def test_an_explicit_allowlist_wins_over_the_domain():
    a = AuthConfig(allowed_emails=["a@1mg.com"])
    assert a.permits("A@1MG.COM")             # case-insensitive
    assert not a.permits("b@1mg.com"), "on the domain but not on the list"


def test_a_lookalike_domain_is_refused():
    a = AuthConfig()
    assert not a.permits("attacker@evil1mg.com")
    assert not a.permits("attacker@1mg.com.evil.com")


# --- sessions --------------------------------------------------------------


def test_a_session_is_claimed_only_with_its_own_state(tmp_path: Path):
    """A replayed or foreign callback must not complete someone's login."""
    s = _sessions(tmp_path)
    sid, state = s.begin("hr@1mg.com")

    with pytest.raises(AuthError):
        s.claim(sid, "not-the-state", USER)

    s.claim(sid, state, USER)
    assert s.get(sid, load_config(_config(tmp_path))).email == USER

    # The state is spent, so the same callback cannot be replayed.
    with pytest.raises(AuthError):
        s.claim(sid, state, "someone.else@1mg.com")


def test_an_unclaimed_session_is_not_logged_in(tmp_path: Path):
    s = _sessions(tmp_path)
    sid, _state = s.begin("hr@1mg.com")
    with pytest.raises(AuthError) as exc:
        s.get(sid, load_config(_config(tmp_path)))
    assert exc.value.reason == "not_logged_in"


def test_an_unknown_session_id_is_not_logged_in(tmp_path: Path):
    with pytest.raises(AuthError):
        _sessions(tmp_path).get("made-up", load_config(_config(tmp_path)))


def test_an_idle_session_expires_and_is_dropped(tmp_path: Path):
    cfg = load_config(_config(tmp_path, "  idle_hours: 0\n"))
    s = _sessions(tmp_path)
    sid, state = s.begin("hr@1mg.com")
    s.claim(sid, state, USER)
    time.sleep(0.01)

    with pytest.raises(AuthError) as exc:
        s.get(sid, cfg)
    assert exc.value.reason == "session_expired"
    # Dropped, not just refused, so the row cannot be revived.
    with pytest.raises(AuthError):
        s.get(sid, load_config(_config(tmp_path)))


def test_losing_access_ends_the_session_without_a_new_login(tmp_path: Path):
    """Membership is re-checked on every request, not only at the callback."""
    s = _sessions(tmp_path)
    sid, state = s.begin("hr@1mg.com")
    s.claim(sid, state, "contractor@1mg.com")
    cfg = load_config(_config(tmp_path, "  allowed_emails: [someone.else@1mg.com]\n"))

    with pytest.raises(AuthError) as exc:
        s.get(sid, cfg)
    assert "no longer allowed" in exc.value.detail


def test_the_admin_token_is_never_written_to_the_session_file(tmp_path: Path):
    """It is a ~10h production bearer; persisting it buys one skipped paste."""
    s = _sessions(tmp_path)
    sid, state = s.begin("hr@1mg.com")
    s.claim(sid, state, USER)
    s.set_hra(sid, "super-secret-bearer")

    assert s.get(sid, load_config(_config(tmp_path))).hra_token == "super-secret-bearer"
    assert b"super-secret-bearer" not in (tmp_path / "sessions.sqlite3").read_bytes()


def test_purge_removes_expired_rows(tmp_path: Path):
    s = _sessions(tmp_path)
    sid, state = s.begin("hr@1mg.com")
    s.claim(sid, state, USER)
    assert s.purge(load_config(_config(tmp_path))) == 0
    assert s.purge(load_config(_config(tmp_path, "  idle_hours: 0\n"))) == 1


# --- per-user isolation ----------------------------------------------------


def test_each_user_gets_their_own_token_file_and_store(tmp_path: Path):
    cfg = load_config(_config(tmp_path))
    a = cfg.for_user("a@1mg.com")
    b = cfg.for_user("b@1mg.com")

    assert a.gmail.token_file != b.gmail.token_file
    assert a.store_path != b.store_path
    assert user_slug("a@1mg.com") in str(a.gmail.token_file)
    # The address itself is not in the path.
    assert "a@1mg.com" not in str(a.store_path)
    # And the shared config is untouched.
    assert cfg.gmail.token_file == Path(".secrets/token.json")


def test_one_users_cached_summary_is_not_served_to_another(tmp_path: Path):
    """The reason the stores are separate files."""
    from oncallbot.models import IssueSummary

    cfg = load_config(_config(tmp_path))
    mine = cfg.for_user(USER)
    Path(mine.store_path).parent.mkdir(parents=True, exist_ok=True)
    with Store(mine.store_path) as store:
        store.upsert(
            IssueSummary(thread_id="t1", subject="s", reporter="", last_message_at="",
                         summary="", issue="i", category="other", severity="p0"),
            "m1",
        )

    client = _app(tmp_path)
    _logged_in(tmp_path, client, USER)
    assert client.get("/api/thread/t1").status_code == 200

    other = TestClient(create_app(_config(tmp_path)))
    _logged_in(tmp_path, other, "someone.else@1mg.com")
    assert other.get("/api/thread/t1").status_code == 404
    assert other.get("/api/context").json()["cached"] == 0


def test_the_session_group_address_is_what_gets_searched(tmp_path: Path):
    cfg = load_config(_config(tmp_path))
    assert cfg.for_user(USER, "other-group@1mg.com").gmail.support_address == (
        "other-group@1mg.com"
    )
    # Blank means keep the configured default.
    assert cfg.for_user(USER, "").gmail.support_address == "hr@1mg.com"


# --- the HTTP surface ------------------------------------------------------


def test_the_root_serves_the_login_page_when_not_signed_in(tmp_path: Path):
    r = _app(tmp_path).get("/")
    assert r.status_code == 200
    assert "Sign in with Google" in r.text
    assert "no-store" in r.headers.get("cache-control", "")


def test_the_root_serves_the_app_once_signed_in(tmp_path: Path):
    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    assert "Sign in with Google" not in ui(client)


def test_every_api_route_refuses_an_anonymous_request(tmp_path: Path):
    client = _app(tmp_path)
    calls = [
        ("get", "/api/context", None),
        ("post", "/api/chat", {"message": "hi"}),
        ("post", "/api/summarize", {"thread_id": "t1"}),
        ("post", "/api/diagnose", {"thread_id": "t1"}),
        ("get", "/api/thread/t1", None),
        ("get", "/api/thread/t1/messages", None),
        ("post", "/api/session", {"group_email": "x@1mg.com"}),
    ]
    for method, url, body in calls:
        r = getattr(client, method)(url, **({"json": body} if body else {}))
        assert r.status_code == 401, f"{method} {url} -> {r.status_code}"
        assert r.json()["reason"] == "not_logged_in", url


def test_the_login_redirect_sets_an_httponly_cookie(tmp_path: Path, monkeypatch):
    client = _app(tmp_path)

    class FakeFlow:
        def authorization_url(self, **kw):
            assert kw["state"], "a state nonce is required"
            return ("https://accounts.google.com/o/oauth2/auth?x=1", kw["state"])

    monkeypatch.setattr("oncallbot.chat.auth.build_flow", lambda c, uri: FakeFlow())

    r = client.get("/auth/login?group=hr@1mg.com", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("https://accounts.google.com/")

    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie, "script must not be able to read it"
    assert "samesite=lax" in cookie.lower(), "a cross-site POST must not carry it"
    assert "Secure" not in cookie, "loopback is plain http"


def test_the_callback_refuses_a_foreign_domain(tmp_path: Path, monkeypatch):
    client = _app(tmp_path)
    sessions = _sessions(tmp_path)
    sid, state = sessions.begin("hr@1mg.com")
    client.app.state.sessions = sessions
    client.cookies.set(COOKIE, sid)

    class FakeFlow:
        credentials = object()

        def fetch_token(self, code):
            return None

    monkeypatch.setattr("oncallbot.chat.auth.build_flow", lambda c, uri: FakeFlow())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())
    monkeypatch.setattr(
        "oncallbot.gmail_auth.authorized_email", lambda _s: "outsider@gmail.com"
    )

    r = client.get(f"/auth/callback?code=x&state={state}", follow_redirects=False)
    assert r.status_code == 302
    assert "err=not_allowed" in r.headers["location"]
    assert "outsider%40gmail.com" in r.headers["location"]
    # No session, and the cookie is cleared.
    with pytest.raises(AuthError):
        sessions.get(sid, load_config(_config(tmp_path)))


def test_the_callback_signs_in_an_allowed_user(tmp_path: Path, monkeypatch):
    client = _app(tmp_path)
    sessions = _sessions(tmp_path)
    sid, state = sessions.begin("hr@1mg.com")
    client.app.state.sessions = sessions
    client.cookies.set(COOKIE, sid)

    class FakeCreds:
        def to_json(self):
            return '{"refresh_token": "rt"}'

    class FakeFlow:
        credentials = FakeCreds()

        def fetch_token(self, code):
            return None

    monkeypatch.setattr("oncallbot.chat.auth.build_flow", lambda c, uri: FakeFlow())
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())
    monkeypatch.setattr("oncallbot.gmail_auth.authorized_email", lambda _s: USER)

    r = client.get(f"/auth/callback?code=x&state={state}", follow_redirects=False)
    assert r.headers["location"] == "/"
    assert sessions.get(sid, load_config(_config(tmp_path))).email == USER

    token = tmp_path / "tokens" / f"{user_slug(USER)}.json"
    assert token.exists()
    assert oct(token.stat().st_mode)[-3:] == "600", "the refresh token is not world-readable"


def test_a_declined_consent_comes_back_with_a_reason(tmp_path: Path):
    r = _app(tmp_path).get("/auth/callback?error=access_denied", follow_redirects=False)
    assert "err=access_denied" in r.headers["location"]


def test_signing_out_drops_the_session(tmp_path: Path):
    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    assert client.get("/api/context").status_code == 200

    r = client.post("/auth/logout")
    assert r.status_code == 204
    assert client.get("/api/context").status_code == 401


def test_a_cross_origin_post_is_refused(tmp_path: Path):
    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    r = client.post(
        "/api/session", json={"group_email": "x@1mg.com"},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_a_same_origin_post_is_allowed(tmp_path: Path):
    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    r = client.post(
        "/api/session", json={"group_email": "x@1mg.com"},
        headers={"origin": "http://testserver"},
    )
    assert r.status_code == 200


def test_the_session_endpoint_updates_the_group_and_the_token(tmp_path: Path):
    client = _app(tmp_path)
    sessions = _logged_in(tmp_path, client)
    sid = client.cookies[COOKIE]

    client.post("/api/session", json={"group_email": "new-group@1mg.com",
                                      "hra_token": "bearer-1"})
    user = sessions.get(sid, load_config(_config(tmp_path)))
    assert user.group_email == "new-group@1mg.com"
    assert user.hra_token == "bearer-1"

    # Omitted means "leave it alone".
    client.post("/api/session", json={"hra_token": "bearer-2"})
    assert sessions.get(sid, load_config(_config(tmp_path))).group_email == (
        "new-group@1mg.com"
    )


def test_context_reports_who_is_signed_in_and_whether_a_token_is_held(tmp_path: Path):
    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    body = client.get("/api/context").json()
    assert body["user"] == USER
    assert body["mailbox"] == USER, "reads happen as the person who signed in"
    assert body["has_hra_token"] is False

    client.post("/api/session", json={"hra_token": "b"})
    assert client.get("/api/context").json()["has_hra_token"] is True


def test_diagnose_without_an_admin_token_asks_for_one(tmp_path: Path):
    """Not a 500, and not a re-login: a distinct reason the UI acts on."""
    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    r = client.post("/api/diagnose", json={"order_group_id": "PO10003583002-668"})
    assert r.status_code == 401
    assert r.json()["reason"] == "hra_missing"
    assert "accessToken" in r.json()["detail"], "it says where to get one"


def test_a_new_turn_supersedes_the_one_in_flight(tmp_path: Path, monkeypatch):
    """Asking something new is the point, so the old turn is cancelled.

    Refusing the second request was the first design, and it was wrong: after
    pressing Stop the user's next question raced the slot's release and got a
    409 it could do nothing about. Newest wins -- still one turn at a time.
    """
    import oncallbot.chat.server as srv

    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    sid = client.cookies[COOKIE]

    stale = srv._Turn()
    client.app.state.turns[sid] = stale

    monkeypatch.setattr(
        "oncallbot.chat.server._stream",
        lambda *a, **k: iter(['event: done\ndata: {}\n\n']),
    )
    # It would otherwise wait for a turn that does not exist to unwind.
    monkeypatch.setattr(srv, "TURN_HANDOVER_SECONDS", 0.05)

    r = client.post("/api/chat", json={"message": "something else"})
    assert r.status_code == 200
    assert stale.cancel.is_set(), "the turn that was running must be told to stop"
    assert sid not in client.app.state.turns, "and the finished turn frees the slot"


def test_single_user_mode_needs_no_login(tmp_path: Path):
    """auth.enabled: false is how the CLI-era single-user setup keeps working."""
    client = _app(tmp_path, "  enabled: false\n")
    assert "Sign in with Google" not in ui(client)
    assert client.get("/api/context").status_code == 200


def test_the_login_page_names_every_error_the_server_can_send(tmp_path: Path):
    """A reason with no message renders as a bare code to the user."""
    from oncallbot.chat import auth as a

    page = (Path("src/oncallbot/chat/static/login.html")).read_text()
    for reason in (a.SESSION_EXPIRED, a.GMAIL_REVOKED, a.NOT_LOGGED_IN,
                   "not_allowed", "exchange_failed", "access_denied", "no_code"):
        assert f"{reason}:" in page, reason


def test_the_ui_routes_a_401_rather_than_rendering_it(tmp_path: Path):
    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    html = ui(client)
    assert "class NeedsToken extends Error" in html
    assert "async function guard(res)" in html
    assert "location.replace('/login?'" in html
    # The admin token is the one 401 that must not throw the user out.
    assert "if(body.reason === 'hra_missing')" in html
    assert "function askForToken(" in html


def test_a_logged_in_user_never_borrows_the_operators_admin_token(
    tmp_path: Path, monkeypatch
):
    """Otherwise every admin API call is made as whoever started the server."""
    monkeypatch.setenv("ONCALLBOT_HRA_TOKEN", "operator-bearer")
    cfg = load_config(_config(tmp_path))

    # The CLI, which has no session, still uses the environment.
    assert cfg.hra.token_value() == "operator-bearer"

    mine = cfg.for_user(USER)
    assert mine.hra.token_value(required=False) == "", "the env token is not theirs"
    mine.hra.session_token = "my-own-bearer"
    assert mine.hra.token_value() == "my-own-bearer"


def test_diagnose_ignores_the_env_token_and_asks_the_user(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ONCALLBOT_HRA_TOKEN", "operator-bearer")
    client = _app(tmp_path)
    _logged_in(tmp_path, client)

    r = client.post("/api/diagnose", json={"order_group_id": "PO10003583002-668"})
    assert r.status_code == 401
    assert r.json()["reason"] == "hra_missing"


def test_the_footer_and_sign_out_follow_whether_login_is_on(tmp_path: Path):
    client = _app(tmp_path)
    _logged_in(tmp_path, client)
    assert client.get("/api/context").json()["auth_enabled"] is True

    single = _app(tmp_path, "  enabled: false\n")
    assert single.get("/api/context").json()["auth_enabled"] is False

    html = ui(client)
    # Sign out is meaningless without a session to end.
    assert "if(c.user && c.auth_enabled){" in html
    assert "no authentication" not in html.split("<script>")[0], (
        "the footer must not still claim there is none"
    )


# --- PKCE -------------------------------------------------------------------

FAKE_CLIENT = """{
  "installed": {
    "client_id": "test.apps.googleusercontent.com",
    "client_secret": "test-secret",
    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
    "token_uri": "https://oauth2.googleapis.com/token",
    "redirect_uris": ["http://localhost"]
  }
}"""


def _real_flow_app(tmp_path: Path) -> TestClient:
    """An app whose /auth/login builds a genuine Flow, PKCE and all."""
    secrets_file = tmp_path / "oauth_client.json"
    secrets_file.write_text(FAKE_CLIENT)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "gmail:\n  account: bot@1mg.com\n  support_address: hr@1mg.com\n"
        f"  credentials_file: {secrets_file}\n"
        f"store:\n  path: {tmp_path / 'x.db'}\n"
        "categories: [other]\n"
        "auth:\n"
        f"  tokens_dir: {tmp_path / 'tokens'}\n"
        f"  stores_dir: {tmp_path / 'users'}\n"
    )
    return TestClient(create_app(cfg))


def test_the_authorization_url_uses_pkce(tmp_path: Path):
    client = _real_flow_app(tmp_path)
    r = client.get("/auth/login?group=hr@1mg.com", follow_redirects=False)

    url = r.headers["location"]
    assert "code_challenge=" in url
    assert "code_challenge_method=S256" in url

    sid = r.cookies[COOKIE]
    stored = _sessions(tmp_path).verifier(sid)
    assert len(stored) >= 43, "the verifier Google's challenge was derived from"


def test_the_code_verifier_survives_to_the_token_exchange(tmp_path: Path, monkeypatch):
    """The bug this pins: two requests build two Flows, and the second one had
    no verifier -- Google answered `invalid_grant: Missing code verifier`."""
    client = _real_flow_app(tmp_path)
    r = client.get("/auth/login?group=hr@1mg.com", follow_redirects=False)
    sid = r.cookies[COOKIE]
    state = _sessions(tmp_path)._conn.execute(  # noqa: SLF001 - reading our own row
        "SELECT pending_state FROM sessions WHERE id = ?", (sid,)
    ).fetchone()["pending_state"]
    issued = _sessions(tmp_path).verifier(sid)

    seen = {}

    def fake_fetch(self, code=None, **kw):
        seen["verifier"] = self.code_verifier
        seen["code"] = code

    monkeypatch.setattr("google_auth_oauthlib.flow.Flow.fetch_token", fake_fetch)
    monkeypatch.setattr(
        "google_auth_oauthlib.flow.Flow.credentials",
        property(lambda self: type("C", (), {"to_json": lambda s: "{}"})()),
    )
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: object())
    monkeypatch.setattr("oncallbot.gmail_auth.authorized_email", lambda _s: USER)

    client.cookies.set(COOKIE, sid)
    res = client.get(f"/auth/callback?code=abc&state={state}", follow_redirects=False)

    assert seen["code"] == "abc"
    assert seen["verifier"] == issued, "the exchange must present the same verifier"
    assert res.headers["location"] == "/"
    # Single use: spent along with the state.
    assert _sessions(tmp_path).verifier(sid) == ""


def test_a_sessions_file_from_before_pkce_is_migrated(tmp_path: Path):
    """An existing session file must not need deleting to keep working."""
    import sqlite3

    path = tmp_path / "sessions.sqlite3"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, user_email TEXT NOT NULL, "
        "group_email TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, "
        "last_seen_at REAL NOT NULL, pending_state TEXT NOT NULL DEFAULT '');"
    )
    old.execute(
        "INSERT INTO sessions VALUES ('sid-1', ?, 'hr@1mg.com', ?, ?, '')",
        (USER, time.time(), time.time()),
    )
    old.commit()
    old.close()

    s = Sessions(path)
    assert s.get("sid-1", load_config(_config(tmp_path))).email == USER
    assert s.verifier("sid-1") == ""
    s.stash_verifier("sid-1", "v")
    assert s.verifier("sid-1") == "v"


def test_single_user_mode_honours_a_pasted_admin_token(tmp_path: Path):
    """Otherwise the paste box is a dead end: it asks, accepts, asks again."""
    client = _app(tmp_path, "  enabled: false\n")

    assert client.get("/api/context").json()["has_hra_token"] is False
    assert client.post("/api/session", json={"hra_token": "pasted"}).status_code == 200
    assert client.get("/api/context").json()["has_hra_token"] is True


def test_the_default_port_is_stated_in_exactly_one_place():
    """It drifted once already: `doctor` printed a hardcoded port that no
    longer matched what `serve` bound, so the redirect URI it told people to
    register was wrong."""
    from pathlib import Path as _Path

    from oncallbot.config import DEFAULT_PORT

    assert DEFAULT_PORT == 8765, "8080 collides with a local nginx"
    cli = _Path("src/oncallbot/cli.py").read_text()
    assert str(DEFAULT_PORT) not in cli, "the CLI must read the constant, not repeat it"
    assert "DEFAULT_PORT" in cli
