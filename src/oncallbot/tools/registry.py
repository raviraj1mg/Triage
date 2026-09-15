"""Turn tools/order_info.md into callable tool specs.

The markdown is the source of truth on purpose: adding or re-documenting an
endpoint there makes it available without touching code, and the prose under
each heading becomes the description the model routes on.

Only reads are ever exposed. Classification is by HTTP method plus an explicit
allowlist for the one read that happens to be a POST (`presigned-url`, which
signs a URL rather than changing anything). Everything else in that file --
merge, demerge, move-order, update-demographics -- is a write and is parsed,
labelled, and withheld. A future resolve phase will reach those through their
own allowlist and confirmation flow, never through model tool selection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from pathlib import Path

DEFAULT_DOC = Path(__file__).parent / "order_info.md"

# POST endpoints that do not mutate anything. Nothing is added here without
# reading the handler.
_POST_READS = frozenset({"/presigned-url"})

_HEADING = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE)
_CURL_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_URL = re.compile(r"curl\s+--location\s+(?:--request\s+(\w+)\s+)?'([^']+)'")
_PLACEHOLDER = re.compile(r"<([a-z_]+)>")


@dataclass
class ToolSpec:
    """One documented endpoint."""

    number: int
    title: str
    description: str
    method: str
    url_template: str
    path: str
    path_params: list[str] = field(default_factory=list)
    query_params: list[str] = field(default_factory=list)
    body_params: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        """A stable snake_case handle the model can name."""
        slug = re.sub(r"[^a-z0-9]+", "_", self.title.lower()).strip("_")
        return slug

    @property
    def is_read(self) -> bool:
        return self.method == "GET" or self.path in _POST_READS

    @property
    def required_params(self) -> list[str]:
        seen: list[str] = []
        for p in [*self.path_params, *self.query_params]:
            if p not in seen:
                seen.append(p)
        return seen

    def describe(self) -> str:
        """One compact block for the routing prompt."""
        parts = [f"{self.name}: {self.description}"]
        if self.required_params:
            parts.append(f"  needs: {', '.join(self.required_params)}")
        optional = [p for p in self.query_params if p not in self.path_params]
        if optional:
            parts.append(f"  optional: {', '.join(optional)}")
        return "\n".join(parts)


# --- dependency chains -----------------------------------------------------
#
# order_info.md describes these in prose -- tool 1 is "the usual first call",
# tool 2 is what yields "the booking that needs fixing" -- and the ids make it
# unavoidable: bookings+parameters is addressed by patient_id and booking_id,
# and neither exists until the first two calls have returned them. Encoded here
# so it is enforced rather than left to whichever caller remembered.

PREREQUISITES: dict[str, tuple[str, ...]] = {
    "get_diagnostic_bookings_parameters": (
        "get_user_details_for_an_order",   # order_group_id -> user_id, patients
        "fetch_all_orders_of_a_user",      # user_id -> booking_id + patient_id
    ),
    "get_json_report_url_of_a_booking": (
        "get_user_details_for_an_order",
        "fetch_all_orders_of_a_user",      # the booking_id comes from here
    ),
}

# Beyond the path parameters. A call missing these is not an error the API will
# explain: it answers for the whole order, or for nothing, and the reply reads
# as if the data were absent.
REQUIRED_PARAMS: dict[str, tuple[str, ...]] = {
    "get_diagnostic_bookings_parameters": (
        "order_group_id", "patient_id", "booking_id",
    ),
    "get_json_report_url_of_a_booking": ("order_group_id", "booking_id"),
    # It signs a URL, so it needs one. Nothing in the path or query says so,
    # which let a planned call go out empty and fail in the client instead.
    "get_presigned_url_for_private_content": ("url",),
}


def required_params(name: str, spec: ToolSpec | None = None) -> tuple[str, ...]:
    """Every parameter this tool must have, path parameters included."""
    declared = REQUIRED_PARAMS.get(name, ())
    if declared:
        return declared
    return tuple(spec.path_params) if spec is not None else ()


def missing_params(name: str, params: dict[str, Any], spec: ToolSpec | None = None) -> list[str]:
    return [
        p for p in required_params(name, spec)
        if not str(params.get(p) or "").strip()
    ]


def missing_prerequisites(name: str, called: Iterable[str]) -> list[str]:
    """Which earlier calls this one needs and has not had."""
    done = set(called)
    return [t for t in PREREQUISITES.get(name, ()) if t not in done]


def load_tools(doc: Path | None = None) -> list[ToolSpec]:
    """Parse every documented endpoint, reads and writes alike."""
    doc = doc or DEFAULT_DOC
    if not doc.exists():
        raise FileNotFoundError(
            f"{doc} not found — the API reference the tools are built from."
        )
    text = doc.read_text()

    headings = list(_HEADING.finditer(text))
    specs: list[ToolSpec] = []

    for i, h in enumerate(headings):
        start = h.end()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section = text[start:end]

        curl = _CURL_BLOCK.search(section)
        if not curl:
            continue
        block = curl.group(1)
        m = _URL.search(block)
        if not m:
            continue

        explicit_method, url = m.group(1), m.group(2)
        has_body = "--data" in block
        method = (explicit_method or ("POST" if has_body else "GET")).upper()

        path_part, _, query_part = url.partition("?")
        marker = "/health-record/admin"
        path = path_part[path_part.index(marker) + len(marker):] if marker in path_part else path_part

        specs.append(
            ToolSpec(
                number=int(h.group(1)),
                title=h.group(2),
                description=_description(section),
                method=method,
                url_template=url,
                path=_strip_placeholders(path),
                path_params=_PLACEHOLDER.findall(path_part),
                query_params=_PLACEHOLDER.findall(query_part),
                body_params=sorted(set(_PLACEHOLDER.findall(block[block.find("--data"):])))
                if has_body
                else [],
            )
        )

    return specs


def read_only_tools(doc: Path | None = None) -> list[ToolSpec]:
    """The only set a model is ever offered."""
    return [t for t in load_tools(doc) if t.is_read]


def withheld_tools(doc: Path | None = None) -> list[ToolSpec]:
    """Documented writes, so the UI can say what exists but is not available."""
    return [t for t in load_tools(doc) if not t.is_read]


def _description(section: str) -> str:
    """The bolded lead paragraph under a heading, flattened."""
    bold = re.search(r"\*\*(.+?)\*\*", section, re.DOTALL)
    if bold:
        return " ".join(bold.group(1).split())
    for line in section.strip().splitlines():
        line = line.strip()
        if line and not line.startswith(("```", "|", ">", "#", "-")):
            return " ".join(line.split())
    return ""


def _strip_placeholders(path: str) -> str:
    """"/patient/<patient_id>/versions" -> "/patient/{}/versions" for matching."""
    return _PLACEHOLDER.sub("{}", path)
