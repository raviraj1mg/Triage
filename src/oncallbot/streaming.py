"""Incremental text from the `claude` CLI.

`--output-format stream-json --include-partial-messages` emits one JSON object
per line; the text we want lives in `content_block_delta` events. Anything we
do not recognise is skipped, so a new event type in a future CLI release is
inert rather than fatal.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import threading
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator


class StreamError(RuntimeError):
    pass


# Set by the request worker for the duration of one turn. A model call can run
# for minutes; when the reader goes away -- the user pressed Stop, or closed
# the tab -- there is nothing left to send the output to, so the work should
# end rather than finish into a void. Set per thread by contextvars, so
# concurrent turns cannot cancel each other.
CANCEL: ContextVar[threading.Event | None] = ContextVar("oncallbot_cancel", default=None)


def cancelled() -> bool:
    """True once this turn's reader has gone away."""
    ev = CANCEL.get()
    return ev is not None and ev.is_set()


class Cancelled(RuntimeError):
    """Raised inside a turn that has been stopped. Not an error to report."""


# What the CLI said, mapped to the one thing the reader can do about it.
# A teammate who had followed every README step saw this instead:
#   Could not understand that: {"duration_api_ms":0,"stop_reason":"stop_sequ…
# -- the result envelope, truncated mid-key. The CLI writes that envelope to
# stdout even when it fails, and stderr was empty, so the raw telemetry was
# all the caller had to show.
_CLI_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("not logged in", "please log in", "please run /login", "/login",
      "authentication_error", "invalid api key", "oauth", "unauthorized",
      "no credentials", "not authenticated"),
     "Claude Code is installed but not signed in. Run `claude` once in a "
     "terminal, finish the login, then try again."),
    (("usage limit", "rate limit", "rate_limit", "quota", "too many requests"),
     "Claude Code has hit its usage limit. Wait for the reset, or set "
     "summarizer.backend to anthropic_api or local in config.yaml."),
    (("credit balance", "billing", "insufficient funds"),
     "The Claude account has no credit left. Top it up, or set "
     "summarizer.backend to anthropic_api or local in config.yaml."),
    (("unknown option", "unrecognized option", "unknown argument",
      "unknown flag", "invalid option"),
     "This Claude Code is too old for the options oncallbot passes. Update it "
     "(`npm install -g @anthropic-ai/claude-code`) and try again."),
)


