"""Anthropic API backend, for running without the Claude Code CLI.

Selected with `summarizer.backend: anthropic_api`. The API key comes from the
environment variable named by `summarizer.api_key_env`, never from config.yaml.

Two things are better here than in the CLI backend: the summary schema is
enforced server-side via structured outputs, so there is no JSON to salvage out
of prose; and attachments are sent as native image/document content blocks
rather than staged as files for a sandboxed tool to read.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Iterator

from .attachments import READABLE_TYPES, download
from .config import AttachmentConfig, Config, SummarizerConfig
from .gmail_client import GmailClient
from .models import EmailThread, IssueSummary
from .prompts.live import prompt_for
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .redact import redact
from .streaming import Cancelled, cancelled
from .summarizer import SummarizerError, _redacted_copy

# Types the API accepts inline. Text attachments are inlined into the prompt
# instead, since a document block would be wasteful for a few KB of plain text.
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/gif", "image/webp"}
_DOC_TYPES = {"application/pdf"}
_TEXT_TYPES = {"text/plain", "text/csv"}

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reporter": {"type": "string"},
        "summary": {"type": "string"},
        "issue": {"type": "string"},
        "category": {"type": "string"},
        "severity": {"type": "string", "enum": ["p0", "p1", "p2", "p3"]},
        "asks": {"type": "array", "items": {"type": "string"}},
        "missing_info": {"type": "array", "items": {"type": "string"}},
        "affected_entities": {
            "type": "object",
            "properties": {
                "patient_ids": {"type": "array", "items": {"type": "string"}},
                "order_ids": {"type": "array", "items": {"type": "string"}},
                "prescription_ids": {"type": "array", "items": {"type": "string"}},
                "record_ids": {"type": "array", "items": {"type": "string"}},
                "user_emails": {"type": "array", "items": {"type": "string"}},
                "other_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "patient_ids", "order_ids", "prescription_ids",
                "record_ids", "user_emails", "other_ids",
            ],
            "additionalProperties": False,
        },
        "suggested_owner": {"type": "string"},
        "confidence": {"type": "number"},
        # The closure read. The threshold on closure_confidence is applied in
        # code, in models.decide_closure -- never here.
        "closed": {"type": "boolean"},
        "closure_confidence": {"type": "number"},
        "closed_by": {"type": "string"},
        "closed_at": {"type": "string"},
        "closure_reason": {"type": "string"},
    },
    "required": [
        "reporter", "summary", "issue", "category", "severity", "asks",
        "missing_info", "affected_entities", "suggested_owner", "confidence",
        "closed", "closure_confidence", "closed_by", "closed_at",
        "closure_reason",
    ],
    "additionalProperties": False,
}


def build_client(cfg: SummarizerConfig) -> Any:
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise SummarizerError(
            "The `anthropic` package is not installed. Run `uv sync`."
        ) from exc
    return anthropic.Anthropic(api_key=cfg.api_key(), timeout=float(cfg.timeout_seconds))


def wrap_api_errors(exc: Exception) -> SummarizerError:
    """Turn SDK exceptions into something an operator can act on."""
    name = type(exc).__name__
    if name == "AuthenticationError":
        return SummarizerError(
            "The Anthropic API rejected the key. Check the value of the "
            "environment variable named by summarizer.api_key_env."
        )
    if name == "PermissionDeniedError":
        return SummarizerError("That API key lacks permission for this model.")
    if name == "NotFoundError":
        return SummarizerError(
            "Unknown model id. Check summarizer.model in config.yaml."
        )
    if name == "RateLimitError":
        return SummarizerError("Rate limited by the Anthropic API. Try again shortly.")
    if name == "APITimeoutError":
        return SummarizerError(
            "The Anthropic API call timed out. Raise summarizer.timeout_seconds."
        )
    if name == "APIConnectionError":
        return SummarizerError("Could not reach the Anthropic API. Check the network.")
    return SummarizerError(f"{name}: {exc}")


@dataclass
class AnthropicSummarizer:
    """Summarizes one thread per API call, with the schema enforced server-side."""

    summarizer: SummarizerConfig
    categories: tuple[str, ...] = ("other",)
    redaction_enabled: bool = True
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    gmail: GmailClient | None = None
    client: Any = None

    @classmethod
    def from_config(cls, cfg: Config, gmail: GmailClient | None = None) -> AnthropicSummarizer:
        return cls(
            summarizer=cfg.summarizer,
            categories=tuple(cfg.categories),
            redaction_enabled=cfg.redaction_enabled,
            attachments=cfg.attachments,
            gmail=gmail,
        )

    def _client(self) -> Any:
        if self.client is None:
            self.client = build_client(self.summarizer)
        return self.client

    @property
    def model_id(self) -> str:
        return self.summarizer.api_model

    def _request(
        self, thread: EmailThread
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """The content blocks and schema for one thread. Shared by both paths."""
        prepared = _redacted_copy(thread) if self.redaction_enabled else thread
        blocks, readable, skipped = self._attachment_blocks(thread)

        schema = dict(SUMMARY_SCHEMA)
        schema["properties"] = dict(schema["properties"])
        schema["properties"]["category"] = {
            "type": "string",
            "enum": list(self.categories),
        }

        prompt = build_user_prompt(
            prepared,
            list(self.categories),
            self.summarizer.max_body_chars,
            readable_attachments=readable,
            skipped_attachments=skipped,
        )
        return [*blocks, {"type": "text", "text": prompt}], schema

    def raw_stream(self, thread: EmailThread) -> Iterator[str]:
        """The schema-enforced JSON, yielded as the model writes it."""
        content, schema = self._request(thread)
        try:
            with self._client().messages.stream(
                model=self.summarizer.api_model,
                max_tokens=16000,
                system=prompt_for("summarize", SYSTEM_PROMPT),
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self.summarizer.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": content}],
            ) as stream:
                yield from stream.text_stream
        except Exception as exc:  # noqa: BLE001 - mapped to a readable message
            raise wrap_api_errors(exc) from exc

    def summarize(self, thread: EmailThread) -> IssueSummary:
        content, schema = self._request(thread)

        try:
            response = self._client().messages.create(
                model=self.summarizer.api_model,
                max_tokens=16000,
                system=prompt_for("summarize", SYSTEM_PROMPT),
                thinking={"type": "adaptive"},
                output_config={
                    "effort": self.summarizer.effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                messages=[{"role": "user", "content": content}],
            )
        except Exception as exc:  # noqa: BLE001 - mapped to a readable message
            raise wrap_api_errors(exc) from exc

        if getattr(response, "stop_reason", None) == "refusal":
            raise SummarizerError(
                "The model declined to summarize this thread "
                f"({getattr(getattr(response, 'stop_details', None), 'category', 'unspecified')})."
            )

        text = next(
            (b.text for b in response.content if getattr(b, "type", "") == "text"), ""
        )
        if not text.strip():
            raise SummarizerError("The API returned no text content for this thread.")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SummarizerError(f"Malformed JSON from the API: {exc}") from exc

        return IssueSummary.from_model_output(data, thread, self.summarizer.api_model)

    def _attachment_blocks(
        self, thread: EmailThread
    ) -> tuple[list[dict[str, Any]], list[tuple[str, str]], list[tuple[str, str]]]:
        """Fetch eligible attachments and turn them into content blocks."""
        present = [a for m in thread.messages for a in m.attachments]
        if not present:
            return [], [], []
        if not (self.attachments.enabled and self.gmail is not None):
            reason = (
                "attachment downloads are disabled"
                if not self.attachments.enabled
                else "no Gmail client available to fetch it"
            )
            return [], [], [(a.filename, reason) for a in present]

        import tempfile
        from pathlib import Path

        blocks: list[dict[str, Any]] = []
        readable: list[tuple[str, str]] = []

        # download() owns the size, count and filename rules; reuse it rather
        # than duplicating those controls for a second backend.
        with tempfile.TemporaryDirectory(prefix="oncallbot-api-att-") as tmp:
            saved, skipped_raw = download(
                self.gmail, thread, self.attachments, Path(tmp)
            )
            for item in saved:
                data = item.path.read_bytes()
                mime = item.mime_type
                if mime in _IMAGE_TYPES:
                    blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg" if mime == "image/jpg" else mime,
                            "data": base64.b64encode(data).decode(),
                        },
                    })
                    readable.append((item.name, f"{mime}, sent inline"))
                elif mime in _DOC_TYPES:
                    blocks.append({
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": mime,
                            "data": base64.b64encode(data).decode(),
                        },
                    })
                    readable.append((item.name, f"{mime}, sent inline"))
                elif mime in _TEXT_TYPES:
                    body = data.decode("utf-8", errors="replace")[:4000]
                    if self.redaction_enabled:
                        body = redact(body)
                    blocks.append({
                        "type": "text",
                        "text": (
                            f"=== BEGIN UNTRUSTED ATTACHMENT {item.name} ===\n"
                            f"{body}\n=== END ATTACHMENT ==="
                        ),
                    })
                    readable.append((item.name, f"{mime}, inlined as text"))

        skipped = [(s.original_filename, s.reason) for s in skipped_raw]
        return blocks, readable, skipped


def stream_answer(
    cfg: Config, system_prompt: str, prompt: str, client: Any = None
) -> Iterator[str]:
    """Token stream for the chat 'answer' action, via the API."""
    api = client or build_client(cfg.summarizer)
    try:
        with api.messages.stream(
            model=cfg.summarizer.api_model,
            max_tokens=8000,
            system=system_prompt,
            thinking={"type": "adaptive"},
            output_config={"effort": cfg.summarizer.effort},
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for chunk in stream.text_stream:
                if cancelled():
                    raise Cancelled("stopped")
                yield chunk
    except Cancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - mapped to a readable message
        raise wrap_api_errors(exc) from exc


def complete_json(cfg: Config, system_prompt: str, prompt: str, client: Any = None) -> str:
    """One non-streaming call that must return a JSON object. Used for routing."""
    api = client or build_client(cfg.summarizer)
    try:
        response = api.messages.create(
            model=cfg.summarizer.api_model,
            max_tokens=2000,
            system=system_prompt,
            thinking={"type": "adaptive"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a readable message
        raise wrap_api_errors(exc) from exc
    return next((b.text for b in response.content if getattr(b, "type", "") == "text"), "")
