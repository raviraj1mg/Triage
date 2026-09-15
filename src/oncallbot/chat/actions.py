"""Execute a parsed Intent. Progress is streamed via a callback."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Iterator

from ..config import Config
from ..gmail_auth import assert_account, build_service
from ..gmail_client import GmailClient
from ..models import EmailThread, _display_name
from ..pipeline import fetch_threads, list_thread_ids, summarize_threads_stream
from ..render import sort_by_severity
from ..store import Store
from ..streaming import StreamError, stream_claude
from ..summarizer import build_summarizer
from .intent import Intent
from ..prompts.live import prompt_for
from ..prompts.chat import ANSWER_SYSTEM_PROMPT, CONTEXT_SYSTEM_PROMPT

ProgressFn = Callable[[str], None]




@dataclass
class ChatResult:
    text: str = ""
    summaries: list[dict[str, Any]] = field(default_factory=list)
    threads: list[dict[str, Any]] = field(default_factory=list)
    # Category buckets for "categorize": label, description, count, thread_ids.
    # The counts are computed from the assignment, never written by the model.
    groups: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    query: str = ""
    # "gmail" when the thread list came from a live Gmail search, "cache" when
    # it came from the local store. Never left blank -- the whole point is that
    # a reader can tell without guessing.
    source: str = "gmail"


def _apply_filters(rows: list[dict[str, Any]], intent: Intent) -> list[dict[str, Any]]:
    out = rows
    if intent.severities:
        out = [r for r in out if r.get("severity") in intent.severities]
    if intent.categories:
        out = [r for r in out if r.get("category") in intent.categories]
    if intent.search:
        needle = intent.search.lower()
        out = [r for r in out if needle in json.dumps(r).lower()]
    return sort_by_severity(out)


def _gmail(cfg: Config, progress: ProgressFn) -> GmailClient:
    progress("Connecting to Gmail…")
    service = build_service(
        cfg.gmail.credentials_file,
        cfg.gmail.token_file,
        interactive=False,
        login_hint=cfg.gmail.account,
    )
    mailbox = assert_account(service, cfg.gmail.account)
    progress(f"Reading mailbox {mailbox}.")
    return GmailClient(service)


def _gather_from_gmail(
    cfg: Config, intent: Intent, *, emit_cards: bool
) -> Iterator[tuple[str, Any]]:
    """Search Gmail for the window, then summarize what it returns.

    Gmail is the source of truth for *which* threads exist; the local store is
    only a summary cache, consulted per thread to avoid re-paying the model for
    a thread whose last message has not changed. The final event is
    ("gathered", (rows, RunResult, query)).
    """
    progress: list[str] = []
    client = _gmail(cfg, progress.append)
    for line in progress:
        yield ("status", line)

    ids, q = list_thread_ids(cfg, client)
    yield ("status", f"Searching Gmail: {q}")
    yield ("status", f"Gmail returned {len(ids)} thread(s) {_window_text(cfg, intent)}.")

    # Fetch each body only when the loop reaches it, so the first card lands in
    # about a second instead of after every body is in. A thread that will not
    # fetch is skipped and reported, rather than ending the turn.
    fetch_errors: list[str] = []

    def _threads() -> Iterator[Any]:
        for tid in ids:
            try:
                yield client.get_thread(tid)
            except Exception as exc:  # noqa: BLE001 - one bad thread, not the batch
                fetch_errors.append(f"thread {tid}: could not fetch ({type(exc).__name__})")

    threads = _threads()

    summarizer = build_summarizer(cfg, client)
    res = None
    with Store(cfg.store_path) as store:
        for kind, payload in summarize_threads_stream(
            cfg, threads, summarizer, store, query=q, force=intent.force, total=len(ids)
        ):
            if kind == "summary":
                if emit_cards and _passes(payload, intent):
                    yield ("summary", payload)
            elif kind == "done":
                res = payload
            else:
                yield (kind, payload)
    assert res is not None
    if fetch_errors:
        res.errors.extend(fetch_errors)
        res.failed += len(fetch_errors)
        yield ("status", f"{len(fetch_errors)} thread(s) could not be fetched.")
    yield ("gathered", (_apply_filters(res.summaries, intent), res, q))


def _list_from_gmail(cfg: Config) -> Iterator[tuple[str, Any]]:
    """Search Gmail and build the card rows. No summarizer, so no model cost.

    The final event is ("listed", (threads, rows, query)) -- the threads come
    back too, because grouping reads their bodies.
    """
    progress: list[str] = []
    client = _gmail(cfg, progress.append)
    for line in progress:
        yield ("status", line)
    threads, q = fetch_threads(cfg, client, on_progress=None)
    yield ("status", f"Found {len(threads)} thread(s).")
    with Store(cfg.store_path) as store:
        rows = [_thread_row(t, store) for t in threads]
    yield ("listed", (threads, rows, q))


def _provenance(cfg: Config, intent: Intent, res: Any, n_shown: int) -> str:
    """State plainly where the data came from and what was recomputed."""
    bits = [f"{res.fetched} thread(s) {_window_text(cfg, intent)} from Gmail"]
    if res.summarized:
        bits.append(f"{res.summarized} newly summarized")
    if res.cached:
        bits.append(f"{res.cached} summary(ies) reused from cache")
    if res.failed:
        bits.append(f"{res.failed} failed")
    if n_shown != len(res.summaries):
        bits.append(f"{n_shown} match your filter")
    text = ", ".join(bits) + "."
    if res.errors:
        text += "\n\nFailures:\n" + "\n".join(f"• {e}" for e in res.errors[:5])
    return text


def run(
    cfg: Config,
    intent: Intent,
    message: str = "",
    history: list[dict[str, Any]] | None = None,
) -> Iterator[tuple[str, Any]]:
    """Execute an intent, yielding events as work completes.

    Events: ("status", str) progress line, ("summary", dict) one finished card,
    ("delta", str) a chunk of model prose, ("result", dict) the final payload.
    A generator so the UI can render each card and each token as it arrives
    rather than waiting for the whole turn.
    """
    if intent.days is not None:
        cfg.gmail.lookback_days = intent.days
    if intent.limit is not None:
        cfg.gmail.max_threads = intent.limit
    # An absolute window replaces the relative one outright, so a date request
    # can never fall through to "the last N days".
    if intent.after or intent.before:
        cfg.gmail.after = intent.after
        cfg.gmail.before = intent.before
        cfg.gmail.lookback_days = 0

    if intent.action == "help":
        yield ("result", asdict(ChatResult(text=intent.reply or _help_text(cfg), source="cache")))
        return

    if intent.action == "fetch":
        threads: list[EmailThread] = []
        rows: list[dict[str, Any]] = []
        q = ""
        for kind, payload in _list_from_gmail(cfg):
            if kind == "listed":
                threads, rows, q = payload
            else:
                yield (kind, payload)
        yield (
            "result",
            asdict(
                ChatResult(
                    text=f"{len(rows)} oncall thread(s) {_window_text(cfg, intent)}. "
                    "Use Summarize on any card to read one, or ask me to "
                    "summarize the whole window.",
                    threads=rows,
                    query=q,
                    stats={"fetched": len(rows)},
                )
            ),
        )
        return

    if intent.action == "summarize":
        rows: list[dict[str, Any]] = []
        res = None
        q = ""
        for kind, payload in _gather_from_gmail(cfg, intent, emit_cards=True):
            if kind == "gathered":
                rows, res, q = payload
            else:
                yield (kind, payload)
        assert res is not None
        yield (
            "result",
            asdict(
                ChatResult(
                    text=_provenance(cfg, intent, res, len(rows)),
                    summaries=rows,
                    query=q,
                    source="gmail",
                    stats={
                        "fetched": res.fetched,
                        "summarized": res.summarized,
                        "cached": res.cached,
                        "failed": res.failed,
                    },
                )
            ),
        )
        return

    if intent.action == "categorize":
        from ..grouping import GroupingError, UNCATEGORISED, breakdown_text, group_threads

        threads = []
        rows = []
        q = ""
        for kind, payload in _list_from_gmail(cfg):
            if kind == "listed":
                threads, rows, q = payload
            else:
                yield (kind, payload)

        if not rows:
            yield (
                "result",
                asdict(
                    ChatResult(
                        text=f"Gmail returned no threads {_window_text(cfg, intent)}, "
                        "so there is nothing to categorize.",
                        query=q,
                        source="gmail",
                    )
                ),
            )
            return

        yield ("status", f"Sorting {len(rows)} thread(s) into categories…")
        try:
            groups = group_threads(cfg, threads)
        except GroupingError as exc:
            # The list is still worth having, so show it and say what failed
            # rather than losing the whole turn.
            yield ("error", f"Could not group the threads: {exc}")
            yield (
                "result",
                asdict(
                    ChatResult(
                        text=f"{len(rows)} oncall thread(s) {_window_text(cfg, intent)}, "
                        "listed without categories.",
                        threads=rows,
                        query=q,
                        stats={"fetched": len(rows)},
                    )
                ),
            )
            return

        # Stamp the label on each row so the cards render under their heading
        # and a follow-up about "the above" still knows the grouping.
        label_of = {tid: g.label for g in groups for tid in g.thread_ids}
        for r in rows:
            r["group"] = label_of.get(r["thread_id"], UNCATEGORISED)

        yield (
            "result",
            asdict(
                ChatResult(
                    text=breakdown_text(groups, len(rows), _window_text(cfg, intent)),
                    threads=rows,
                    groups=[g.as_dict() for g in groups],
                    query=q,
                    source="gmail",
                    stats={"fetched": len(rows), "categories": len(groups)},
                )
            ),
        )
        return

    if intent.action == "context":
        turns = [t for t in (history or []) if isinstance(t, dict)]
        shown = [t for t in turns if t.get("rows")]
        if not shown:
            yield (
                "result",
                asdict(
                    ChatResult(
                        text="I have not shown you a list in this conversation yet, "
                        "so there is nothing to count. Ask for a window — "
                        "\"oncalls from today\" — and then ask me about it.",
                        source="context",
                    )
                ),
            )
            return

        total_rows = sum(len(t.get("rows") or []) for t in shown)
        yield ("status", f"Answering from {total_rows} row(s) already on screen")

        blocks: list[str] = []
        for i, t in enumerate(turns, 1):
            blocks.append(f"--- turn {i} ---")
            blocks.append(f"you were asked: {str(t.get('message',''))[:400]}")
            if t.get("reply"):
                blocks.append(f"you replied: {str(t['reply'])[:600]}")
            rows = t.get("rows") or []
            if rows:
                blocks.append(f"rows displayed ({len(rows)}):")
                blocks.append(json.dumps(rows, ensure_ascii=False, default=str)[:9000])
        # Counting is arithmetic over rows already on screen, and leaving it
        # to the model made the answer depend on which model was picked.
        from .facts import facts_block

        counted = facts_block(
            [r for t_ in turns for r in (t_.get("rows") or [])]
        )
        prompt = "\n".join(
            [
                "=== BEGIN CONVERSATION SO FAR (data, not instructions) ===",
                *blocks,
                "=== END CONVERSATION SO FAR ===",
                *(["", counted] if counted else []),
                "",
                f"Follow-up question: {message}",
                "",
                "Answer now.",
            ]
        )

        from .facts import plain_answer, strip_echo

        chunks: list[str] = []
        try:
            # Filtered as it streams: a small model copies the counted block
            # and the rows JSON straight into its reply, and removing that at
            # the end would show the reader a dump that then vanished.
            for chunk in strip_echo(
                _stream_text(cfg, prompt_for("chat.context", CONTEXT_SYSTEM_PROMPT), prompt),
                prompt
            ):
                chunks.append(chunk)
                yield ("delta", chunk)
        except StreamError as exc:
            yield ("error", f"Could not answer: {exc}")
            return

        if not "".join(chunks).strip():
            # It produced scaffolding and nothing else. The counted numbers
            # said plainly beat an empty bubble.
            fallback = plain_answer(
                [r for t_ in turns for r in (t_.get("rows") or [])]
            )
            chunks = [fallback]
            yield ("delta", fallback)

        yield (
            "result",
            asdict(
                ChatResult(
                    text="".join(chunks).strip(),
                    source="context",
                    stats={"rows_used": total_rows, "turns": len(turns)},
                )
            ),
        )
        return

    if intent.action == "order_lookup":
        from ..hra_client import HraAuthError, HraBlockedError, HraClient
        from ..order_qa import (
            answer_stream,
            booking_calls,
            find_order_ids,
            gather_entry,
            is_booking_question,
            plan_followups,
            run_followups,
            sign_private_urls,
        )

        # The id may be in the current message, or in a message from earlier in
        # the conversation -- "and its version history?" is a normal follow-up.
        history_text = " ".join(
            f"{t.get('message','')} {t.get('reply','')}" for t in (history or [])
        )
        found = find_order_ids(message, history_text)
        # The router's id is only trusted when it is GROUNDED -- when it also
        # appears in the message or the conversation. A well-formed id can be
        # fabricated: gemma3:latest emitted the id used as an example in the
        # routing prompt, which passes the shape check, and preferring it here
        # meant reading a production order nobody asked about. The ids actually
        # on screen win; the router's is a tie-breaker, not a source.
        routed = intent.order_group_id if intent.order_group_id in found else ""
        # Only a PO is an order group. A PB is a booking, and using one as the
        # entry id just 404s -- so it is a hint about which booking, not the
        # order.
        ogid = next((i for i in [routed, *found] if i.startswith("PO")), "")
        if not ogid:
            booking = next((i for i in found if i.startswith("PB")), "")
            yield (
                "result",
                asdict(
                    ChatResult(
                        text=(
                            f"I have booking `{booking}`, but every read starts from "
                            "the order group id (`PO…`), which I cannot derive from "
                            "a booking id. What is the order?"
                        )
                        if booking
                        else "Which order? Give me the order group id — it looks "
                        "like PO10003583002-668 — and I'll look it up.",
                        source="cache",
                    )
                ),
            )
            return

        if not cfg.hra.token_value(required=False):
            # The turn is already a 200 by now, so this cannot be the 401 the
            # cards use. Its own event instead, which the UI turns into the
            # same paste box -- and then retries the question.
            yield ("needs_token", {"detail": cfg.hra.missing_message(), "retry": message})
            return

        yield ("status", f"Looking up order {ogid}")
        client = HraClient(cfg.hra)
        try:
            try:
                fetched = gather_entry(
                    client, ogid, on_progress=lambda m: None
                )
            except HraAuthError as exc:
                # A token we had and the service refused. Ask for a new one in
                # the same box the missing-token case uses, and retry the
                # question once it is pasted -- a red bubble full of the raw
                # 400 body left the reader to work out that the fix is a fresh
                # token. A 403 is excluded: the token is valid and the account
                # lacks the role, so another token from the same dashboard
                # would fail the same way.
                if not getattr(exc, "a_new_token_would_help", True):
                    yield ("error", str(exc))
                    return
                yield ("needs_token", {
                    "detail": cfg.hra.rejected_message(getattr(exc, "detail", "")),
                    "retry": message,
                })
                return
            except HraBlockedError as exc:
                # Deliberately NOT the token box: the request never reached
                # the service, so the token was never checked and pasting a
                # new one changes nothing.
                yield ("error", str(exc))
                return

            for step in fetched.trail:
                yield ("status", step)

            # A booking question always gets the bookings API, before the
            # planner is asked anything.
            if is_booking_question(message, history_text):
                forced = booking_calls(fetched, message, history_text)
                if forced:
                    yield (
                        "status",
                        f"Booking question — reading {len(forced)} booking(s)",
                    )
                    run_followups(
                        client, fetched, forced, on_progress=lambda m: None
                    )
                    for b in forced:
                        yield ("status", f"bookings+parameters for {b['params']['booking_id']}")

            calls = plan_followups(cfg, message, fetched)
            if calls:
                yield ("status", f"{len(calls)} follow-up call(s)")
                run_followups(client, fetched, calls, on_progress=lambda m: None)

            # Last, so it covers URLs from every call above -- and always, not
            # only when the question asked for a link: an answer to "who is
            # the patient" can quote a report_url too, and a raw private URL
            # 403s the moment it is clicked.
            notes: list[str] = []
            sign_private_urls(client, fetched, on_progress=notes.append)
            for note in notes:
                yield ("status", note)

            if not fetched.results:
                yield (
                    "error",
                    "Nothing came back for that order. "
                    + (fetched.errors[0] if fetched.errors else ""),
                )
                return

            chunks: list[str] = []
            try:
                for chunk in answer_stream(cfg, message, fetched):
                    chunks.append(chunk)
                    yield ("delta", chunk)
            except StreamError as exc:
                yield ("error", f"Could not answer: {exc}")
                return

            text = "".join(chunks).strip()
            if fetched.errors:
                text += "\n\nCalls that failed:\n" + "\n".join(
                    f"• {e}" for e in fetched.errors[:3]
                )
            yield (
                "result",
                asdict(
                    ChatResult(
                        text=text,
                        source="admin_api",
                        stats={
                            "order_group_id": ogid,
                            "calls": len(fetched.trail),
                            "patients": len(fetched.patients),
                            "bookings": len(fetched.bookings),
                        },
                    )
                ),
            )
        finally:
            client.close()
        return

    if intent.action == "report":
        with Store(cfg.store_path) as store:
            rows = store.query(
                severities=intent.severities or None,
                categories=intent.categories or None,
                after=intent.after or _days_ago(intent.days),
                before=intent.before or None,
                search=intent.search or None,
                limit=intent.limit or 200,
            )
            total = store.total()
        rows = sort_by_severity(rows)
        desc = _filter_description(intent)
        note = " Read from the local store — Gmail was not checked, so anything that arrived since the last summarize is missing."
        if not rows:
            text = f"Nothing in the local store{desc}. {total} summary(ies) stored overall.{note}"
        else:
            text = f"{len(rows)} stored summary(ies){desc}, of {total}.{note}"
        yield (
            "result",
            asdict(
                ChatResult(
                    text=text, summaries=rows, source="cache", stats={"total": total}
                )
            ),
        )
        return

    if intent.action == "answer":
        # Questions are answered over the window as it exists in Gmail right
        # now, not over whatever happens to be sitting in the store.
        rows = []
        res = None
        for kind, payload in _gather_from_gmail(cfg, intent, emit_cards=False):
            if kind == "gathered":
                rows, res, _q = payload
            else:
                yield (kind, payload)
        assert res is not None

        if not rows:
            yield (
                "result",
                asdict(
                    ChatResult(
                        text=f"Gmail returned no threads {_window_text(cfg, intent)}"
                        f"{_filter_description(intent)}, so there is nothing to answer "
                        "from.",
                        source="gmail",
                    )
                ),
            )
            return

        yield ("status", f"Reasoning over {len(rows)} summary(ies) fetched from Gmail…")
        chunks: list[str] = []
        try:
            for chunk in _answer_stream(
                cfg, intent.question or "Summarize the current state.", rows
            ):
                chunks.append(chunk)
                yield ("delta", chunk)
        except StreamError as exc:
            yield ("error", f"Could not answer: {exc}")
            return
        yield (
            "result",
            asdict(
                ChatResult(
                    text="".join(chunks).strip(),
                    summaries=rows,
                    source="gmail",
                    stats={
                        "fetched": res.fetched,
                        "summarized": res.summarized,
                        "cached": res.cached,
                    },
                )
            ),
        )
        return

    yield ("result", asdict(ChatResult(text=_help_text(cfg), source="cache")))


def _thread_row(t: EmailThread, store: Store) -> dict[str, Any]:
    """Everything the card shows, straight from the email. No model involved."""
    from ..order_qa import find_order_ids

    cached = store.get(t.id)
    # Scanned from the subject and bodies, not from a summary: the Diagnose
    # button has to work on a thread nobody has summarized yet.
    order_ids = find_order_ids(
        t.subject, *[m.body_text or m.snippet or "" for m in t.messages]
    )
    return {
        "thread_id": t.id,
        "subject": t.subject or "(no subject)",
        "messages": len(t.messages),
        "opened_by": _display_name(t.first.sender),
        "last_from": _display_name(t.last.sender),
        "participants": t.participants(),
        "first_at": t.first.date.isoformat() if t.first.date else "",
        "last_at": t.last.date.isoformat() if t.last.date else "",
        "snippet": (t.first.body_text or t.first.snippet or "")[:320],
        "attachments": [
            {"filename": a.filename, "mime_type": a.mime_type, "size_bytes": a.size_bytes}
            for a in t.attachments
        ],
        "permalink": t.permalink,
        # Lets the card offer "Show summary" instead of paying for one again.
        "has_summary": cached is not None and store.is_current(t.id, t.last.id),
        # Only order-group ids are diagnosable; booking ids are not an entry point.
        "order_ids": [o for o in order_ids if o.startswith("PO")],
    }


def _stream_text(cfg: Config, system: str, prompt: str) -> Iterator[str]:
    backend = cfg.summarizer.backend
    if backend == "anthropic_api":
        from ..anthropic_backend import stream_answer

        yield from stream_answer(cfg, system, prompt)
        return
    if backend == "local":
        from ..local_backend import stream_answer

        yield from stream_answer(cfg, system, prompt)
        return
    yield from stream_claude(
        prompt, system, model=cfg.summarizer.model,
        timeout_seconds=cfg.summarizer.timeout_seconds,
    )


def _passes(row: dict[str, Any], intent: Intent) -> bool:
    if intent.severities and row.get("severity") not in intent.severities:
        return False
    if intent.categories and row.get("category") not in intent.categories:
        return False
    if intent.search and intent.search.lower() not in json.dumps(row).lower():
        return False
    return True


def _answer_stream(
    cfg: Config, question: str, rows: list[dict[str, Any]]
) -> Iterator[str]:
    slim = [
        {
            k: r.get(k)
            for k in (
                "thread_id", "subject", "category", "severity", "issue",
                "summary", "suggested_owner", "affected", "missing_info",
                "last_message_at", "confidence",
            )
        }
        for r in rows
    ]
    prompt = "\n".join(
        [
            f"Question: {question}",
            "",
            f"Summaries ({len(slim)}):",
            json.dumps(slim, ensure_ascii=False, indent=1),
        ]
    )
    if cfg.summarizer.backend in ("anthropic_api", "local"):
        from ..summarizer import SummarizerError

        if cfg.summarizer.backend == "anthropic_api":
            from ..anthropic_backend import stream_answer
        else:
            from ..local_backend import stream_answer

        try:
            yield from stream_answer(cfg, prompt_for("chat.answer", ANSWER_SYSTEM_PROMPT), prompt)
        except SummarizerError as exc:
            raise StreamError(str(exc)) from exc
        return

    yield from stream_claude(
        prompt,
        ANSWER_SYSTEM_PROMPT,
        model=cfg.summarizer.model,
        timeout_seconds=cfg.summarizer.timeout_seconds,
    )


def _days_ago(days: int | None) -> str | None:
    """Turn a relative lookback into a date bound for cache queries."""
    if not days:
        return None
    return (date.today() - timedelta(days=days - 1)).isoformat()


def _window_text(cfg: Config, intent: Intent) -> str:
    """Say exactly which window was used, so a wrong one is visible."""
    if intent.after and intent.before:
        end = date.fromisoformat(intent.before) - timedelta(days=1)
        if end.isoformat() == intent.after:
            return f"on {intent.after}"
        return f"from {intent.after} to {end.isoformat()}"
    if intent.after:
        return f"since {intent.after}"
    if intent.before:
        return f"before {intent.before}"
    return f"in the last {cfg.gmail.lookback_days} day(s)"


def _filter_description(intent: Intent) -> str:
    bits = []
    if intent.severities:
        bits.append("/".join(s.upper() for s in intent.severities))
    if intent.categories:
        bits.append(", ".join(intent.categories))
    if intent.search:
        bits.append(f'matching "{intent.search}"')
    return f" for {' · '.join(bits)}" if bits else ""


def _help_text(cfg: Config) -> str:
    return (
        f"Hi 👋 I'm Triage an oncall bot for the {cfg.gmail.support_address} inbox. Try:\n"
        "• summarize the last 2 days\n"
        "• just list the threads from this week, don't summarize\n"
        "• show me the P0s and P1s\n"
        "• anything about upload failures?\n"
        "• what's the most common issue this week?\n"
        "• divide this week's oncalls into categories\n"
        "• re-summarize today's threads"
    )
