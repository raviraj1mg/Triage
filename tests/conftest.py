"""Shared fakes.

The summarizer drives `claude` with Popen + communicate() on a helper thread,
so it can be killed when a turn is stopped. Tests therefore fake Popen rather
than subprocess.run -- and a test that fakes the wrong one launches the real
binary, which is slow and non-deterministic rather than failing outright.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class FakeClaude:
    """Stands in for one `claude` subprocess, recording how it was launched."""

    def __init__(self, stdout: str = '{"issue": "x"}', returncode: int = 0) -> None:
        self.stdout_text = stdout
        self.returncode = returncode
        self.calls: list[dict[str, Any]] = []

    @property
    def last(self) -> dict[str, Any]:
        assert self.calls, "the subprocess was never launched"
        return self.calls[-1]

    def flag(self, name: str) -> str | None:
        cmd = self.last["cmd"]
        return cmd[cmd.index(name) + 1] if name in cmd else None

    def popen(self, cmd, **kw):  # noqa: ANN001, ANN003 - a stdlib signature
        cwd = kw.get("cwd")
        self.calls.append({
            "cmd": list(cmd),
            "cwd": cwd,
            "files": sorted(p.name for p in Path(str(cwd)).iterdir()) if cwd else None,
        })
        outer = self

        class Proc:
            returncode = outer.returncode

            def communicate(self, input=None, timeout=None):  # noqa: A002
                outer.calls[-1]["prompt"] = input
                return (outer.stdout_text, "")

            def poll(self):
                return outer.returncode

            def kill(self):
                pass

        return Proc()

    def install(self, monkeypatch) -> FakeClaude:
        monkeypatch.setattr("oncallbot.summarizer.shutil.which", lambda _b: "/usr/bin/claude")
        monkeypatch.setattr("oncallbot.summarizer.subprocess.Popen", self.popen)
        return self


def ui(client) -> _Collapsed:
    """The served page, for assertions about wiring rather than formatting.

    index.html gets reformatted (by an editor, or by hand), and a test that
    fails on a re-indent is noise: it says nothing about whether the feature
    still works. Whitespace is collapsed on both sides of every comparison, so
    these tests pin the wiring and ignore the layout.
    """
    return _Collapsed(client.get("/").text)


def _flat(text: str) -> str:
    """All whitespace removed.

    Not merely collapsed: a formatter turns `a==='b'` into `a === 'b'`, and
    collapsing runs of spaces does not make "no space" equal "one space".
    These needles are code and display text, so removing whitespace on both
    sides is the comparison that survives reformatting.
    """
    import re

    return re.sub(r"\s+", "", str(text))


class _Collapsed(str):
    """A string whose membership and search ignore whitespace differences."""

    def __contains__(self, needle: object) -> bool:  # type: ignore[override]
        return _flat(needle) in _flat(self)  # type: ignore[arg-type]

    def index(self, needle, *args) -> int:  # type: ignore[override]
        return _flat(self).index(_flat(needle), *args)

    def count(self, needle, *args) -> int:  # type: ignore[override]
        return _flat(self).count(_flat(needle), *args)
