"""Prompts as edited in the UI, for the turn that carries them.

A prompt is behaviour: most of what this tool gets right or wrong is decided
by the wording, and the fastest way to fix a bad answer is to change the
words and ask again. The UI exposes the prompt behind each answer, lets it be
edited, and sends the edit back with the next question.

Held per REQUEST, not per session and never on disk. The client keeps the
edits in memory and attaches them to each call, so a page refresh is what
resets them -- which is what "try something, see if it is better" wants, and
means one person's experiment cannot leak into anyone else's answers or
outlive the tab.

The defaults are the ones in this package. An override is used only for the
key it was edited under, so editing the routing prompt cannot change how a
diagnosis is narrated.
"""

from __future__ import annotations

from contextvars import ContextVar

from . import (
    chat,
    diagnosis,
    grouping,
    order_qa as order_prompts,
    routing,
    suggest,
    summarize,
)

# Set by the request worker for the duration of one turn. A worker thread does
# not inherit the caller's context, so it is set explicitly there -- the same
# way the cancel token is.
OVERRIDES: ContextVar[dict[str, str]] = ContextVar("oncallbot_prompts", default={})


def prompt_for(key: str, default: str) -> str:
    """The edited prompt for this key, or the one that ships.

    Every model call in the tool resolves its system prompt here, which makes
    this the one place that knows a call is about to happen and which wording
    it will use -- so it is also where the trace is recorded.
    """
    from ..trace import note

    edited = (OVERRIDES.get() or {}).get(key)
    text = edited if isinstance(edited, str) and edited.strip() else default
    label = CATALOG.get(key, ("", key, ""))[1]
    note("prompt", label, key=key, edited=bool(edited), chars=len(text), text=text)
    return text


def set_overrides(edits: dict[str, str] | None) -> None:
    """Called once per turn, on the thread that will do the work."""
    clean = {
        k: v for k, v in (edits or {}).items()
        if k in CATALOG and isinstance(v, str) and v.strip()
    }
    OVERRIDES.set(clean)


# key -> (surface it belongs to, human label, the shipped text).
#
# `surface` is what the UI groups by: which CTA offers this prompt. A key that
# no surface names would be editable by nobody, so the listing is the contract.
CATALOG: dict[str, tuple[str, str, str]] = {
    "routing": (
        "routing", "Routing — which action a message becomes",
        routing.SYSTEM_PROMPT_TEMPLATE,
    ),
    "summarize": (
        "summary", "Summarizing one thread",
        summarize.SYSTEM_PROMPT,
    ),
    "diagnosis.reason": (
        "diagnosis", "Diagnosis — why the oncall happened",
        diagnosis.REASON_SYSTEM_PROMPT,
    ),
    "diagnosis.findings": (
        "diagnosis", "Diagnosis — what the evidence shows",
        diagnosis.FINDINGS_SYSTEM_PROMPT,
    ),
    "diagnosis.followup": (
        "diagnosis", "Diagnosis — narrating the further check",
        diagnosis.FOLLOWUP_SYSTEM_PROMPT,
    ),
    "diagnosis.followup_plan": (
        "diagnosis", "Diagnosis — which further read to run",
        diagnosis.FOLLOWUP_PLAN_SYSTEM_PROMPT,
    ),
    "chat.context": (
        "followup", "Follow-up about what is already on screen",
        chat.CONTEXT_SYSTEM_PROMPT,
    ),
    "chat.answer": (
        "followup", "Question about the inbox",
        chat.ANSWER_SYSTEM_PROMPT,
    ),
    "order.answer": (
        "followup", "Order / booking answer",
        order_prompts.ANSWER_SYSTEM_PROMPT,
    ),
    "order.plan": (
        "followup", "Order / booking — which reads to run",
        order_prompts.PLAN_SYSTEM_PROMPT,
    ),
    "suggest": (
        "followup", "The suggestion chips",
        suggest.SYSTEM_PROMPT,
    ),
    "grouping": (
        "followup", "Sorting a window into categories",
        grouping.GROUPING_SYSTEM_PROMPT,
    ),
    "diagnosis.closure": (
        "diagnosis", "Diagnosis — does the mail thread read as resolved",
        diagnosis.CLOSURE_SYSTEM_PROMPT,
    ),
}


def catalog_for(surface: str) -> list[dict[str, str]]:
    """Everything one CTA should offer, defaults included."""
    return [
        {"key": key, "label": label, "text": text}
        for key, (where, label, text) in CATALOG.items()
        if where == surface
    ]
