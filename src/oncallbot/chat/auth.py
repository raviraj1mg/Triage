"""Browser login: a Google consent per person, a session cookie, no tokens in the page.

Three decisions worth stating, because they are the ones a reviewer should
check rather than infer:

1. The cookie holds an opaque session id and nothing else. Google's refresh
   token stays on disk, server-side, in a file named after a hash of the
   address that consented. The page renders untrusted email, so a credential
   reachable from JavaScript would turn any XSS into a mailbox compromise.
2. Identity comes from Gmail itself. `users.getProfile` on the token that was
   just issued says which mailbox it reads, so no `openid` scope is needed and
   the account that consented *is* the user -- there is no second claim to
   trust.
3. The admin API bearer is held in memory only. It is a ~10h production
   credential; persisting it buys one skipped paste after a restart and costs a
   secret at rest.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response

from ..config import Config
from ..gmail_auth import SCOPES

COOKIE = "oncallbot_sid"
# Reasons the UI switches on. `hra_expired` is handled inline; the rest send
# the user back to the login page.
SESSION_EXPIRED = "session_expired"
GMAIL_REVOKED = "gmail_revoked"
HRA_MISSING = "hra_missing"
NOT_LOGGED_IN = "not_logged_in"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_email    TEXT NOT NULL,
    group_email   TEXT NOT NULL DEFAULT '',
    created_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    -- Set before the redirect to Google and cleared by the callback. A
    -- callback whose state does not match a pending row is not ours.
    pending_state TEXT NOT NULL DEFAULT '',
    -- The PKCE verifier, which the authorization URL generates and the token
    -- exchange must present. Two requests, two Flow objects, so it has to
    -- survive in between -- without it Google answers "Missing code verifier".
    code_verifier TEXT NOT NULL DEFAULT '',
    -- Which model answers, chosen per person from the composer. A preference,
    -- not a credential, so it lives here rather than in memory.
    backend TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT ''
);
"""


