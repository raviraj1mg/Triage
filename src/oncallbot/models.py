"""Domain objects: what we pull out of Gmail, and what the model gives back."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SEVERITIES = ("p0", "p1", "p2", "p3")


@dataclass
class Attachment:
    filename: str
    mime_type: str
    size_bytes: int
    # Needed to fetch the bytes: attachments are not inlined in a thread.get.
    attachment_id: str = ""
    message_id: str = ""


@dataclass
class EmailMessage:
    id: str
    thread_id: str
    date: datetime | None
    sender: str
    to: str
    cc: str
    subject: str
    body_text: str
    snippet: str
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class EmailThread:
    id: str
    subject: str
    messages: list[EmailMessage]
    label_ids: list[str] = field(default_factory=list)

    @property
    def first(self) -> EmailMessage:
        return self.messages[0]

    @property
    def last(self) -> EmailMessage:
        return self.messages[-1]

    @property
    def permalink(self) -> str:
        return f"https://mail.google.com/mail/u/0/#all/{self.id}"

    @property
    def attachments(self) -> list[Attachment]:
        return [a for m in self.messages for a in m.attachments]

    def participants(self, limit: int = 4) -> list[str]:
        """Distinct senders, oldest first, for showing who is on the thread."""
        seen: list[str] = []
        for m in self.messages:
            name = _display_name(m.sender)
            if name and name not in seen:
                seen.append(name)
        return seen[:limit]


def _display_name(addr: str) -> str:
    """"Asha Menon <a@b.com>" -> "Asha Menon"; a bare address stays as-is."""
    addr = addr.strip()
    if "<" in addr:
        name = addr.split("<", 1)[0].strip().strip('"')
        if name:
            return name
        return addr.split("<", 1)[1].rstrip(">").strip()
    return addr


@dataclass
class AffectedEntities:
    """Identifiers a human (or, later, an automated fix) would need to act."""

    patient_ids: list[str] = field(default_factory=list)
    order_ids: list[str] = field(default_factory=list)
    prescription_ids: list[str] = field(default_factory=list)
    record_ids: list[str] = field(default_factory=list)
    user_emails: list[str] = field(default_factory=list)
    other_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> AffectedEntities:
        d = d or {}
        return cls(
            **{
                f: [str(x) for x in (d.get(f) or [])]
                for f in (
                    "patient_ids",
                    "order_ids",
                    "prescription_ids",
                    "record_ids",
                    "user_emails",
                    "other_ids",
                )
            }
        )

    def is_empty(self) -> bool:
        return not any(vars(self).values())


# A claimed closure below this is treated as open. Marking a live ticket closed
# is the expensive mistake -- nobody looks at it again -- so the threshold is
# applied here, in code, and never left to the model. Both readers of a thread
# (the summarizer and the diagnosis) go through decide_closure, so they cannot
# disagree about what "closed" means.
CLOSURE_CONFIDENCE_THRESHOLD = 0.75


def decide_closure(
    claimed: bool, confidence: float, reason: str = ""
) -> tuple[bool, float, str]:
    """Turn a model's closure claim into a verdict. Returns (closed, confidence, reason)."""
    confidence = _clamp_float(confidence)
    closed = bool(claimed) and confidence >= CLOSURE_CONFIDENCE_THRESHOLD
    reason = str(reason or "").strip()
    if claimed and not closed:
        # Say why it is still open, rather than silently dropping the claim.
        reason = (
            f"A reply may have resolved this, but not clearly enough to call it "
            f"closed (confidence {confidence:.2f})"
            + (f": {reason}" if reason else "")
        )
    return closed, confidence, reason


@dataclass
class IssueSummary:
    """The structured read of one thread."""

    thread_id: str
    subject: str
    reporter: str
    last_message_at: str
    summary: str
    issue: str
    category: str
    severity: str
    # Thread span start. Defaulted so a single-message thread, or a row from an
    # older store, degrades to last_message_at rather than breaking callers.
    first_message_at: str = ""
    asks: list[str] = field(default_factory=list)
    missing_info: list[str] = field(default_factory=list)
    affected: AffectedEntities = field(default_factory=AffectedEntities)
    suggested_owner: str = ""
    confidence: float = 0.0
    permalink: str = ""
    model: str = ""
    # Read from the thread, same question the diagnosis asks. None means it was
    # never read -- a summary cached before this existed, or a backend whose
    # output did not carry it. Absent is not "open": showing a verdict nobody
    # established would be a guess.
    closed: bool | None = None
    closure_confidence: float = 0.0
    closed_by: str = ""
    closed_at: str = ""
    closure_reason: str = ""

    @classmethod
    def from_model_output(
        cls, data: dict[str, Any], thread: EmailThread, model: str
    ) -> IssueSummary:
        severity = str(data.get("severity", "p3")).lower()
        if severity not in SEVERITIES:
            severity = "p3"
        last = thread.last
        first = thread.first

        closed: bool | None = None
        confidence = 0.0
        closed_by = ""
        closed_at = ""
        closure_reason = ""
        if data.get("closed") is not None:
            closed, confidence, closure_reason = decide_closure(
                bool(data.get("closed")),
                data.get("closure_confidence"),
                str(data.get("closure_reason") or ""),
            )
            if closed:
                closed_by = str(data.get("closed_by") or "").strip()
                closed_at = str(data.get("closed_at") or "").strip()

        return cls(
            thread_id=thread.id,
            subject=thread.subject,
            reporter=str(data.get("reporter") or thread.first.sender),
            first_message_at=first.date.isoformat() if first.date else "",
            last_message_at=last.date.isoformat() if last.date else "",
            summary=str(data.get("summary", "")).strip(),
            issue=str(data.get("issue", "")).strip(),
            category=str(data.get("category", "other")),
            severity=severity,
            asks=[str(x) for x in (data.get("asks") or [])],
            missing_info=[str(x) for x in (data.get("missing_info") or [])],
            affected=AffectedEntities.from_dict(data.get("affected_entities")),
            suggested_owner=str(data.get("suggested_owner", "")),
            confidence=_clamp_float(data.get("confidence")),
            permalink=thread.permalink,
            model=model,
            closed=closed,
            closure_confidence=confidence,
            closed_by=closed_by,
            closed_at=closed_at,
            closure_reason=closure_reason,
        )

    def to_dict(self) -> dict[str, Any]:
        d = vars(self).copy()
        d["affected"] = vars(self.affected).copy()
        return d


def _clamp_float(v: Any) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0
