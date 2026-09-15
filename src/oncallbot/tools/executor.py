"""Execute a parsed ToolSpec against the admin API.

Defence in depth: the registry only hands out reads, and this refuses a
non-read again. Two independent checks, because the cost of getting it wrong
is a mutation to a patient record.
"""

from __future__ import annotations

from typing import Any

from ..hra_client import HraClient
from .registry import PREREQUISITES, ToolSpec, missing_params


class ToolError(RuntimeError):
    """The call could not be made as asked. Not an API failure."""


def call_tool(client: HraClient, spec: ToolSpec, params: dict[str, Any]) -> Any:
    from ..trace import note

    note("tool", spec.name, params=dict(params), method=spec.method)
    if not spec.is_read:
        raise ToolError(
            f"{spec.name} is a {spec.method} write and is not callable here. "
            "Writes are reserved for the resolve phase, behind their own "
            "allowlist and confirmation."
        )

    # Path parameters plus anything the tool cannot answer meaningfully
    # without. bookings+parameters is the case that matters: called with only
    # the order id it answers for the whole order, and the reply reads as if
    # the booking's data were missing rather than never asked for.
    missing = missing_params(spec.name, params, spec)
    if missing:
        chain = PREREQUISITES.get(spec.name, ())
        hint = (
            f" Those come from {' then '.join(chain)}."
            if chain else ""
        )
        raise ToolError(f"{spec.name} needs {', '.join(missing)}.{hint}")

    path = spec.path
    for name in spec.path_params:
        path = path.replace("{}", str(params[name]).strip(), 1)

    query = {
        p: params[p]
        for p in spec.query_params
        if p not in spec.path_params and str(params.get(p) or "").strip()
    }

    if spec.method == "GET":
        return client.get(path, query)

    # The one POST read: signing a private URL.
    body = {k: v for k, v in params.items() if k in spec.body_params or k == "ttl"}
    if "url" in params:
        body["url"] = params["url"]
    if not body.get("url"):
        raise ToolError(f"{spec.name} needs a `url` to sign.")
    return client.post_read(path, body)
