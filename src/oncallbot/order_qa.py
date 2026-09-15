"""Answer questions about an order using the read-only admin APIs.

The two entry calls are always made in the same order, because that is the
dependency chain rather than a choice: `users/order/{ogid}` yields the user and
their patients, and `user/{uid}/orders` yields the bookings with their
`booking_id` and `patient_id`. Everything else needs ids from those two.

Only the follow-ups are the model's decision, and only from the read set
parsed out of tools/order_info.md. It gets one planning round, capped, so a
question costs two model calls and a handful of HTTP requests rather than an
open-ended agent loop.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .config import Config
from .hra_client import HraAuthError, HraBlockedError, HraClient, HraError
from .tools.executor import ToolError, call_tool
from .prompts.live import prompt_for
from .prompts.order_qa import ANSWER_SYSTEM_PROMPT, PLAN_SYSTEM_PROMPT
from .tools.registry import (
    ToolSpec,
    missing_params,
    missing_prerequisites,
    read_only_tools,
)

# PO10003583002-668 (order group) and PB10006149945-461 (booking).
ORDER_ID_RE = re.compile(r"\b(P[OB]\d{6,}-\d{2,})\b", re.IGNORECASE)

MAX_FOLLOWUPS = 4

# A booking question always gets the bookings API, whether or not the planner
# would have picked it. It is the one call that says what HR actually stored
# for a booking -- the DiagnosticBookings rows and the digitised parameters --
# so leaving it to model choice made the answer a coin flip.
BOOKINGS_TOOL = "get_diagnostic_bookings_parameters"
MAX_BOOKING_CALLS = 3

# Deliberately broad. An extra read on an order we are already reading is
# cheap; missing the one call that holds the answer is not.
_BOOKING_WORDS = (
    "booking", "bookings", "test", "tests", "parameter", "parameters",
    "digitis", "digitiz", "panel", "analyte", "sample", "value", "values",
    "result", "results", "lab",
)

ProgressFn = Callable[[str], None]

# The two calls that are never a choice.
ENTRY_TOOLS = ("get_user_details_for_an_order", "fetch_all_orders_of_a_user")




@dataclass
class Fetched:
    """What the reads returned, keyed by tool name."""

    order_group_id: str
    results: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    trail: list[str] = field(default_factory=list)

    # raw private URL -> signed URL. Substituted wherever the raw one appears
    # in what the model is shown, so it cannot quote a link that 403s.
    signed: dict[str, str] = field(default_factory=dict)

    # --- ids discovered along the way, for filling later params ---
    user_id: str = ""
    patients: list[dict[str, Any]] = field(default_factory=list)
    bookings: list[dict[str, Any]] = field(default_factory=list)

    def called_tools(self) -> set[str]:
        """Tool names already called, ignoring the per-call suffix.

        A result key is `tool` or `tool[booking_id]` -- the same tool is called
        once per booking, and one shared key would have kept only the last.
        """
        return {k.split("[", 1)[0] for k in self.results}

    def known_ids(self) -> dict[str, Any]:
        return {
            "order_group_id": self.order_group_id,
            "user_id": self.user_id,
            "patient_ids": [str(p.get("id", "")) for p in self.patients if p.get("id")],
            "booking_ids": [
                str(b.get("booking_id") or b.get("id") or "") for b in self.bookings
            ],
        }

    def context_for_model(self, max_chars: int = 24000) -> str:
        """Every response, each with its own share of the budget.

        Not one slice over the lot: a digitised parameter list runs to
        thousands of lines, so a shared slice left the later calls out
        entirely and the answer reported them as never made. A cut is stated
        where it happens instead.
        """
        if not self.results:
            return "(nothing)"
        share = max(1500, max_chars // len(self.results))
        parts: list[str] = []
        for name, payload in self.results.items():
            blob = json.dumps(payload, indent=1, default=str)
            # Signed in place: a raw S3 URL in here is one the model may quote,
            # and a raw private URL 403s the moment it is clicked.
            for raw, signed in self.signed.items():
                blob = blob.replace(raw, signed)
            parts += [f"--- {name} ---", blob[:share]]
            if len(blob) > share:
                parts.append(
                    f"[cut off here: {share} of {len(blob)} characters shown. "
                    "Do not treat what is missing as absent from the record.]"
                )
        return "\n".join(parts)


def find_order_ids(*texts: str) -> list[str]:
    """Order-group and booking ids mentioned in any of the given strings."""
    found: list[str] = []
    for t in texts:
        for m in ORDER_ID_RE.finditer(t or ""):
            v = m.group(1).upper()
            if v not in found:
                found.append(v)
    return found


PRESIGN_TOOL = "get_presigned_url_for_private_content"
MAX_PRESIGN_CALLS = 4

# "give me the report", "can I open it", "presigned url", "share the pdf".
_LINK_WORDS = (
    "presign", "pre-sign", "signed url", "signed link", "link", "url",
    "download", "open the", "view the", "access the", "share the", "pdf",
    "see the report", "read the report",
)

# Any key that holds a URL, whatever the payload calls it.
_URL_KEYS = ("url", "pdf", "link", "href")


def is_report_link_question(*texts: str) -> bool:
    """Does this ask for something they can actually open?"""
    haystack = " ".join(t or "" for t in texts).lower()
    return any(w in haystack for w in _LINK_WORDS)


def find_private_urls(fetched: Fetched) -> list[str]:
    """Every URL this order's own data gave us, in the order found.

    Only from the fetched payloads -- never from the question. The signer will
    sign whatever URL it is handed, so taking one from the conversation would
    let a request mint access to an object this order has nothing to do with.
    """
    found: list[str] = []

    def walk(node: Any, key: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k))
        elif isinstance(node, list):
            for item in node:
                walk(item, key)
        elif isinstance(node, str):
            if not node.startswith("http"):
                return
            if not any(w in key.lower() for w in _URL_KEYS):
                return
            # Already signed: signing it again buys nothing.
            bare = node.split("?", 1)[0]
            if bare not in [u.split("?", 1)[0] for u in found]:
                found.append(node)

    walk(fetched.results)
    return found


def presign_calls(fetched: Fetched, *texts: str) -> list[dict[str, Any]]:
    """Sign the report files this order has, so they can be opened."""
    urls = find_private_urls(fetched)[:MAX_PRESIGN_CALLS]
    return [
        {
            "tool": PRESIGN_TOOL,
            "params": {"url": url, "ttl": 3600},
            "key": f"{PRESIGN_TOOL}[{url.rsplit('/', 1)[-1].split('?')[0][:40]}]",
        }
        for url in urls
    ]


def sign_private_urls(
    client: Any, fetched: Fetched, *, on_progress: ProgressFn | None = None
) -> int:
    """Sign every private URL the fetched data holds. Returns how many.

    Unconditional, not only when the question asked for a link: an answer to
    "who is the patient" can quote a `report_url` too, and a raw private URL
    403s the moment it is clicked. Signing it is the difference between a link
    and a dead end.
    """
    from .tools.executor import ToolError, call_tool
    from .tools.registry import read_only_tools

    say = on_progress or (lambda _m: None)
    spec = next((t for t in read_only_tools() if t.name == PRESIGN_TOOL), None)
    if spec is None:
        return 0

    todo = [
        url for url in find_private_urls(fetched)[:MAX_PRESIGN_CALLS]
        if url not in fetched.signed and not _looks_signed(url)
    ]
    if not todo:
        return 0

    say(f"Signing {len(todo)} report file(s)")
    for url in todo:
        try:
            answer = call_tool(client, spec, {"url": url, "ttl": 3600})
        except (ToolError, HraError) as exc:
            # Left raw rather than substituted: better a link that fails
            # visibly than a claim that it was signed.
            fetched.errors.append(f"{PRESIGN_TOOL}: {exc}")
            continue
        signed = _signed_url_from(answer)
        if signed and signed != url:
            fetched.signed[url] = signed
    return len(fetched.signed)


def _looks_signed(url: str) -> bool:
    lowered = url.lower()
    return "x-amz-signature" in lowered or "&sig=" in lowered or "?sig=" in lowered


def _signed_url_from(answer: Any) -> str:
    """The signer returns the URL under one of a few keys, or as a bare string."""
    if isinstance(answer, str):
        return answer if answer.startswith("http") else ""
    if isinstance(answer, dict):
        for key in ("url", "signed_url", "presigned_url", "presignedUrl", "data"):
            value = answer.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
            if isinstance(value, dict):
                nested = _signed_url_from(value)
                if nested:
                    return nested
    return ""


# Anything that is not one of these ends a URL token in prose.
_URL_END = set(" \t\n\r\"'`<>()[]{},;")

# What replaces a URL the data never returned. Deliberately says the data is
# missing rather than the link is broken: the engineer's next step is to find
# out why nothing was returned.
NO_URL = "(no link for that in the data)"


def allowed_urls(fetched: Fetched) -> dict[str, str]:
    """URL -> what may be shown for it. Anything absent must not be shown.

    gemma3:latest, told the report for one booking and nothing for the other,
    wrote out `.../reports/1/b.pdf?X-Amz-Signature=...` for the second by
    pattern-matching the first (5/5 runs) and captioned it as a report link.
    The prompt already forbids rebuilding a URL, so the rule has to be
    enforced where a model cannot ignore it. A fabricated S3 key is not a dead
    link -- it can name a real object belonging to a different patient.
    """
    return allowed_urls_in(fetched.results, fetched.signed)


def allowed_urls_in(
    results: Any, signed: dict[str, str] | None = None
) -> dict[str, str]:
    """The same map, from raw payloads rather than a Fetched.

    Every http string in the payloads counts, not only the ones under a key
    named url/pdf/link/href: the question is whether the DATA contained this
    URL, and narrowing it by key name would replace a real link with the
    "not in the data" note.
    """
    signed = signed or {}
    ok: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
        elif isinstance(node, str) and node.startswith(("http://", "https://")):
            ok[node] = signed.get(node, node)

    walk(results)
    for raw, s in signed.items():
        ok[raw] = s
        ok[s] = s                    # already-signed form quoted back
    return ok


def scrub_urls(chunks: Iterator[str], allowed: dict[str, str]) -> Iterator[str]:
    """Pass the model's text through, replacing any URL the data did not give.

    Streams: only the tail from a live "http" is held back, so the answer still
    arrives a token at a time.
    """
    held = ""
    for chunk in chunks:
        held += chunk
        while True:
            start = held.find("http")
            if start < 0:
                # Keep the last 3 chars back: they may be a partial "http".
                cut = max(0, len(held) - 3)
                safe, held = held[:cut], held[cut:]
                if safe:
                    yield safe
                break
            if start:
                yield held[:start]
                held = held[start:]
            end = next((i for i, c in enumerate(held) if c in _URL_END), -1)
            if end < 0:
                break                # URL still arriving
            url, held = held[:end], held[end:]
            yield _resolve_url(url, allowed)
    if held:
        if held.startswith("http"):
            yield _resolve_url(held, allowed)
        else:
            yield held


def _resolve_url(url: str, allowed: dict[str, str]) -> str:
    """One URL from the model: the signed form, or a note that it is not real."""
    if url in allowed:
        return allowed[url]
    # Trailing punctuation is prose, not part of the URL -- and it has to
    # survive the replacement, or "see <url>." loses its full stop.
    stripped = url.rstrip(".,;:!?")
    tail = url[len(stripped):]
    if stripped in allowed:
        return allowed[stripped] + tail
    # The bare word "http" in prose is not a URL, and must not be replaced.
    if not stripped.startswith(("http://", "https://")):
        return url
    return NO_URL + tail


# "Report links expire in about an hour" under an answer containing no link
# reads as though one were given, and sends the reader hunting for it. The
# prompt asks for it only alongside a link and a small model says it anyway,
# so when the order's data holds no URL at all the sentence is removed here --
# a decision that needs no model, because there is nothing that could expire.
_LINK_NOTE = re.compile(
    r"(?i)\s*(?:and\s+|also,?\s+)?(?:the\s+)?report\s+links?\s+expire[^.\n]*\.?"
)


def drop_link_notes(chunks: Iterator[str]) -> Iterator[str]:
    """Strip the expiry note as it streams. Line-buffered, like the scrubber."""
    held = ""
    for chunk in chunks:
        held += chunk
        while "\n" in held:
            line, held = held.split("\n", 1)
            cleaned = _LINK_NOTE.sub("", line)
            # A line that was only the note goes entirely; a blank line stays,
            # because it is paragraph spacing rather than content.
            if cleaned.strip() or not line.strip():
                yield cleaned + "\n"
    if held:
        cleaned = _LINK_NOTE.sub("", held)
        if cleaned.strip():
            yield cleaned


def overview(fetched: Fetched) -> str:
    """What this order actually contains, counted rather than described.

    An open-ended "what is PO…" gave a small model nothing to aim at, and it
    answered by offering a menu -- "would you like me to extract, filter,
    analyze, summarise?" -- without ever saying what the order was. The shape
    of the order is arithmetic, so it is stated here and the model narrates.
    """
    lines: list[str] = []
    if fetched.order_group_id:
        lines.append(f"- order_group_id: {fetched.order_group_id}")
    if fetched.user_id:
        lines.append(f"- user_id: {fetched.user_id}")
    if fetched.patients:
        names = ", ".join(
            f"{p.get('id')}" + (f" ({p.get('name')})" if p.get("name") else "")
            for p in fetched.patients[:6]
        )
        lines.append(f"- patients: {len(fetched.patients)} — {names}")
    if fetched.bookings:
        lines.append(f"- bookings: {len(fetched.bookings)}")
        for b in fetched.bookings[:6]:
            bits = [str(b.get("booking_id") or "?")]
            if b.get("status"):
                bits.append(str(b["status"]))
            if b.get("collection_time"):
                bits.append(f"collected {b['collection_time']}")
            lines.append("  - " + " — ".join(bits))
    params = fetched.results.get("get_diagnostic_bookings_parameters")
    n_params = 0
    if isinstance(params, dict):
        for value in params.values():
            if isinstance(value, list):
                n_params += len(value)
            elif isinstance(value, dict) and isinstance(value.get("parameters"), list):
                n_params += len(value["parameters"])
    elif isinstance(params, list):
        n_params = len(params)
    if n_params:
        lines.append(f"- lab parameters returned: {n_params}")
    if not lines:
        # Nothing was fetched. A block saying "0 report links" is noise.
        return ""
    lines.append(f"- report links available: {len(allowed_urls(fetched))}")
    if fetched.errors:
        lines.append(f"- calls that failed: {len(fetched.errors)}")
    return "=== WHAT THIS ORDER HOLDS (counted in code) ===\n" + "\n".join(lines)


def is_booking_question(*texts: str) -> bool:
    """Does this question need what HR stored for a booking?"""
    haystack = " ".join(t or "" for t in texts).lower()
    if re.search(r"\bpb\d{6,}-\d{2,}\b", haystack):
        return True
    return any(w in haystack for w in _BOOKING_WORDS)


def _booking_sort_key(b: dict[str, Any]) -> str:
    return str(b.get("delivery_time") or b.get("created_at") or "")


def booking_calls(
    fetched: Fetched, *texts: str
) -> list[dict[str, Any]]:
    """The bookings API, once per booking worth asking about.

    A booking id named in the conversation wins; otherwise the bookings on this
    order, newest first. `order_group_id` and `booking_id` come from the entry
    calls, never from the question -- the question only chooses which booking.
    """
    if BOOKINGS_TOOL in fetched.called_tools():
        return []

    rows = [b for b in fetched.bookings if b.get("booking_id") or b.get("id")]
    if not rows:
        return []

    named = {
        i for i in find_order_ids(*texts) if i.startswith("PB")
    }
    if named:
        rows = [
            b for b in rows
            if str(b.get("booking_id") or b.get("id") or "").upper() in named
        ] or rows

    rows = sorted(rows, key=_booking_sort_key, reverse=True)[:MAX_BOOKING_CALLS]
    calls: list[dict[str, Any]] = []
    for b in rows:
        bid = str(b.get("booking_id") or b.get("id") or "")
        params: dict[str, Any] = {
            "order_group_id": str(b.get("order_group_id") or fetched.order_group_id),
            "booking_id": bid,
        }
        pid = str(b.get("patient_id") or "")
        if pid:
            params["patient_id"] = pid
        calls.append({
            "tool": BOOKINGS_TOOL,
            "params": params,
            "key": f"{BOOKINGS_TOOL}[{bid}]",
        })
    return calls


def _tools_by_name() -> dict[str, ToolSpec]:
    return {t.name: t for t in read_only_tools()}


def gather_entry(
    client: HraClient, order_group_id: str, *, on_progress: ProgressFn | None = None
) -> Fetched:
    """Make the two entry calls and record the ids they reveal."""
    say = on_progress or (lambda _m: None)
    tools = _tools_by_name()
    out = Fetched(order_group_id=order_group_id)

    for name in ENTRY_TOOLS:
        spec = tools.get(name)
        if spec is None:
            out.errors.append(f"{name} is not documented in order_info.md")
            continue
        params = {
            "order_group_id": order_group_id,
            "user_id": out.user_id,
        }
        if name == "fetch_all_orders_of_a_user" and not out.user_id:
            out.errors.append("No user_id for this order, so its bookings cannot be listed.")
            continue

        say(f"{spec.method} {spec.path}")
        try:
            data = call_tool(client, spec, params)
        except (ToolError, HraError) as exc:
            out.errors.append(f"{name}: {exc}")
            out.trail.append(f"{name} failed")
            if isinstance(exc, (HraAuthError, HraBlockedError)):
                raise
            continue

        out.results[name] = data
        out.trail.append(name)

        if name == "get_user_details_for_an_order" and isinstance(data, dict):
            out.user_id = str(data.get("user_id") or "")
            out.patients = [p for p in (data.get("patient_details") or []) if isinstance(p, dict)]
            say(f"user {out.user_id or '?'} · {len(out.patients)} patient(s)")
        elif name == "fetch_all_orders_of_a_user":
            rows = data if isinstance(data, list) else (data or {}).get("orders") or []
            out.bookings = [b for b in rows if isinstance(b, dict)]
            say(f"{len(out.bookings)} booking(s)")

    return out


def plan_followups(cfg: Config, question: str, fetched: Fetched) -> list[dict[str, Any]]:
    """One capped planning round over the remaining reads."""
    called = fetched.called_tools()
    remaining = [t for t in read_only_tools() if t.name not in called]
    if not remaining:
        return []

    prompt = "\n".join(
        [
            f"Question: {question}",
            "",
            "Available tools:",
            *[t.describe() for t in remaining],
            "",
            "Ids already known:",
            json.dumps(fetched.known_ids(), indent=1),
            "",
            "Data already fetched:",
            fetched.context_for_model(6000),
            "",
            "Return the JSON object now.",
        ]
    )
    text = _complete_json(cfg, prompt_for("order.plan", PLAN_SYSTEM_PROMPT), prompt)
    try:
        from .summarizer import _parse_json_object

        data = _parse_json_object(text)
    except Exception:  # noqa: BLE001 - a bad plan is not fatal
        return []

    calls = data.get("calls")
    if not isinstance(calls, list):
        return []

    names = {t.name for t in remaining}
    clean: list[dict[str, Any]] = []
    for c in calls[:MAX_FOLLOWUPS]:
        if not isinstance(c, dict):
            continue
        tool = str(c.get("tool") or "")
        params = c.get("params")
        if tool not in names or not isinstance(params, dict):
            continue
        # A placeholder means the model did not have the id; drop the call
        # rather than sending "<patient_id>" to production.
        if any("<" in str(v) for v in params.values()):
            continue
        if missing_prerequisites(tool, called):
            # The ids it needs do not exist yet, so whatever it filled them
            # with is not from this order.
            continue
        spec = next(t for t in remaining if t.name == tool)
        clean.extend(_complete_call(fetched, spec, dict(params)))
    return clean[:MAX_FOLLOWUPS]


def _complete_call(
    fetched: Fetched, spec: ToolSpec, params: dict[str, Any]
) -> list[dict[str, Any]]:
    """Fill a planned call's ids from the chain, or expand it per booking.

    A model that asks for bookings+parameters without naming a booking meant
    "for this order", not "for nothing" -- so the request is honoured against
    every booking the chain found rather than dropped or sent incomplete.
    """
    # Only ids this tool declares: adding an order id to a call that takes a
    # patient id would just clutter what the UI shows was called.
    if "order_group_id" in spec.required_params and not params.get("order_group_id"):
        params["order_group_id"] = fetched.order_group_id

    if not missing_params(spec.name, params, spec):
        return [{"tool": spec.name, "params": params}]

    if spec.name != BOOKINGS_TOOL:
        return []

    named = str(params.get("booking_id") or "").strip()
    return booking_calls(fetched, named) if named else booking_calls(fetched)


def run_followups(
    client: HraClient,
    fetched: Fetched,
    calls: list[dict[str, Any]],
    *,
    on_progress: ProgressFn | None = None,
) -> None:
    say = on_progress or (lambda _m: None)
    tools = _tools_by_name()
    for call in calls:
        spec = tools[call["tool"]]
        params = {**fetched.known_ids(), **call["params"]}
        key = str(call.get("key") or spec.name)
        say(f"{spec.method} {spec.path}")
        try:
            fetched.results[key] = call_tool(client, spec, params)
            fetched.trail.append(key)
        except (ToolError, HraError) as exc:
            fetched.errors.append(f"{key}: {exc}")
            fetched.trail.append(f"{key} failed")


def answer_stream(cfg: Config, question: str, fetched: Fetched) -> Iterator[str]:
    prompt = "\n".join(
        [
            f"Question: {question}",
            "",
            f"order_group_id: {fetched.order_group_id}",
            "",
            overview(fetched),
            "",
            "API responses:",
            fetched.context_for_model(),
            *(["", "Calls that failed:", *[f"- {e}" for e in fetched.errors]]
              if fetched.errors else []),
            "",
            "Answer now.",
        ]
    )
    # Every URL that reaches the UI has to be one this order's data returned:
    # the UI turns URLs into click-through links, so a rebuilt S3 key would be
    # handed to the engineer as a working report.
    allowed = allowed_urls(fetched)
    stream = scrub_urls(_stream_text(cfg, prompt_for("order.answer", ANSWER_SYSTEM_PROMPT), prompt), allowed)
    if not allowed:
        stream = drop_link_notes(stream)
    yield from stream


# --- backend plumbing ------------------------------------------------------


def _complete_json(cfg: Config, system: str, prompt: str) -> str:
    backend = cfg.summarizer.backend
    if backend == "anthropic_api":
        from .anthropic_backend import complete_json

        return complete_json(cfg, system, prompt)
    if backend == "local":
        from .local_backend import complete_json

        return complete_json(cfg, system, prompt)

    from .streaming import stream_claude

    return "".join(
        stream_claude(prompt, system, model=cfg.summarizer.model, timeout_seconds=120)
    )


def _stream_text(cfg: Config, system: str, prompt: str) -> Iterator[str]:
    backend = cfg.summarizer.backend
    if backend == "anthropic_api":
        from .anthropic_backend import stream_answer

        yield from stream_answer(cfg, system, prompt)
        return
    if backend == "local":
        from .local_backend import stream_answer

        yield from stream_answer(cfg, system, prompt)
        return

    from .streaming import stream_claude

    yield from stream_claude(
        prompt, system, model=cfg.summarizer.model,
        timeout_seconds=cfg.summarizer.timeout_seconds,
    )
