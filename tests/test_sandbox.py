"""What the model is allowed to touch.

`--restricted` removes the code-running tools but KEEPS Read/Grep/Glob,
confined to the working directory. For a server whose working directory is the
project, that directory holds `.secrets/tokens/`, `.env` and `.data/` — so
"restricted" was never the same as "cannot read anything". These tests pin the
two things that make it safe: no tools unless a caller asks, and never the
process's own directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _capture_popen(monkeypatch) -> dict:
    """Record the argv and cwd stream_claude would have launched with."""
    import oncallbot.streaming as st

    seen: dict = {}

    class FakeProc:
        returncode = 0

        def __init__(self, cmd, **kw):
            seen["cmd"] = list(cmd)
            seen["cwd"] = kw.get("cwd")
            seen["cwd_files"] = (
                sorted(p.name for p in Path(str(kw["cwd"])).iterdir())
                if kw.get("cwd") else None
            )
            self.stdin = _Sink()
            self.stdout = iter(())
            self.stderr = _Sink()

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

        def kill(self):
            pass

    class _Sink:
        def write(self, _s):
            pass

        def close(self):
            pass

        def read(self):
            return ""

    monkeypatch.setattr(st.shutil, "which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr(st.subprocess, "Popen", FakeProc)
    return seen


def _flag(cmd: list[str], name: str) -> str | None:
    return cmd[cmd.index(name) + 1] if name in cmd else None


# --- narration calls get no tools at all ------------------------------------


def test_no_tools_by_default(monkeypatch):
    """The bug: the model used Read/Grep on the project and streamed
    "let me look at the working directory..." in as the finding."""
    from oncallbot.streaming import stream_claude

    seen = _capture_popen(monkeypatch)
    list(stream_claude("p", "s"))

    assert _flag(seen["cmd"], "--tools") == "", "every built-in tool disabled"
    assert "--restricted" in seen["cmd"]
    assert "--strict-mcp-config" in seen["cmd"]


def test_the_default_working_directory_is_not_the_projects(monkeypatch):
    """Defence in depth: if a tool is somehow available, it finds nothing."""
    from oncallbot.streaming import stream_claude

    seen = _capture_popen(monkeypatch)
    list(stream_claude("p", "s"))

    cwd = Path(str(seen["cwd"]))
    assert cwd != Path.cwd()
    assert Path.cwd() not in cwd.parents
    assert seen["cwd_files"] == [], "an empty directory, not the project"


def test_the_sandbox_directory_is_cleaned_up(monkeypatch):
    from oncallbot.streaming import stream_claude

    seen = _capture_popen(monkeypatch)
    list(stream_claude("p", "s"))
    assert not Path(str(seen["cwd"])).exists()


def test_a_caller_can_opt_into_read_with_its_own_directory(monkeypatch, tmp_path: Path):
    from oncallbot.streaming import stream_claude

    (tmp_path / "shot.png").write_bytes(b"x")
    seen = _capture_popen(monkeypatch)
    list(stream_claude("p", "s", cwd=tmp_path, tools="Read"))

    assert _flag(seen["cmd"], "--tools") == "Read"
    assert Path(str(seen["cwd"])) == tmp_path
    assert seen["cwd_files"] == ["shot.png"]


# --- every call site ---------------------------------------------------------


@pytest.mark.parametrize(
    "module,func",
    [
        ("oncallbot.diagnose", "_stream_reason"),
        ("oncallbot.order_qa", "_stream_text"),
        ("oncallbot.chat.actions", "_stream_text"),
    ],
)
def test_prose_call_sites_run_without_tools(monkeypatch, module, func):
    import importlib

    from oncallbot.config import Config

    mod = importlib.import_module(module)
    seen = _capture_popen(monkeypatch)
    list(getattr(mod, func)(Config(), "system", "prompt"))
    assert _flag(seen["cmd"], "--tools") == ""


@pytest.mark.parametrize(
    "module", ["oncallbot.diagnose", "oncallbot.order_qa", "oncallbot.grouping"]
)
def test_json_call_sites_run_without_tools(monkeypatch, module):
    import importlib

    from oncallbot.config import Config

    mod = importlib.import_module(module)
    seen = _capture_popen(monkeypatch)
    mod._complete_json(Config(), "system", "prompt")
    assert _flag(seen["cmd"], "--tools") == ""


def test_the_router_runs_without_tools_in_an_empty_directory(monkeypatch):
    """Routing is classification: nothing to look up, so nothing to look in."""
    import subprocess

    import oncallbot.chat.intent as it

    seen: dict = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        seen["cwd"] = kw.get("cwd")
        seen["files"] = sorted(p.name for p in Path(str(kw["cwd"])).iterdir())
        return subprocess.CompletedProcess(cmd, 0, stdout='{"action": "fetch"}', stderr="")

    monkeypatch.setattr(it.shutil, "which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr(it.subprocess, "run", fake_run)

    it.parse_intent("oncalls today", ["other"])
    assert _flag(seen["cmd"], "--tools") == ""
    assert seen["files"] == []
    assert Path(str(seen["cwd"])) != Path.cwd()


# --- the summarizer is the one caller that needs a file tool ----------------


def test_the_summarizer_gets_read_and_only_read(monkeypatch, tmp_path: Path):
    """It has attachments to open, and nothing else to look at."""

    from oncallbot.config import Config
    from oncallbot.summarizer import ClaudeCLISummarizer

    from conftest import FakeClaude

    fake = FakeClaude('{"result": "{\\"issue\\": \\"x\\"}"}').install(monkeypatch)

    from oncallbot.models import EmailMessage, EmailThread

    m = EmailMessage(id="m1", thread_id="t1", date=None, sender="a@b.com", to="",
                     cc="", subject="s", body_text="b", snippet="")
    ClaudeCLISummarizer.from_config(Config()).summarize(
        EmailThread(id="t1", subject="s", messages=[m])
    )

    assert fake.flag("--tools") == "Read", "no Grep, no Glob, no Bash"
    assert "oncallbot-att-" in Path(str(fake.last["cwd"])).name
