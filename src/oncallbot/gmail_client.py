"""Thin Gmail wrapper: find matching threads, flatten them into EmailThreads."""

from __future__ import annotations

import base64
import http.client
import logging
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator, TypeVar

from googleapiclient.errors import HttpError

from .models import Attachment, EmailMessage, EmailThread

logger = logging.getLogger(__name__)
T = TypeVar("T")

# Gmail reads are interleaved with multi-second model calls, so an idle
# keep-alive connection can go stale between them. Retry the transient cases.
_RETRY_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.5
_TRANSIENT_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)
_BLANKS_RE = re.compile(r"\n{3,}")


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout, ConnectionError, ssl.SSLError)):
        return True
    if isinstance(exc, HttpError):
        return getattr(exc.resp, "status", None) in _TRANSIENT_STATUSES
    # httplib2 raises this when a pooled connection was closed under it.
    return isinstance(exc, http.client.HTTPException)


def _with_retry(what: str, call: Callable[[], T]) -> T:
    last: BaseException | None = None
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - re-raised below when fatal
            if not _is_transient(exc) or attempt == _RETRY_ATTEMPTS:
                raise
            last = exc
            logger.warning(
                "%s failed (%s: %s); retrying %d/%d",
                what, type(exc).__name__, exc, attempt, _RETRY_ATTEMPTS - 1,
            )
            time.sleep(_RETRY_BACKOFF_SECONDS * attempt)
    raise AssertionError(f"unreachable; last error was {last}")


class GmailClient:
    def __init__(self, service: Any) -> None:
        self._svc = service

    def list_thread_ids(
        self, query: str, *, max_threads: int = 50, include_spam_trash: bool = False
    ) -> list[str]:
        ids: list[str] = []
        page_token: str | None = None
        while len(ids) < max_threads:
            resp = _with_retry(
                "threads.list",
                lambda: self._svc.users()
                .threads()
                .list(
                    userId="me",
                    q=query,
                    maxResults=min(100, max_threads - len(ids)),
                    pageToken=page_token,
                    includeSpamTrash=include_spam_trash,
                )
                .execute(),
            )
            ids.extend(t["id"] for t in resp.get("threads", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return ids[:max_threads]

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Raw attachment bytes. Not included in thread.get, needs its own call."""
        raw = _with_retry(
            f"attachments.get({attachment_id[:12]}…)",
            lambda: self._svc.users()
            .messages()
            .attachments()
            .get(userId="me", messageId=message_id, id=attachment_id)
            .execute(),
        )
        return base64.urlsafe_b64decode(raw["data"].encode())

    def get_thread(self, thread_id: str) -> EmailThread:
        raw = _with_retry(
            f"threads.get({thread_id})",
            lambda: self._svc.users()
            .threads()
            .get(userId="me", id=thread_id, format="full")
            .execute(),
        )
        messages = [_parse_message(m) for m in raw.get("messages", [])]
        messages.sort(key=lambda m: m.date or datetime.min.replace(tzinfo=timezone.utc))
        subject = next((m.subject for m in messages if m.subject), "(no subject)")
        label_ids = sorted({lid for m in raw.get("messages", []) for lid in m.get("labelIds", [])})
        return EmailThread(id=thread_id, subject=subject, messages=messages, label_ids=label_ids)

    def iter_threads(
        self, query: str, *, max_threads: int = 50, include_spam_trash: bool = False
    ) -> Iterator[EmailThread]:
        for tid in self.list_thread_ids(
            query, max_threads=max_threads, include_spam_trash=include_spam_trash
        ):
            yield self.get_thread(tid)


def _parse_message(msg: dict[str, Any]) -> EmailMessage:
    payload = msg.get("payload", {})
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}

    body_text, attachments = _walk_payload(payload, msg["id"])

    return EmailMessage(
        id=msg["id"],
        thread_id=msg["threadId"],
        date=_parse_date(headers.get("date"), msg.get("internalDate")),
        sender=headers.get("from", ""),
        to=headers.get("to", ""),
        cc=headers.get("cc", ""),
        subject=headers.get("subject", ""),
        body_text=_tidy(body_text),
        snippet=msg.get("snippet", ""),
        attachments=attachments,
    )


def _walk_payload(part: dict[str, Any], message_id: str = "") -> tuple[str, list[Attachment]]:
    """Prefer text/plain; fall back to de-tagged text/html. Collect attachments."""
    plain: list[str] = []
    html: list[str] = []
    attachments: list[Attachment] = []

    def walk(p: dict[str, Any]) -> None:
        mime = p.get("mimeType", "")
        body = p.get("body", {})
        filename = p.get("filename") or ""

        if filename:
            attachments.append(
                Attachment(
                    filename=filename,
                    mime_type=mime,
                    size_bytes=int(body.get("size", 0) or 0),
                    attachment_id=str(body.get("attachmentId") or ""),
                    message_id=message_id,
                )
            )
        elif mime == "text/plain" and body.get("data"):
            plain.append(_b64(body["data"]))
        elif mime == "text/html" and body.get("data"):
            html.append(_html_to_text(_b64(body["data"])))

        for child in p.get("parts", []) or []:
            walk(child)

    walk(part)
    return ("\n".join(plain) if plain else "\n".join(html)), attachments


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    import html as html_mod

    text = _STYLE_RE.sub("", html)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.IGNORECASE)
    text = _TAG_RE.sub("", text)
    return html_mod.unescape(text)


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    lines = [ln.rstrip() for ln in text.split("\n")]
    return _BLANKS_RE.sub("\n\n", "\n".join(lines)).strip()


def _parse_date(header: str | None, internal_ms: str | None) -> datetime | None:
    if header:
        try:
            return parsedate_to_datetime(header)
        except (TypeError, ValueError):
            pass
    if internal_ms:
        try:
            return datetime.fromtimestamp(int(internal_ms) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            pass
    return None
