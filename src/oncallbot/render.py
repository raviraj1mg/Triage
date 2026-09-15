"""Output: console table, markdown digest, raw JSON."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Sequence

from rich.console import Console
from rich.table import Table

_SEVERITY_ORDER = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
_SEVERITY_STYLE = {"p0": "bold red", "p1": "red", "p2": "yellow", "p3": "dim"}


def sort_by_severity(items: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda s: (
            _SEVERITY_ORDER.get(s.get("severity", "p3"), 3),
            s.get("last_message_at", ""),
        ),
    )


def print_table(items: Sequence[dict[str, Any]], console: Console | None = None) -> None:
    console = console or Console()
    if not items:
        console.print("[dim]No summarized threads.[/dim]")
        return

    # Flexible widths: fixed ones overflow and collapse a column to nothing on
    # an 80-column terminal.
    table = Table(show_lines=True, header_style="bold", expand=True)
    table.add_column("Sev", no_wrap=True)
    table.add_column("State", no_wrap=True)
    table.add_column("Category", max_width=24, overflow="fold")
    table.add_column("Subject", ratio=2, min_width=18, overflow="fold")
    table.add_column("Issue", ratio=3, min_width=18, overflow="fold")
    table.add_column("Ids", ratio=2, min_width=14, overflow="fold")

    for s in sort_by_severity(items):
        sev = s.get("severity", "p3")
        table.add_row(
            f"[{_SEVERITY_STYLE.get(sev, '')}]{sev.upper()}[/]",
            _state_cell(s),
            s.get("category", ""),
            s.get("subject", ""),
            s.get("issue", ""),
            _id_blob(s.get("affected") or {}) or "[dim]-[/dim]",
        )

    console.print(table)


def _state_cell(s: dict[str, Any]) -> str:
    """Open, closed, or blank. Blank means nobody read the thread for it."""
    closed = s.get("closed")
    if closed is None:
        return "[dim]?[/dim]"
    return "[dim]closed[/dim]" if closed else "[yellow]open[/yellow]"


def to_markdown(items: Sequence[dict[str, Any]]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Health-record support digest",
        "",
        f"Generated {now} · {len(items)} thread(s)",
        "",
    ]
    if not items:
        lines.append("_Nothing matched._")
        return "\n".join(lines)

    for s in sort_by_severity(items):
        lines += [
            f"## {s.get('severity', 'p3').upper()} · {s.get('subject', '(no subject)')}",
            "",
            f"- **Category:** {s.get('category', '')}",
            f"- **Reporter:** {s.get('reporter', '')}",
            f"- **Last activity:** {s.get('last_message_at', '')}",
            f"- **Confidence:** {s.get('confidence', 0):.2f}",
        ]
        if s.get("closed") is not None:
            state = "closed" if s["closed"] else "open"
            by = f" by {s['closed_by']}" if s.get("closed") and s.get("closed_by") else ""
            lines.append(
                f"- **Email:** {state}{by} "
                f"(confidence {float(s.get('closure_confidence') or 0):.2f})"
            )
        if s.get("suggested_owner"):
            lines.append(f"- **Suggested owner:** {s['suggested_owner']}")
        if s.get("permalink"):
            lines.append(f"- **Thread:** {s['permalink']}")

        ids = _id_blob(s.get("affected") or {})
        if ids:
            lines.append(f"- **Identifiers:** {ids}")

        lines += ["", f"**Issue.** {s.get('issue', '')}", "", s.get("summary", ""), ""]

        if s.get("asks"):
            lines.append("**Sender is asking for**")
            lines += [f"- {a}" for a in s["asks"]]
            lines.append("")
        if s.get("missing_info"):
            lines.append("**Still needed to act**")
            lines += [f"- {m}" for m in s["missing_info"]]
            lines.append("")

    return "\n".join(lines)


def to_json(items: Sequence[dict[str, Any]]) -> str:
    return json.dumps(sort_by_severity(items), indent=2, ensure_ascii=False)


def _id_blob(affected: dict[str, Any]) -> str:
    parts = []
    for key, values in affected.items():
        if values:
            parts.append(f"{key.removesuffix('_ids').removesuffix('s')}: {', '.join(values)}")
    return "; ".join(parts)
