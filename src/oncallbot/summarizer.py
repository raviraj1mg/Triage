"""Summarizer backends.

Only claude_cli exists today: it shells out to the `claude` binary and reuses
the operator's existing Claude Code auth, so there is no API key to provision.
The Protocol is here so an SDK-backed backend can drop in later without
touching callers.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

from .attachments import download
from .config import AttachmentConfig, Config
from .gmail_client import GmailClient
from .models import EmailThread, IssueSummary
from .prompts.live import prompt_for
from .prompts import SYSTEM_PROMPT, build_user_prompt
from .redact import redact


class SummarizerError(RuntimeError):
    pass


class Summarizer(Protocol):
    def summarize(self, thread: EmailThread) -> IssueSummary: ...


class StreamingSummarizer(Protocol):
    """A backend that can hand over the JSON while the model is still writing.

    Optional: `summarize_stream` falls back to one blocking call for a backend
    that does not implement it, and says so rather than pretending to stream.
    """

    def raw_stream(self, thread: EmailThread) -> Iterator[str]: ...
    @property
    def model_id(self) -> str: ...


@dataclass
class ClaudeCLISummarizer:
    model: str = "sonnet"
    timeout_seconds: int = 180
    max_body_chars: int = 12000
    categories: tuple[str, ...] = ("other",)
    redaction_enabled: bool = True
    binary: str = "claude"
    attachments: AttachmentConfig = field(default_factory=AttachmentConfig)
    gmail: GmailClient | None = None

    @classmethod
    def from_config(cls, cfg: Config, gmail: GmailClient | None = None) -> ClaudeCLISummarizer:
        return cls(
            model=cfg.summarizer.model,
            timeout_seconds=cfg.summarizer.timeout_seconds,
            max_body_chars=cfg.summarizer.max_body_chars,
            categories=tuple(cfg.categories),
            redaction_enabled=cfg.redaction_enabled,
            attachments=cfg.attachments,
            gmail=gmail,
        )

    def summarize(self, thread: EmailThread) -> IssueSummary:
        if shutil.which(self.binary) is None:
            raise SummarizerError(
                f"`{self.binary}` not found on PATH. Install Claude Code or set "
                "summarizer.backend to another engine."
            )

        prepared = _redacted_copy(thread) if self.redaction_enabled else thread

        # The model runs with its working directory set to this temp dir, so
        # --restricted confines its file tools to exactly the attachments we
        # put there -- not the project, and not .secrets/.
        with tempfile.TemporaryDirectory(prefix="oncallbot-att-") as tmp:
            workdir = Path(tmp)
            readable, skipped = self._stage_attachments(thread, workdir)
            prompt = build_user_prompt(
                prepared,
                list(self.categories),
                self.max_body_chars,
                readable_attachments=readable,
                skipped_attachments=skipped,
            )
            return self._invoke(prompt, thread, workdir)

    @property
    def model_id(self) -> str:
        return self.model

    def raw_stream(self, thread: EmailThread) -> Iterator[str]:
        """The same call as summarize(), yielding the JSON as it is written."""
        from .streaming import StreamError, stream_claude

        prepared = _redacted_copy(thread) if self.redaction_enabled else thread
        # Same sandbox as the blocking path: cwd is the staged-attachment dir,
        # so --restricted cannot reach the project or .secrets/. The directory
        # has to outlive the generator, so it is cleaned up in `finally`.
        tmp = tempfile.TemporaryDirectory(prefix="oncallbot-att-")
        try:
            workdir = Path(tmp.name)
            readable, skipped = self._stage_attachments(thread, workdir)
            prompt = build_user_prompt(
                prepared,
                list(self.categories),
                self.max_body_chars,
                readable_attachments=readable,
                skipped_attachments=skipped,
            )
            try:
                yield from stream_claude(
                    prompt,
                    prompt_for("summarize", SYSTEM_PROMPT),
                    model=self.model,
                    timeout_seconds=self.timeout_seconds,
                    binary=self.binary,
                    cwd=workdir,
                    tools="Read",  # the staged attachments, nothing else
                )
            except StreamError as exc:
                raise SummarizerError(str(exc)) from exc
        finally:
            tmp.cleanup()

    def _stage_attachments(
        self, thread: EmailThread, workdir: Path
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        present = [a for m in thread.messages for a in m.attachments]
        if not present:
            return [], []
        if not (self.attachments.enabled and self.gmail is not None):
            reason = (
                "attachment downloads are disabled"
                if not self.attachments.enabled
                else "no Gmail client available to fetch it"
            )
            return [], [(a.filename, reason) for a in present]

        saved, skipped = download(self.gmail, thread, self.attachments, workdir)
        return (
            [(s.name, f"{s.mime_type}, {s.size_bytes // 1024}KB") for s in saved],
            [(s.original_filename, s.reason) for s in skipped],
        )

    def _invoke(self, prompt: str, thread: EmailThread, workdir: Path) -> IssueSummary:
        cmd = [
            self.binary,
            "-p",
            "--output-format", "json",
            "--model", self.model,
            # The summarizer must never run code or reach the network, and must
            # not inherit the operator's project settings, agents or MCP servers.
            "--restricted",
            "--strict-mcp-config",
            # Read only, for the attachments staged in `workdir`. --restricted
            # keeps the file tools and confines them to the cwd; naming them
            # explicitly is what stops Grep and Glob from being available at
            # all, so the model cannot go hunting through the directory.
            "--tools", "Read",
            "--system-prompt", prompt_for("summarize", SYSTEM_PROMPT),
        ]

        # Popen rather than run(): a summarize takes tens of seconds, and when
        # the reader has gone away it should end rather than finish into a
        # void. run() cannot be interrupted, so the process would outlive the
        # request -- measured, before this: killing the client left `claude`
        # running to completion.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=workdir,
        )
        stdout, stderr = _communicate_cancellably(
            proc, prompt, self.timeout_seconds, thread.id
        )

        if proc.returncode != 0:
            raise SummarizerError(
                f"claude exited {proc.returncode} on thread {thread.id}: "
                f"{(stderr or stdout or '').strip()[:500]}"
            )

        data = _parse_json_object(_unwrap_envelope(stdout))
        return IssueSummary.from_model_output(data, thread, self.model)


# Long prose fields, in the order the schema asks for them. The UI shows these
# arriving; everything else lands when the object closes.
STREAMED_FIELDS = ("summary", "issue")


def summarize_stream(
    summarizer: Any, thread: EmailThread
) -> Iterator[tuple[str, Any]]:
    """Summarize one thread, reporting fields as the model writes them.

    Events: ("status", str), ("delta", {"name","text"}) a fragment of a field,
    ("field", {"name","value"}) a field that just closed, and finally
    ("summary", IssueSummary). The final summary is parsed from the complete
    text, not assembled from the fragments -- the fragments are for the reader,
    the parse is what gets stored.
    """
    from .json_stream import JsonFieldStream

    raw = getattr(summarizer, "raw_stream", None)
    if raw is None:
        yield (
            "status",
            "This backend cannot stream a summary; waiting for the whole thing.",
        )
        yield ("summary", summarizer.summarize(thread))
        return

    scanner = JsonFieldStream()
    for chunk in raw(thread):
        for kind, key, value in scanner.feed(chunk):
            if kind == "delta":
                yield ("delta", {"name": key, "text": value})
            else:
                yield ("field", {"name": key, "value": value})

    # stream_claude yields only assistant text, so there is no result envelope
    # to unwrap here -- but the model may still fence the object.
    data = _parse_json_object(scanner.text)
    yield (
        "summary",
        IssueSummary.from_model_output(
            data, thread, getattr(summarizer, "model_id", "")
        ),
    )


def _communicate_cancellably(
    proc: Any, prompt: str, timeout_seconds: int, thread_id: str
) -> tuple[str, str]:
    """Feed the prompt and wait, checking whether this turn was stopped.

    communicate() runs on a helper thread because it is the only way to write
    the prompt and drain both pipes without risking a full-buffer deadlock --
    and it cannot be given a timeout more than once, since the input may only
    be sent one time. Killing the process is what makes it return.
    """
    import threading

    from .streaming import Cancelled, cancelled

    box: dict[str, tuple[str, str]] = {}
    error: dict[str, BaseException] = {}

    def pump() -> None:
        try:
            box["out"] = proc.communicate(input=prompt)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            error["exc"] = exc

    worker = threading.Thread(target=pump, daemon=True)
    worker.start()

    waited = 0.0
    tick = 0.25
    while worker.is_alive():
        worker.join(tick)
        if not worker.is_alive():
            break
        waited += tick
        if cancelled():
            proc.kill()
            worker.join(timeout=5)
            raise Cancelled("stopped")
        if waited >= timeout_seconds:
            proc.kill()
            worker.join(timeout=5)
            raise SummarizerError(
                f"claude timed out after {timeout_seconds}s on thread {thread_id}"
            )

    if "exc" in error:
        raise error["exc"]
    out, err = box.get("out", ("", ""))
    return out or "", err or ""


def build_summarizer(cfg: Config, gmail: GmailClient | None = None) -> Summarizer:
    if cfg.summarizer.backend == "claude_cli":
        return ClaudeCLISummarizer.from_config(cfg, gmail)
    if cfg.summarizer.backend == "anthropic_api":
        # Imported lazily so the CLI backend never needs the SDK installed.
        from .anthropic_backend import AnthropicSummarizer

        return AnthropicSummarizer.from_config(cfg, gmail)
    if cfg.summarizer.backend == "local":
        from .local_backend import LocalSummarizer

        return LocalSummarizer.from_config(cfg, gmail)
    raise SummarizerError(
        f"Unknown summarizer backend {cfg.summarizer.backend!r}. "
        "Supported: claude_cli, anthropic_api, local."
    )


def _redacted_copy(thread: EmailThread) -> EmailThread:
    from copy import deepcopy

    clone = deepcopy(thread)
    for msg in clone.messages:
        msg.body_text = redact(msg.body_text)
        msg.snippet = redact(msg.snippet)
        msg.subject = redact(msg.subject)
    clone.subject = redact(clone.subject)
    return clone


def _unwrap_envelope(stdout: str) -> str:
    """`--output-format json` wraps the answer in a result envelope."""
    stdout = stdout.strip()
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(envelope, dict) and "result" in envelope:
        if envelope.get("is_error"):
            raise SummarizerError(f"claude reported an error: {envelope.get('result')}")
        return str(envelope["result"])
    return stdout


def _parse_json_object(text: str) -> dict[str, Any]:
    """Pull the first balanced JSON object out of the model's answer."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise SummarizerError(f"No JSON object in model output: {text[:300]!r}")

    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError as exc:
                    raise SummarizerError(f"Malformed JSON from model: {exc}") from exc

    raise SummarizerError(f"Unterminated JSON object in model output: {text[:300]!r}")
