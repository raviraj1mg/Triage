"""Counts computed in code, for the model to narrate rather than derive.

A follow-up like "how many of these are closed" is arithmetic over rows that
are already on screen. Leaving it to the model makes the answer depend on
which model is answering: asked to count four rows, `gemma3:latest` read
`closed: false` as "not known" and reported two settled-open threads as
undiagnosed. The same prompt on `claude_cli` gets it right, which is worse
than both being wrong -- two people running the same tool see different
numbers for the same screen.

None of this is a judgement, so none of it belongs to the model. It is counted
here and handed over as fact, the way `grouping.py` already does for category
counts.
"""

from __future__ import annotations

from collections import Counter
from typing import Any


def dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per thread. A thread shown in two turns is still one thread.

    The later row wins: it may carry a `closed` value the earlier one did not,
    because the reader ran Diagnose in between.
    """
    out: dict[str, dict[str, Any]] = {}
    loose: list[dict[str, Any]] = []
    for row in rows:
        tid = str(row.get("thread_id") or "").strip()
        if tid:
            out[tid] = row
        else:
            loose.append(row)
    return [*out.values(), *loose]


def _ids(rows: list[dict[str, Any]], limit: int = 12) -> str:
    names = [f"`{r.get('thread_id')}`" for r in rows if r.get("thread_id")][:limit]
    extra = len(rows) - len(names)
    return ", ".join(names) + (f" and {extra} more" if extra > 0 else "")


def tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Closed / open / unknown, plus severity and category counts."""
    rows = dedupe(rows)
    # `closed` is present only for a thread that was actually diagnosed, so
    # absent and False mean different things: "nobody has checked" versus
    # "checked, and it is still open".
    closed = [r for r in rows if r.get("closed") is True]
    opened = [r for r in rows if r.get("closed") is False]
    unknown = [r for r in rows if r.get("closed") is None]
    return {
        "rows": len(rows),
        "closed": closed,
        "open": opened,
        "unknown": unknown,
        "severity": Counter(
            str(r.get("severity") or "").lower() for r in rows if r.get("severity")
        ),
        "category": Counter(
            str(r.get("category") or "") for r in rows if r.get("category")
        ),
    }


def facts_block(rows: list[dict[str, Any]]) -> str:
    """The counted facts, as lines for the prompt. Empty when there is nothing."""
    if not rows:
        return ""
    t = tally(rows)
    lines = [
        "=== COUNTED IN CODE (authoritative; use verbatim, never recount) ===",
        f"- threads on screen: {t['rows']}",
        f"- closed: {len(t['closed'])}" + (f" — {_ids(t['closed'])}" if t["closed"] else ""),
        # Spelled out because the difference was glossed wrongly: an open
        # thread HAS been looked at, an unknown one has not, and calling the
        # first group undiagnosed inverts what the reader should do next.
        f"- open — these WERE diagnosed and came back open: {len(t['open'])}"
        + (f" — {_ids(t['open'])}" if t["open"] else ""),
        f"- unknown — these have NEVER been diagnosed: {len(t['unknown'])}"
        + (f" — {_ids(t['unknown'])}" if t["unknown"] else ""),
    ]
    if t["severity"]:
        lines.append(
            "- by severity: "
            + ", ".join(f"{k} {v}" for k, v in sorted(t["severity"].items()))
        )
    if t["category"]:
        lines.append(
            "- by category: "
            + ", ".join(f"{k} {v}" for k, v in sorted(t["category"].items()))
        )
    lines.append("=== END COUNTED IN CODE ===")
    return "\n".join(lines)


# --- keeping the scaffolding out of the answer -----------------------------
#
# Handing the counts over fixed the arithmetic and immediately caused a new
# problem: `gemma3:latest` copied what it was given straight into its reply.
# One answer pasted the whole rows JSON under "rows displayed (4):", another
# reproduced the counted block verbatim as its "Breakdown". Both are internal
# scaffolding, and neither is something a reader should ever see.
#
# Telling it not to helps and does not hold, the same way the routing prompt's
# example values were copied until they were removed. So the rule is enforced
# here: a line the model took verbatim out of its own prompt is dropped.

_STRUCTURAL_PREFIXES = ("===", "[{", "[ {", "{\"", "rows displayed")


def _norm(line: str) -> str:
    """A line stripped to its content, so a re-marked copy still matches.

    The observed echo re-listed "- threads on screen: 4" as "threads on
    screen: 4" -- copied, but not byte-for-byte, which a verbatim test would
    have let through.
    """
    s = line.strip().lstrip("-*\u2022 \t").strip()
    return " ".join(s.split()).lower()


def _is_echo(line: str, prompt_lines: set[str]) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith(_STRUCTURAL_PREFIXES):
        return True
    # Long enough that matching a line it was given is copying rather than
    # coincidence: "Two threads are P1." is the model's own sentence, and it
    # appears nowhere in the prompt.
    return len(s) > 12 and _norm(s) in prompt_lines


def _is_label(line: str) -> bool:
    """A bare heading like "Breakdown:" -- useless once its body is dropped."""
    s = line.strip()
    return bool(s) and len(s) < 40 and s.endswith(":")


def strip_echo(chunks, prompt: str):
    """Drop lines copied out of the prompt, as they stream.

    Line-buffered rather than post-hoc: stripping at the end would show the
    reader a JSON dump that then vanished, which is the reflow this UI works
    hard to avoid everywhere else.

    A label is held back by one line. Dropping the body under "Breakdown:"
    would otherwise leave the word hanging with nothing beneath it.
    """
    prompt_lines = {_norm(line) for line in prompt.splitlines() if line.strip()}
    held = ""
    pending: list[str] = []

    def take(line: str):
        """Emit a surviving line, flushing any label waiting on it."""
        nonlocal pending
        out = "".join(pending) + line
        pending = []
        return out

    for chunk in chunks:
        held += chunk
        while "\n" in held:
            line, held = held.split("\n", 1)
            if _is_echo(line, prompt_lines):
                continue
            if _is_label(line):
                pending.append(line + "\n")
                continue
            if line.strip() or pending:
                yield take(line + "\n")
            else:
                yield line + "\n"
    if held and not _is_echo(held, prompt_lines):
        yield take(held)
    # Anything still pending was a label with nothing under it.


def plain_answer(rows: list[dict[str, Any]]) -> str:
    """A last-resort sentence, when filtering leaves nothing behind.

    Only reached if the model produced scaffolding and nothing else. Saying
    the counted numbers plainly beats an empty bubble.
    """
    t = tally(rows)
    parts = [f"{t['rows']} thread(s) on screen: {len(t['closed'])} closed, "
             f"{len(t['open'])} open"]
    if t["unknown"]:
        parts.append(
            f", {len(t['unknown'])} not diagnosed yet ({_ids(t['unknown'])}) — "
            "Diagnose on those cards would settle it"
        )
    return "".join(parts) + "."
