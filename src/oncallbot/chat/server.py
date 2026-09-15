"""FastAPI chat server.

Binds to 127.0.0.1 and has no authentication, deliberately: it serves summaries
derived from patient data. See docs/deploy.md before putting it on a network.
"""

from __future__ import annotations

import json
import queue
import re
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from ..config import Config, load_config
from ..gmail_auth import WrongAccountError, assert_account, build_service
from ..gmail_client import GmailClient
from ..store import Store
from ..streaming import CANCEL, Cancelled
from ..summarizer import SummarizerError, build_summarizer
from . import actions, auth
from .auth import AuthError, SessionUser, Sessions
from ..prompts.live import CATALOG, catalog_for, set_overrides
from ..trace import set_sink as set_trace_sink
from .intent import IntentError, parse_intent

STATIC = Path(__file__).parent / "static"
# Optional: drop a screen recording here and the token prompt offers to play
# it. Absent, the button is simply not shown -- an offer to watch a video that
# does not exist is worse than no offer.
HELP_VIDEO = STATIC / "help" / "admin-token.mp4"

# How long a new turn waits for the one it just cancelled to unwind, so their
# model calls do not overlap. Bounded on purpose: the user is waiting.
TURN_HANDOVER_SECONDS = 3.0

# The session id used when login is off: there is only ever one user.
_LOCAL = "local"


@dataclass
class _Turn:
    """One in-flight chat turn: how to stop it, and when it has stopped."""

    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)


class HistoryTurn(BaseModel):
    """One earlier turn, as the client remembers it.

    History is client-supplied, so it is capped and treated as data: the router
    fences it and is told never to act on it.
    """

    message: str = Field(default="", max_length=2000)
    action: str = Field(default="", max_length=32)
    params: dict[str, Any] = Field(default_factory=dict)
    reply: str = Field(default="", max_length=1000)
    # A compact record of what was actually displayed, so a follow-up about
    # "the above" can be answered without going back to Gmail.
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=80)


THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _check_thread_id(thread_id: str) -> str:
    """Thread ids land in a Gmail API path, so validate the shape up front."""
    if not THREAD_ID_RE.match(thread_id):
        raise HTTPException(status_code=422, detail="Malformed thread id.")
    return thread_id


class SummarizeOneRequest(BaseModel):
    thread_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    force: bool = False
    prompts: dict[str, str] = Field(default_factory=dict)


class DiagnoseRequest(BaseModel):
    thread_id: str = Field(default="", max_length=64)
    order_group_id: str = Field(default="", max_length=48)
    reason: bool = True
    prompts: dict[str, str] = Field(default_factory=dict)


class SessionUpdate(BaseModel):
    """None means "leave it alone"; "" means "clear it"."""

    group_email: str | None = Field(default=None, max_length=254)
    hra_token: str | None = Field(default=None, max_length=8000)
    backend: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=128)


# Edited prompts travel with the request rather than being stored: the client
# holds them in memory, so a refresh resets them and one person's experiment
# cannot outlive their tab or reach anyone else.
PromptEdits = dict[str, str]


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    history: list[HistoryTurn] = Field(default_factory=list, max_length=20)
    prompts: PromptEdits = Field(default_factory=dict)


