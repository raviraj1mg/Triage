"""The fetch -> summarize -> cache orchestration, shared by the CLI and the chat server.

Kept out of cli.py so the chat server does not have to reimplement it, and so
progress can be streamed to whatever front end is driving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator, Sequence

from .config import Config
from .gmail_client import GmailClient
from .models import EmailThread
from .query import build_query
from .streaming import Cancelled, cancelled
from .store import Store
from .summarizer import Summarizer, SummarizerError

ProgressFn = Callable[[str], None]


@dataclass
class RunResult:
    summaries: list[dict[str, Any]]
    query: str
    fetched: int
    summarized: int
    cached: int
    failed: int
    errors: list[str]


def _noop(_msg: str) -> None:
    pass


def list_thread_ids(cfg: Config, client: GmailClient) -> tuple[list[str], str]:
    """One cheap API call for the ids, so work can start before every body is in."""
    q = build_query(cfg.gmail)
    ids = client.list_thread_ids(
        q,
        max_threads=cfg.gmail.max_threads,
        include_spam_trash=cfg.gmail.include_spam_trash,
    )
    return ids, q


def fetch_threads(
    cfg: Config,
    client: GmailClient,
    *,
    on_progress: ProgressFn | None = None,
) -> tuple[list[EmailThread], str]:
    progress = on_progress or _noop
    q = build_query(cfg.gmail)
    progress(f"Searching Gmail: {q}")
    threads = list(
        client.iter_threads(
            q,
            max_threads=cfg.gmail.max_threads,
            include_spam_trash=cfg.gmail.include_spam_trash,
        )
    )
    progress(f"Found {len(threads)} thread(s).")
    return threads, q


def summarize_threads_stream(
    cfg: Config,
    threads: Iterable[EmailThread],
    summarizer: Summarizer,
    store: Store,
    *,
    query: str = "",
    force: bool = False,
    total: int | None = None,
) -> Iterator[tuple[str, Any]]:
    """Yield ("status", str) and ("summary", dict) as work completes, then ("done", RunResult).

    A generator rather than a callback so the chat server can relay each card
    the moment its thread finishes, instead of after all of them.
    """
    summaries: list[dict[str, Any]] = []
    errors: list[str] = []
    n_cached = n_new = 0
    # `threads` may be a generator that fetches each thread on demand, so the
    # count cannot be derived from it.
    if total is None:
        total = len(threads) if isinstance(threads, Sequence) else 0
    n_seen = 0

    for i, th in enumerate(threads, 1):
        # A window can be thirty threads and a model call each. Once the reader
        # has gone, the rest of the window is work nobody asked for any more.
        if cancelled():
            raise Cancelled("stopped")
        n_seen = i
        label = th.subject[:70] or "(no subject)"
        cached = store.get(th.id)
        if not force and cached and store.is_current(th.id, th.last.id):
            n_cached += 1
            yield ("status", f"({i}/{total}) cached: {label}")
            summaries.append(cached)
            yield ("summary", cached)
            continue

        yield ("status", f"({i}/{total}) summarizing: {label}")
        try:
            summary = summarizer.summarize(th)
        except SummarizerError as exc:
            errors.append(f"{label}: {exc}")
            yield ("status", f"({i}/{total}) failed: {label}")
            continue
        store.upsert(summary, th.last.id)
        row = summary.to_dict()
        summaries.append(row)
        n_new += 1
        yield ("summary", row)

    yield (
        "done",
        RunResult(
            summaries=summaries,
            query=query,
            fetched=n_seen,
            summarized=n_new,
            cached=n_cached,
            failed=len(errors),
            errors=errors,
        ),
    )


def summarize_threads(
    cfg: Config,
    threads: Sequence[EmailThread],
    summarizer: Summarizer,
    store: Store,
    *,
    query: str = "",
    force: bool = False,
    on_progress: ProgressFn | None = None,
) -> RunResult:
    """Drain the stream and return the final result. Used by the CLI."""
    progress = on_progress or _noop
    result: RunResult | None = None
    for kind, payload in summarize_threads_stream(
        cfg, threads, summarizer, store, query=query, force=force
    ):
        if kind == "status":
            progress(payload)
        elif kind == "done":
            result = payload
    assert result is not None
    return result