class AuthError(HTTPException):
    """A 401 the UI can act on: `reason` decides where the user is sent."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(status_code=401, detail=detail)
        self.reason = reason


@dataclass
class SessionUser:
    session_id: str
    email: str
    group_email: str = ""
    backend: str = ""
    model: str = ""
    # Never persisted. Absent until the user pastes one.
    hra_token: str = ""


def user_slug(email: str) -> str:
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


class Sessions:
    """Session rows on disk, admin tokens in memory.

    On disk so a `serve` restart does not sign the team out; the Gmail token is
    already on disk, so the session adds no new class of secret. The admin
    bearer is the exception and lives in `_hra` for the process lifetime.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # One connection, shared by every request thread and by the turn
        # worker, so every statement is serialized here.
        #
        # Without this a live session was reported as logged out: concurrent
        # `execute` on one connection interleaves, the row comes back empty,
        # and `get` reads that as "no such session" -- a 401, which the UI
        # correctly treats as a dead session and bounces to /login. Measured
        # under six readers and four writers: 57,517 spurious logouts and
        # 14,957 `InterfaceError: bad parameter or other API misuse` in six
        # seconds, on a session that never expired. The reads are microseconds,
        # so serializing them costs nothing worth measuring.
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()
        try:
            path.chmod(0o600)
        except OSError:  # pragma: no cover - filesystem without chmod
            pass
        self._hra: dict[str, str] = {}

    def _migrate(self) -> None:
        """Add columns to a sessions file written by an older build."""
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(sessions)")}
        for col in ("code_verifier", "backend", "model"):
            if col not in cols:
                self._conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"
                )

    # --- every statement goes through one of these ------------------------
    #
    # The fetch has to happen under the same lock as the execute: a cursor
    # from this connection is invalidated by another thread's statement, which
    # is how a valid session came back as no rows at all.

    def _one(self, sql: str, args: tuple = ()) -> Any:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[Any]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def _run(self, sql: str, args: tuple = ()) -> int:
        """A write, committed. Returns the affected row count."""
        with self._lock:
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.rowcount or 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- the OAuth leg ---------------------------------------------------

    def begin(self, group_email: str) -> tuple[str, str]:
        """Open an anonymous session and return (session_id, state)."""
        sid = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(24)
        now = time.time()
        self._run(
            "INSERT INTO sessions (id, user_email, group_email, created_at, "
            "last_seen_at, pending_state) VALUES (?, '', ?, ?, ?, ?)",
            (sid, group_email, now, now, state),
        )
        return sid, state

    def stash_verifier(self, sid: str, verifier: str) -> None:
        """Hold the PKCE verifier until the callback exchanges the code."""
        self._run(
            "UPDATE sessions SET code_verifier = ? WHERE id = ?",
            (verifier or "", sid),
        )

    def verifier(self, sid: str) -> str:
        row = self._one("SELECT code_verifier FROM sessions WHERE id = ?", (sid,))
        return str(row["code_verifier"]) if row else ""

    def claim(self, sid: str, state: str, email: str) -> None:
        """Finish the login. The state must match the one we issued."""
        row = self._one("SELECT pending_state FROM sessions WHERE id = ?", (sid,))
        if row is None:
            raise AuthError(SESSION_EXPIRED, "That login has expired. Start again.")
        if not row["pending_state"] or not secrets.compare_digest(
            str(row["pending_state"]), state
        ):
            # Either a replayed callback or one meant for someone else.
            raise AuthError(SESSION_EXPIRED, "That sign-in link is not valid here.")
        # The verifier is single-use, so it goes with the state.
        self._run(
            "UPDATE sessions SET user_email = ?, last_seen_at = ?, "
            "pending_state = '', code_verifier = '' WHERE id = ?",
            (email.strip().lower(), time.time(), sid),
        )

    # --- the live session ------------------------------------------------

    def get(self, sid: str, cfg: Config) -> SessionUser:
        row = self._one("SELECT * FROM sessions WHERE id = ?", (sid,))
        if row is None or not row["user_email"]:
            raise AuthError(NOT_LOGGED_IN, "Sign in to use oncallbot.")

        now = time.time()
        idle = now - float(row["last_seen_at"])
        age = now - float(row["created_at"])
        if idle > cfg.auth.idle_hours * 3600 or age > cfg.auth.max_hours * 3600:
            self.drop(sid)
            raise AuthError(SESSION_EXPIRED, "Your session expired. Sign in again.")

        # Membership can be revoked between logins, so it is re-checked here
        # rather than only at the callback.
        if not cfg.auth.permits(row["user_email"]):
            self.drop(sid)
            raise AuthError(
                SESSION_EXPIRED,
                f"{row['user_email']} is no longer allowed to use oncallbot.",
            )

        self._run("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (now, sid))
        return SessionUser(
            session_id=sid,
            email=row["user_email"],
            group_email=row["group_email"] or "",
            backend=row["backend"] or "",
            model=row["model"] or "",
            hra_token=self._hra.get(sid, ""),
        )

    def set_engine(self, sid: str, backend: str, model: str) -> None:
        """Which model answers for this session.

        Upsert, because single-user mode has a session id but no row -- login
        is off, so nothing ever created one, and a plain UPDATE silently
        matched nothing. In login mode the row always exists and the insert is
        a no-op.
        """
        now = time.time()
        # Both statements under one lock hold, so a concurrent reader never
        # sees the row between the insert and the update.
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions (id, user_email, created_at, "
                "last_seen_at) VALUES (?, '', ?, ?)",
                (sid, now, now),
            )
            self._conn.execute(
                "UPDATE sessions SET backend = ?, model = ? WHERE id = ?",
                (backend.strip(), model.strip(), sid),
            )
            self._conn.commit()

    def engine_for(self, sid: str) -> tuple[str, str]:
        row = self._one("SELECT backend, model FROM sessions WHERE id = ?", (sid,))
        return (str(row["backend"]), str(row["model"])) if row else ("", "")

    def set_group(self, sid: str, group_email: str) -> None:
        self._run(
            "UPDATE sessions SET group_email = ? WHERE id = ?",
            (group_email.strip(), sid),
        )

    def hra_for(self, sid: str) -> str:
        """The admin bearer held for this session, if any."""
        return self._hra.get(sid, "")

    def set_hra(self, sid: str, token: str) -> None:
        token = token.strip()
        if token:
            self._hra[sid] = token
        else:
            self._hra.pop(sid, None)

    def drop(self, sid: str) -> None:
        self._run("DELETE FROM sessions WHERE id = ?", (sid,))
        self._hra.pop(sid, None)

    def purge(self, cfg: Config) -> int:
        """Drop expired rows. Cheap, and keeps the file from growing forever."""
        now = time.time()
        return self._run(
            "DELETE FROM sessions WHERE last_seen_at < ? OR created_at < ?",
            (now - cfg.auth.idle_hours * 3600, now - cfg.auth.max_hours * 3600),
        )


def set_cookie(response: Response, sid: str, *, secure: bool) -> None:
    """httpOnly so script cannot read it; lax so a cross-site POST cannot use it."""
    response.set_cookie(
        COOKIE,
        sid,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


def check_origin(request: Request) -> None:
    """Refuse a cross-site write.

    SameSite=lax already blocks the cookie on a cross-site POST; this is the
    second lock, because the cost of being wrong is a request made as the user
    against their own mailbox.
    """
    origin = request.headers.get("origin")
    if not origin:
        return  # same-origin fetches and curl send none
    host = request.headers.get("host", "")
    allowed = {f"http://{host}", f"https://{host}"}
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="Cross-origin request refused.")


# --- Google -----------------------------------------------------------------


def build_flow(cfg: Config, redirect_uri: str) -> Any:
    """The auth-code flow, on this server's own callback URL."""
    from google_auth_oauthlib.flow import Flow

    if not cfg.gmail.credentials_file.exists():
        raise HTTPException(
            status_code=503,
            detail=(
                f"OAuth client secrets not found at {cfg.gmail.credentials_file}. "
                "See README.md → Setup."
            ),
        )
    return Flow.from_client_secrets_file(
        str(cfg.gmail.credentials_file), scopes=SCOPES, redirect_uri=redirect_uri
    )


def save_user_token(cfg: Config, email: str, creds: Any) -> Path:
    """One token file per person, named after a hash of their address."""
    path = cfg.auth.tokens_dir / f"{user_slug(email)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json())
    path.chmod(0o600)
    return path