def create_app(config_path: Path, *, public_url: str = "") -> FastAPI:
    app = FastAPI(title="oncallbot", docs_url=None, redoc_url=None)
    # Loaded per request so edits to config.yaml apply without a restart.
    app.state.config_path = config_path
    # The origin Google redirects back to. Set by `serve`; falls back to the
    # request's own host, which is right for loopback.
    app.state.public_url = public_url.rstrip("/")
    app.state.sessions_lock = threading.Lock()

    def cfg() -> Config:
        return load_config(app.state.config_path)

    def sessions() -> Sessions:
        # Built under a lock: two threads racing the first request would each
        # open their own connection, and a per-instance lock cannot serialize
        # across two instances -- which is the same interleaving that reported
        # live sessions as logged out.
        store = getattr(app.state, "sessions", None)
        if store is not None:
            return store
        with app.state.sessions_lock:
            store = getattr(app.state, "sessions", None)
            if store is None:
                c = cfg()
                store = Sessions(c.store_path.parent / "sessions.sqlite3")
                store.purge(c)
                app.state.sessions = store
        return store

    def _redirect_uri(request: Request) -> str:
        base = app.state.public_url or f"{request.url.scheme}://{request.headers['host']}"
        return base + cfg().auth.redirect_path

    def current_user(request: Request) -> SessionUser:
        """Every /api route runs as one logged-in person, or not at all."""
        c = cfg()
        if not c.auth.enabled:
            # Single-user mode: the CLI's cached Gmail token, as before login
            # existed. A pasted admin token still has to be honoured, or the
            # prompt is a dead end -- it asks, accepts, and asks again.
            backend, model = sessions().engine_for(_LOCAL)
            return SessionUser(
                session_id=_LOCAL,
                email=c.gmail.account,
                backend=backend,
                model=model,
                hra_token=sessions().hra_for(_LOCAL),
            )
        sid = request.cookies.get(auth.COOKIE, "")
        if not sid:
            raise AuthError(auth.NOT_LOGGED_IN, "Sign in to use oncallbot.")
        return sessions().get(sid, c)

    # One turn per session at a time, and the NEWEST one wins. A summarize can
    # run for minutes and spawns a model subprocess, so two at once is worth
    # preventing -- but refusing the second is the wrong way round: someone
    # asking a new question has stopped caring about the old answer. Refusing
    # was the first design, and after Stop the next question raced the old
    # turn's release and got a 409 it could do nothing about.
    #
    # Cross-user load is still the queue item in docs/deploy.md.
    app.state.turns: dict[str, _Turn] = {}
    app.state.busy_lock = threading.Lock()

    def claim_turn(sid: str, turn: _Turn) -> None:
        """Take the slot, telling whoever holds it to stop. Always succeeds."""
        with app.state.busy_lock:
            previous = app.state.turns.get(sid)
            app.state.turns[sid] = turn
        if previous is None:
            return
        previous.cancel.set()
        # A courtesy wait so the two model calls do not overlap. The relay
        # wakes on a bounded tick, so this is a fraction of a second. If the
        # old turn is slow to die the new one starts anyway: the person asking
        # is the one waiting.
        previous.done.wait(timeout=TURN_HANDOVER_SECONDS)

    def release_turn(sid: str, turn: _Turn, stream: Iterator[str]) -> Iterator[str]:
        try:
            yield from stream
        finally:
            with app.state.busy_lock:
                # Only if it is still ours: a newer turn may already hold it.
                if app.state.turns.get(sid) is turn:
                    del app.state.turns[sid]
            turn.done.set()

    # Exposed so the slot rules can be tested against the real functions
    # rather than a copy of them in a test.
    app.state.claim_turn = claim_turn
    app.state.release_turn = release_turn

    def user_cfg(user: SessionUser) -> Config:
        """The config as it applies to this person: their token, their cache."""
        c = cfg()
        if not c.auth.enabled:
            # No per-user split here, but a pasted token beats the environment
            # for the same reason it does under login: it is the newer one.
            if user.hra_token:
                c.hra.session_token = user.hra_token
            if user.backend:
                c.summarizer.backend = user.backend
                if user.backend == "local" and user.model:
                    c.local.model = user.model
                elif user.backend == "claude_cli" and user.model:
                    c.summarizer.model = user.model
            return c
        c = c.for_user(user.email, user.group_email)
        if user.backend:
            c.summarizer.backend = user.backend
            if user.backend == "local" and user.model:
                c.local.model = user.model
            elif user.backend == "claude_cli" and user.model:
                c.summarizer.model = user.model
        if user.hra_token:
            # Their own admin bearer, never the operator's.
            c.hra.session_token = user.hra_token
        return c

    @app.exception_handler(AuthError)
    def _auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        """A 401 the UI can route on rather than guess at."""
        return JSONResponse(
            status_code=401, content={"detail": exc.detail, "reason": exc.reason}
        )

    @app.get("/")
    def index(request: Request) -> FileResponse:
        # No-store: the UI ships inside the package, so a cached copy survives
        # an upgrade and silently serves the old app.
        page = "index.html"
        c = cfg()
        if c.auth.enabled:
            sid = request.cookies.get(auth.COOKIE, "")
            try:
                sessions().get(sid, c)
            except AuthError:
                page = "login.html"
        return FileResponse(
            STATIC / page,
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/help/admin-token")
    def help_video() -> FileResponse:
        """The recording of where to find the admin token."""
        if not HELP_VIDEO.exists():
            raise HTTPException(status_code=404, detail="No help video installed.")
        return FileResponse(HELP_VIDEO, media_type="video/mp4")

    @app.get("/login")
    def login_page() -> FileResponse:
        return FileResponse(
            STATIC / "login.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/auth/login")
    def auth_login(request: Request, group: str = "") -> RedirectResponse:
        """Start the consent. The state nonce is stored on a fresh session."""
        c = cfg()
        flow = auth.build_flow(c, _redirect_uri(request))
        sid, state = sessions().begin(group or c.gmail.support_address)
        url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="true",
            state=state,
        )
        # PKCE: authorization_url() just generated the verifier whose hash it
        # sent to Google. The callback builds a different Flow, so it has to be
        # carried across -- otherwise the exchange fails "Missing code verifier".
        sessions().stash_verifier(sid, getattr(flow, "code_verifier", "") or "")
        res = RedirectResponse(url, status_code=302)
        auth.set_cookie(res, sid, secure=request.url.scheme == "https")
        return res

    @app.get("/auth/callback")
    def auth_callback(
        request: Request, code: str = "", state: str = "", error: str = ""
    ) -> RedirectResponse:
        """Exchange the code, then ask Gmail who it belongs to."""
        if error or not code:
            return RedirectResponse(f"/login?err={error or 'no_code'}", status_code=302)

        c = cfg()
        sid = request.cookies.get(auth.COOKIE, "")
        if not sid:
            return RedirectResponse(f"/login?err={auth.SESSION_EXPIRED}", status_code=302)

        flow = auth.build_flow(c, _redirect_uri(request))
        flow.code_verifier = sessions().verifier(sid)
        try:
            flow.fetch_token(code=code)
        except Exception as exc:  # noqa: BLE001 - shown to the user as-is
            return RedirectResponse(
                f"/login?err=exchange_failed&detail={quote(str(exc)[:200])}",
                status_code=302,
            )

        creds = flow.credentials
        # Identity from Gmail itself: whichever mailbox this token reads is the
        # user. No second claim to trust.
        from googleapiclient.discovery import build as build_api

        from ..gmail_auth import authorized_email

        email = authorized_email(build_api("gmail", "v1", credentials=creds,
                                           cache_discovery=False))
        if not c.auth.permits(email):
            sessions().drop(sid)
            res = RedirectResponse(
                f"/login?err=not_allowed&detail={quote(email)}", status_code=302
            )
            auth.clear_cookie(res)
            return res

        auth.save_user_token(c, email, creds)
        sessions().claim(sid, state, email)
        return RedirectResponse("/", status_code=302)

    @app.post("/auth/logout")
    def auth_logout(request: Request) -> Response:
        sid = request.cookies.get(auth.COOKIE, "")
        if sid:
            sessions().drop(sid)
        res = Response(status_code=204)
        auth.clear_cookie(res)
        return res

    @app.post("/api/suggest")
    def suggest(
        req: ChatRequest, request: Request, user: SessionUser = Depends(current_user)
    ) -> dict[str, Any]:
        """What to offer next, from the conversation so far.

        Off the critical path: called after an answer is on screen, and an
        empty list means the caller keeps the chips it has.
        """
        auth.check_origin(request)
        from .suggest import suggest as propose

        history = [t.model_dump() for t in req.history]
        return {"suggestions": propose(user_cfg(user), history)}

    @app.post("/api/session")
    def update_session(
        req: SessionUpdate, request: Request, user: SessionUser = Depends(current_user)
    ) -> dict[str, Any]:
        """The two things the login form collects, changeable later."""
        auth.check_origin(request)
        if req.group_email is not None:
            sessions().set_group(user.session_id, req.group_email)
        if req.hra_token is not None:
            sessions().set_hra(user.session_id, req.hra_token)
        if req.backend is not None:
            from ..config import BACKENDS

            backend = req.backend.strip()
            if backend and backend not in BACKENDS:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unknown backend {backend!r}. One of: {', '.join(BACKENDS)}.",
                )
            sessions().set_engine(user.session_id, backend, (req.model or "").strip())
        return {"ok": True}

    @app.get("/api/context")
    def context(user: SessionUser = Depends(current_user)) -> dict[str, Any]:
        c = user_cfg(user)
        with Store(c.store_path) as store:
            return {
                "support_address": c.gmail.support_address,
                "mailbox": c.gmail.account,
                "user": user.email,
                "auth_enabled": c.auth.enabled,
                "has_hra_token": bool(c.hra.token_value(required=False)),
                "help_video": HELP_VIDEO.exists(),
                "engines": available_engines(c),
                "engine": {"backend": c.summarizer.backend,
                           "model": c.local.model if c.summarizer.backend == "local"
                           else (c.summarizer.api_model
                                 if c.summarizer.backend == "anthropic_api"
                                 else c.summarizer.model)},
                "categories": c.categories,
                "lookback_days": c.gmail.lookback_days,
                "cached": store.total(),
                "by_severity": store.counts_by("severity"),
                "by_category": store.counts_by("category"),
            }

    @app.get("/api/prompts")
    def prompts_catalog(user: SessionUser = Depends(current_user)) -> dict[str, Any]:
        """The prompts behind each answer, so the UI can show and edit them.

        Defaults only -- an edit lives in the client and travels with the next
        request, so there is nothing per-user to return here.
        """
        return {
            "surfaces": {
                surface: catalog_for(surface)
                for surface in ("routing", "summary", "diagnosis", "followup")
            },
            "keys": sorted(CATALOG),
        }

    @app.post("/api/chat")
    def chat(
        req: ChatRequest, request: Request, user: SessionUser = Depends(current_user)
    ) -> StreamingResponse:
        auth.check_origin(request)
        c = user_cfg(user)
        turn = _Turn()
        claim_turn(user.session_id, turn)
        return StreamingResponse(
            release_turn(
                user.session_id,
                turn,
                _stream(
                    c, req.message, [t.model_dump() for t in req.history], turn.cancel,
                    prompts=req.prompts,
                ),
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/thread/{thread_id}/messages")
    def thread_messages(
        thread_id: str, user: SessionUser = Depends(current_user)
    ) -> dict[str, Any]:
        """The full thread, straight from Gmail. No summarizer, no store.

        Bodies are returned unredacted: this serves the mailbox owner, who can
        already read the thread in Gmail. Redaction exists to limit what leaves
        the machine for the model, not to hide mail from its own operator.
        """
        _check_thread_id(thread_id)
        c = user_cfg(user)
        thread = _fetch_thread(c, thread_id)

        return {
            "thread_id": thread.id,
            "subject": thread.subject,
            "permalink": thread.permalink,
            "messages": [
                {
                    "id": m.id,
                    "date": m.date.isoformat() if m.date else "",
                    "from": m.sender,
                    "to": m.to,
                    "cc": m.cc,
                    "subject": m.subject,
                    "body": m.body_text or m.snippet or "",
                    "attachments": [
                        {
                            "filename": a.filename,
                            "mime_type": a.mime_type,
                            "size_bytes": a.size_bytes,
                        }
                        for a in m.attachments
                    ],
                }
                for m in thread.messages
            ],
        }

    @app.post("/api/diagnose")
    def diagnose(
        req: DiagnoseRequest, request: Request,
        user: SessionUser = Depends(current_user),
    ) -> StreamingResponse:
        auth.check_origin(request)
        """Diagnose the order behind one email thread, streamed.

        The order id is read out of the mail itself when not supplied, so this
        works on a thread that has never been summarized. The thread is fetched
        here rather than on the worker, so a wrong account or an unknown id is
        still a real HTTP error instead of an error event.
        """
        c = user_cfg(user)
        # Diagnosis is the one card action that needs the admin API, so a
        # missing bearer is asked for rather than failing the turn.
        if not c.hra.token_value(required=False):
            raise AuthError(auth.HRA_MISSING, c.hra.missing_message())
        ogid = (req.order_group_id or "").strip().upper()
        thread = None

        if req.thread_id:
            _check_thread_id(req.thread_id)
            thread = _fetch_thread(c, req.thread_id)
        elif not ogid:
            raise HTTPException(status_code=422, detail="Give a thread_id or an order_group_id.")

        return StreamingResponse(
            _sse_worker(
                lambda: _diagnose_events(c, thread, ogid, req.reason),
                prompts=req.prompts,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/summarize")
    def summarize_one(
        req: SummarizeOneRequest, request: Request,
        user: SessionUser = Depends(current_user),
    ) -> StreamingResponse:
        auth.check_origin(request)
        """Summarize a single thread, on demand from a card's CTA, streamed.

        Listing threads costs nothing; this is the only place the summarizer
        runs for a browse, and only for the thread the user picked. The Gmail
        fetch happens here so its failures stay HTTP errors; only the model
        call is streamed.
        """
        _check_thread_id(req.thread_id)
        c = user_cfg(user)
        gmail = _gmail_client(c)
        thread = _fetch_thread(c, req.thread_id, client=gmail)

        return StreamingResponse(
            _sse_worker(
                lambda: _summarize_events(c, gmail, thread, req.force),
                prompts=req.prompts,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/thread/{thread_id}")
    def thread(
        thread_id: str, user: SessionUser = Depends(current_user)
    ) -> dict[str, Any]:
        with Store(user_cfg(user).store_path) as store:
            row = store.get(thread_id)
        if row is None:
            raise HTTPException(status_code=404, detail="No summary cached for that thread.")
        return row

    return app


# What each local model was actually measured doing, said at the point of
# choosing. One blanket warning was wrong in both directions: it told people
# gemma3 mis-extracts ids when it does not, and it would have reassured them
# about a model nobody here has run.
_LOCAL_NOTES: dict[str, str] = {
    # Correct ids, severity and category on a real thread, and it held the
    # diagnosis bullet format exactly. Routing needed the example values taken
    # out of the prompt first -- it was copying them verbatim.
    "gemma3": "measured good on routing, ids and diagnosis bullets",
    # Routed correctly in 12s, then filled the id fields with a redaction
    # placeholder ("REDACTED:CARD" as an order id) and two patient names in
    # one patient_id. Those ids drive the Diagnose button and the copy chips,
    # so wrong ones are worse than none.
    "phi4-mini": "good for routing; measured mis-extracting ids in summaries",
}

_LOCAL_UNMEASURED = "not measured here -- check the ids and severity before trusting it"


def _local_note(model: str) -> str:
    """The measured note for this model, or an honest admission there is none."""
    base = model.split(":", 1)[0].lower()
    return _LOCAL_NOTES.get(base, _LOCAL_UNMEASURED)


def available_engines(c: Config) -> list[dict[str, Any]]:
    """The model choices that would actually work on this machine.

    Probed rather than assumed: offering a backend that fails on selection is
    worse than not offering it. The local models come from asking the server
    what it has, so the list is whatever has been pulled.
    """
    import shutil

    out: list[dict[str, Any]] = [{
        "id": "claude_cli",
        "model": c.summarizer.model,
        "label": f"Claude Code ({c.summarizer.model})",
        "available": shutil.which("claude") is not None,
        "why": "" if shutil.which("claude") else "`claude` is not on PATH",
        "note": "",
    }]

    try:
        c.summarizer.api_key()
        api_ok, api_why = True, ""
    except Exception as exc:  # noqa: BLE001 - the message is the point
        api_ok, api_why = False, str(exc).splitlines()[0]
    out.append({
        "id": "anthropic_api",
        "model": c.summarizer.api_model,
        "label": f"Anthropic API ({c.summarizer.api_model})",
        "available": api_ok,
        "why": api_why,
        "note": "",
    })

    for name in _local_models(c):
        out.append({
            "id": "local",
            "model": name,
            "label": f"Local · {name}",
            "available": True,
            "why": "",
            "note": _local_note(name),
        })
    return out


def _local_models(c: Config) -> list[str]:
    """Ask the local server what it has. Empty if it is not running."""
    import httpx

    try:
        r = httpx.get(f"{c.local.base_url.rstrip('/')}/v1/models", timeout=2.0)
        if r.status_code >= 400:
            return []
        names = [str(m.get("id", "")) for m in (r.json().get("data") or [])]
    except Exception:  # noqa: BLE001 - not running is the common case
        return []
    return [n for n in names if n]


def _gmail_client(c: Config) -> GmailClient:
    """Authenticated Gmail client, or an HTTP error a browser can show."""
    try:
        service = build_service(
            c.gmail.credentials_file,
            c.gmail.token_file,
            interactive=False,
            login_hint=c.gmail.account,
        )
        assert_account(service, c.gmail.account)
    except WrongAccountError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return GmailClient(service)


def _fetch_thread(c: Config, thread_id: str, client: GmailClient | None = None) -> Any:
    try:
        return (client or _gmail_client(c)).get_thread(thread_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - unknown id, network, etc.
        raise HTTPException(
            status_code=502, detail=f"Could not fetch that thread: {exc}"
        ) from exc


def _summarize_events(
    c: Config, gmail: GmailClient, thread: Any, force: bool
) -> Iterator[tuple[str, Any]]:
    """One thread's summary, field by field as the model writes it.

    A current cached summary short-circuits the model entirely -- the card asks
    for this every time it is opened, and re-paying for an unchanged thread
    would be the wrong default.
    """
    from ..summarizer import summarize_stream

    with Store(c.store_path) as store:
        cached = store.get(thread.id)
        if not force and cached and store.is_current(thread.id, thread.last.id):
            yield ("summary", {"summary": cached, "cached": True})
            return

        yield ("status", f"Summarizing {len(thread.messages)} message(s)…")
        summary = None
        try:
            for kind, payload in summarize_stream(build_summarizer(c, gmail), thread):
                if kind == "summary":
                    summary = payload
                else:
                    yield (kind, payload)
        except SummarizerError as exc:
            yield ("error", str(exc))
            return
        assert summary is not None
        store.upsert(summary, thread.last.id)
        yield ("summary", {"summary": summary.to_dict(), "cached": False})


def _diagnose_events(
    c: Config, thread: Any, ogid: str, with_reason: bool
) -> Iterator[tuple[str, Any]]:
    """A diagnosis as it is established: checks first, narration after."""
    from ..diagnose import (
        MISMATCH,
        REASON_SYSTEM_PROMPT,
        _stream_prose,
        build_reason_prompt,
        diagnose_order,
        diagnose_thread_stream,
    )
    from ..hra_client import HraAuthError, HraBlockedError, HraClient

    client = HraClient(c.hra)
    try:
        if thread is not None:
            d = None
            for kind, payload in diagnose_thread_stream(
                c, thread, client=client, order_group_id=ogid
            ):
                if kind == "result":
                    d = payload
                else:
                    yield (kind, payload)
            assert d is not None
            yield ("diagnosis", {"diagnosis": d.to_dict(), "order_group_id": d.order_group_id})
            return

        # Order-only: no thread to read, so there is no closed/open verdict.
        yield ("status", f"Checking runbooks against {ogid}")
        trail: list[str] = []
        d = diagnose_order(
            c, ogid, client=client, on_progress=trail.append,
            with_reason=False, subject="",
        )
        for line in trail:
            yield ("status", line)
        if d.checks:
            yield ("checks", [asdict(ch) for ch in d.checks])
        if with_reason and d.verdict == MISMATCH:
            text, failure = yield from _stream_prose(
                c, REASON_SYSTEM_PROMPT, build_reason_prompt(d, "")
            )
            d.reason = text
            if failure and not text:
                d.trail.append(f"reason unavailable ({failure})")
        yield ("diagnosis", {"diagnosis": d.to_dict(), "order_group_id": ogid})
    except HraAuthError as exc:
        # The turn is already a 200, so this cannot be the 401 the card uses
        # when the token is absent. Same box, asked for as an event instead --
        # except for a 403, where the account lacks the role and a new token
        # from the same dashboard would be refused identically.
        if getattr(exc, "a_new_token_would_help", True):
            yield ("needs_token",
                   {"detail": c.hra.rejected_message(getattr(exc, "detail", ""))})
        else:
            yield ("error", str(exc))
    except HraBlockedError as exc:
        # The token was never checked -- a new one would not help.
        yield ("error", str(exc))
    finally:
        client.close()


# How long the relay will sit on an empty queue before coming up for air.
# It has to be bounded: a blocking get() means this generator is parked inside
# a worker thread, so the GeneratorExit that a disconnect triggers has no yield
# point to land on -- and the turn would keep running until its next event,
# which for one long model call is the end of the answer. Measured: without
# this, killing the reader left `claude` running.
_RELAY_TICK_SECONDS = 1.0


def _relay(q: queue.Queue[tuple[str, Any] | None]) -> Iterator[str]:
    """Turn queued events into SSE, waking often enough to notice a disconnect.

    The idle yield is an SSE comment: valid, ignored by every reader, and it
    doubles as the keep-alive that stops a proxy timing out a slow turn.
    """
    while True:
        try:
            item = q.get(timeout=_RELAY_TICK_SECONDS)
        except queue.Empty:
            yield ": keep-alive\n\n"
            continue
        if item is None:
            yield _sse("done", {})
            return
        yield _sse(item[0], item[1])


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _sse_worker(work: Any, *, prompts: dict[str, str] | None = None) -> Iterator[str]:
    """Relay a blocking generator of (event, payload) pairs as SSE.

    The work is blocking (subprocess calls, Gmail and admin-API I/O) and can
    take minutes, so it cannot run on the event loop.

    If the reader goes away -- Stop pressed, tab closed, connection dropped --
    the generator is closed and the `finally` fires the cancel token, which the
    worker's model call polls and acts on by killing its subprocess. Without
    that, "stop" would only stop the browser listening while the machine kept
    paying for an answer nobody will read.
    """
    q: queue.Queue[tuple[str, Any] | None] = queue.Queue()
    cancel = threading.Event()

    def run() -> None:
        CANCEL.set(cancel)
        # A worker thread does not inherit the request's context, so the
        # edited prompts are set here rather than where they arrived.
        set_overrides(prompts)
        set_trace_sink(lambda entry: q.put(("trace", entry)))
        try:
            for event in work():
                q.put(event)
                if cancel.is_set():
                    break
        except Cancelled:
            pass  # the reader asked for this
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            if not cancel.is_set():
                q.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()

    try:
        yield from _relay(q)
    finally:
        cancel.set()


def _stream(
    c: Config,
    message: str,
    history: list[dict[str, Any]],
    cancel: threading.Event | None = None,
    *,
    prompts: dict[str, str] | None = None,
) -> Iterator[str]:
    """Run the turn on a worker thread, relaying progress lines as SSE events.

    The work is blocking (subprocess calls, Gmail I/O) and can take minutes, so
    it cannot run on the event loop.
    """
    q: queue.Queue[tuple[str, Any] | None] = queue.Queue()
    # Supplied by the endpoint so a newer turn can cancel this one.
    cancel = cancel or threading.Event()

    def work() -> None:
        CANCEL.set(cancel)
        set_overrides(prompts)
        # What the turn does, as it does it -- the UI shows it in the
        # "Thinking" accordion. Pushed through the same queue as everything
        # else, so it arrives in order with the statuses it explains.
        set_trace_sink(lambda entry: q.put(("trace", entry)))
        try:
            q.put(("status", "Understanding the request…"))
            intent = parse_intent(
                message,
                c.categories,
                history=history,
                model=c.summarizer.model,
                timeout_seconds=60,
                cfg=c,
            )
            q.put(("intent", asdict(intent)))
            for event in actions.run(c, intent, message=message, history=history):
                q.put(event)
                if cancel.is_set():
                    break
        except Cancelled:
            pass  # the reader asked for this
        except IntentError as exc:
            q.put(("error", f"Could not understand that: {exc}"))
        except FileNotFoundError as exc:
            q.put(("error", str(exc)))
        except (TimeoutError, ConnectionError) as exc:
            q.put((
                "error",
                "Lost the connection to Gmail partway through "
                f"({type(exc).__name__}: {exc}). It retried and still failed — "
                "try again.",
            ))
        except Exception as exc:  # noqa: BLE001 - surface anything to the UI
            if not cancel.is_set():
                q.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            q.put(None)

    threading.Thread(target=work, daemon=True).start()

    try:
        yield from _relay(q)
    finally:
        # Fires on a clean finish too, which is harmless, and on the reader
        # disappearing, which is the case that matters.
        cancel.set()