def _human_part(stdout: str) -> str:
    """The readable sentence out of the CLI's JSON, not the telemetry."""
    text = (stdout or "").strip()
    if not text:
        return ""
    data: Any = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # stream-json is one object per line; the last complete one carries
        # the outcome.
        for line in reversed(text.splitlines()):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(data, dict):
        return text[:200]
    for key in ("result", "error", "message", "detail"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            inner = value.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    subtype = data.get("subtype")
    if isinstance(subtype, str) and subtype and subtype != "success":
        return f"the CLI reported {subtype!r}"
    return ""


def explain_cli_failure(stdout: str, stderr: str, code: int) -> str:
    """One actionable sentence from a failed `claude` run.

    Never the raw envelope: token counts, a session id and a duration tell the
    reader nothing they can act on, and truncating that to fit only makes it
    look corrupt.
    """
    said = _human_part(stdout) or (stderr or "").strip()
    haystack = f"{said}\n{stderr or ''}".lower()
    for markers, advice in _CLI_HINTS:
        if any(m in haystack for m in markers):
            return f"{advice} (claude said: {said[:160]})" if said else advice
    if said:
        return f"claude exited {code}: {said[:200]}"
    return (
        f"claude exited {code} without saying why. Run `claude -p hello` in a "
        "terminal to see what it reports — most often it needs signing in."
    )


def probe_cli(binary: str = "claude", timeout_seconds: int = 60) -> tuple[bool, str]:
    """Does `claude` actually answer, or is it only installed?

    `which` was the whole check, and it passes for a Claude Code that has
    never been signed in -- so setup looked healthy and every question failed
    afterwards with the CLI's telemetry. One trivial round-trip, with the same
    options the real calls use, is what tells the two apart.
    """
    if shutil.which(binary) is None:
        return False, (
            f"`{binary}` is not on PATH. Install Claude Code, or set "
            "summarizer.backend to anthropic_api or local in config.yaml."
        )
    cmd = [
        binary, "-p",
        "--output-format", "json",
        "--restricted",
        "--strict-mcp-config",
        "--tools", "",
        "--system-prompt", "Reply with the single word: ok",
    ]
    try:
        with tempfile.TemporaryDirectory(prefix="oncallbot-probe-") as empty:
            proc = subprocess.run(
                cmd, input="Say ok.", capture_output=True, text=True,
                timeout=timeout_seconds, cwd=empty,
            )
    except subprocess.TimeoutExpired:
        return False, (
            f"`{binary}` did not answer within {timeout_seconds}s. Run "
            f"`{binary} -p hello` in a terminal to see what it is waiting for."
        )
    except OSError as exc:
        return False, f"Could not run `{binary}`: {exc}"

    if proc.returncode != 0:
        return False, explain_cli_failure(proc.stdout, proc.stderr, proc.returncode)
    answer = _human_part(proc.stdout)
    if not answer:
        return False, explain_cli_failure(proc.stdout, proc.stderr, proc.returncode)
    return True, f"answered: {answer[:60]}"


def stream_claude(
    prompt: str,
    system_prompt: str,
    *,
    model: str = "sonnet",
    timeout_seconds: int = 180,
    binary: str = "claude",
    cwd: str | Path | None = None,
    tools: str = "",
) -> Iterator[str]:
    """Yield text chunks as the model produces them.

    `tools` defaults to "" -- `--tools ""` disables every built-in tool. Every
    caller here hands the model all the data it needs in the prompt, so a tool
    call is never the answer: it is the model wandering off into whatever
    directory the process happens to be in, narrating "let me look at..." into
    what the UI renders as the finding. `--restricted` alone does NOT prevent
    that: it removes the code-running tools but keeps Read/Grep/Glob, confined
    to the working directory -- which for the server is the project, including
    `.secrets/` and `.env`. Pass `tools="Read"` only alongside a `cwd` holding
    exactly the files the model may see.

    `cwd` defaults to a fresh empty directory rather than the process's own, so
    a tool that is somehow available still finds nothing.
    """
    if shutil.which(binary) is None:
        raise StreamError(f"`{binary}` not found on PATH.")

    cmd = [
        binary,
        "-p",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",  # required alongside stream-json
        "--model", model,
        "--restricted",
        "--strict-mcp-config",
        "--tools", tools,
        "--system-prompt", system_prompt,
    ]

    sandbox: tempfile.TemporaryDirectory[str] | None = None
    if cwd is None:
        sandbox = tempfile.TemporaryDirectory(prefix="oncallbot-notools-")
        cwd = sandbox.name

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        cwd=cwd,
    )
    assert proc.stdin and proc.stdout

    try:
        proc.stdin.write(prompt)
        proc.stdin.close()

        saw_text = False
        for line in proc.stdout:
            # Checked per line rather than at the end: the point of stopping is
            # to stop paying for the rest of the answer.
            if cancelled():
                proc.kill()
                raise Cancelled("stopped")
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            if event.get("type") == "stream_event":
                inner = event.get("event", {})
                if inner.get("type") == "content_block_delta":
                    text = (inner.get("delta") or {}).get("text")
                    if text:
                        saw_text = True
                        yield text
            elif event.get("type") == "result" and event.get("is_error"):
                raise StreamError(str(event.get("result", "claude reported an error"))[:300])

        code = proc.wait(timeout=timeout_seconds)
        if code != 0 and not saw_text:
            stderr = (proc.stderr.read() if proc.stderr else "").strip()
            raise StreamError(explain_cli_failure("", stderr, code))
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        raise StreamError(f"claude timed out after {timeout_seconds}s") from exc
    finally:
        if proc.poll() is None:
            proc.kill()
        if sandbox is not None:
            sandbox.cleanup()
