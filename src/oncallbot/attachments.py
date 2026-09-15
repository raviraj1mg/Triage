"""Download thread attachments to an isolated directory for the model to read.

Two things drive the design.

Filenames come from email, so they are attacker-controlled: every name is
reduced to a safe basename before it touches the filesystem. And the content is
unredactable -- a lab report PDF cannot be masked the way a phone number in a
body can -- so type, size and count are all capped, and the whole thing is
switchable off. See docs/phi.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .config import AttachmentConfig
from .gmail_client import GmailClient
from .models import EmailThread

logger = logging.getLogger(__name__)

# Types the model can actually make use of. Everything else is named in the
# prompt but not downloaded.
READABLE_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/csv": ".csv",
}

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_LEN = 60


@dataclass
class SavedAttachment:
    path: Path
    original_filename: str
    mime_type: str
    size_bytes: int

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class SkippedAttachment:
    original_filename: str
    mime_type: str
    size_bytes: int
    reason: str


def safe_name(filename: str, mime_type: str, taken: set[str]) -> str:
    """Reduce an email-supplied filename to something safe and unique.

    Strips any directory component (so "../../.ssh/authorized_keys" becomes
    "authorized_keys"), limits the character set, and appends a counter on
    collision.
    """
    # Both separators: a Windows-authored name can carry backslashes.
    base = filename.replace("\\", "/").split("/")[-1].strip()
    stem, _, ext = base.rpartition(".")
    if not stem:
        stem, ext = base, ""

    stem = _SAFE_CHARS.sub("_", stem).strip("._-")[:_MAX_NAME_LEN] or "attachment"
    ext = _SAFE_CHARS.sub("", ext).lower()[:8]

    # Trust the declared type over the claimed extension.
    expected = READABLE_TYPES.get(mime_type, "")
    if expected and f".{ext}" != expected:
        ext = expected.lstrip(".")

    candidate = f"{stem}.{ext}" if ext else stem
    n = 2
    while candidate in taken:
        candidate = f"{stem}-{n}.{ext}" if ext else f"{stem}-{n}"
        n += 1
    taken.add(candidate)
    return candidate


def download(
    client: GmailClient,
    thread: EmailThread,
    cfg: AttachmentConfig,
    dest: Path,
) -> tuple[list[SavedAttachment], list[SkippedAttachment]]:
    """Save eligible attachments into `dest`. Returns (saved, skipped)."""
    saved: list[SavedAttachment] = []
    skipped: list[SkippedAttachment] = []

    if not cfg.enabled:
        return saved, skipped

    taken: set[str] = set()
    total = 0

    for msg in thread.messages:
        for att in msg.attachments:
            def skip(reason: str) -> None:
                skipped.append(
                    SkippedAttachment(att.filename, att.mime_type, att.size_bytes, reason)
                )

            if len(saved) >= cfg.max_per_thread:
                skip(f"over the {cfg.max_per_thread}-attachment limit for one thread")
                continue
            if att.mime_type not in READABLE_TYPES:
                skip(f"type {att.mime_type} is not readable")
                continue
            if not att.attachment_id:
                skip("no attachment id in the message payload")
                continue
            if att.size_bytes > cfg.max_bytes_each:
                skip(f"{_mb(att.size_bytes)} exceeds the {_mb(cfg.max_bytes_each)} per-file cap")
                continue
            if total + att.size_bytes > cfg.max_total_bytes:
                skip(f"would exceed the {_mb(cfg.max_total_bytes)} per-thread total")
                continue

            try:
                data = client.get_attachment(att.message_id, att.attachment_id)
            except Exception as exc:  # noqa: BLE001 - one attachment, not the thread
                logger.warning("attachment %s failed: %s", att.filename, exc)
                skip(f"download failed ({type(exc).__name__})")
                continue

            # The declared size can lie; the bytes cannot.
            if len(data) > cfg.max_bytes_each or total + len(data) > cfg.max_total_bytes:
                skip("larger than declared, over the cap")
                continue

            name = safe_name(att.filename, att.mime_type, taken)
            path = dest / name
            path.write_bytes(data)
            total += len(data)
            saved.append(SavedAttachment(path, att.filename, att.mime_type, len(data)))

    return saved, skipped


def _mb(n: int) -> str:
    return f"{n / 1_048_576:.1f}MB"
