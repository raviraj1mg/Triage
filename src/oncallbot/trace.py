"""What the turn actually did, as it does it.

The UI shows this in a "Thinking" accordion so a reader can see why an answer
came out the way it did: which prompt produced it, which reads went out, and
what came back.

This is a TRACE, not the model's private reasoning. We stream content deltas
and nothing else -- there is no thinking channel being hidden from you here,
and the local models do not emit one at all. Calling it "thinking" in the UI
is a name for the panel, and the entries are the honest contents: prompts,
tool calls, and the steps around them.

Entries are pushed through a sink set by whichever worker is running the turn,
because a worker thread does not inherit the request's context -- the same
reason the cancel token and the prompt overrides are set there.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import Any, Callable

# Set per turn, on the thread doing the work. None means nobody is listening,
# which is the case for the CLI and for every test that has not opted in.
SINK: ContextVar[Callable[[dict[str, Any]], None] | None] = ContextVar(
    "oncallbot_trace", default=None
)

# A parameter value can be a whole API payload; the panel is a summary.
MAX_VALUE = 200


def set_sink(fn: Callable[[dict[str, Any]], None] | None) -> None:
    SINK.set(fn)


def note(kind: str, label: str, **fields: Any) -> None:
    """Record one step. Silent when nothing is listening."""
    sink = SINK.get()
    if sink is None:
        return
    entry = {"kind": kind, "label": label, "at": round(time.time(), 3)}
    for key, value in fields.items():
        if value in (None, "", [], {}):
            continue
        entry[key] = _short(value)
    try:
        sink(entry)
    except Exception:  # noqa: BLE001 - a trace must never break the answer
        pass


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_VALUE else value[:MAX_VALUE] + "…"
    if isinstance(value, dict):
        return {k: _short(v) for k, v in list(value.items())[:12]}
    if isinstance(value, (list, tuple)):
        return [_short(v) for v in list(value)[:12]]
    return value
