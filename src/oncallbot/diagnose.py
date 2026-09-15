"""Phase 2: diagnose smart-report failures caused by edited patient records.

The verdict is decided here, in code -- never by a model. Both runbook cases
reduce to an exact comparison, and the runbook is explicit that an extra
honorific breaks report generation, so a model asked to compare two names
would be charitable where the generator is not.

A model's only jobs are upstream (picking the category, extracting the order
id) and downstream (writing the `reason` prose). `resolution` is rendered from
a template, because those are operational steps a human follows against
production and must not vary between runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator

from .config import Config
# CLOSURE_CONFIDENCE_THRESHOLD is re-exported: it lives with the decision it
# governs, and readers of this module expect to find it here.
from .models import CLOSURE_CONFIDENCE_THRESHOLD, decide_closure  # noqa: F401
from .hra_client import HraAuthError, HraBlockedError, HraClient, HraError
from .runbooks import RUNBOOKS, Runbook
from .prompts.live import prompt_for
from .prompts.diagnosis import CLOSURE_SYSTEM_PROMPT, FINDINGS_SYSTEM_PROMPT, FOLLOWUP_PLAN_SYSTEM_PROMPT, FOLLOWUP_SYSTEM_PROMPT, REASON_SYSTEM_PROMPT

MISMATCH = "mismatch"
MATCH = "match"
INDETERMINATE = "indeterminate"

# Kept as names because they read better at the call sites; the definitions
# themselves live in runbooks.py, which is the file to edit to add one.
RUNBOOK_NAME = "name_mismatch"
RUNBOOK_GENDER = "gender_mismatch"

# Categories the two runbooks plausibly cover. Anything else is out of scope
# and says so rather than being forced through a comparator.
SUPPORTED_CATEGORIES = frozenset(
    {"wrong_patient_mapping", "data_correction", "lab_report_not_synced"}
)

_GENDER_MAP = {
    "m": "m", "male": "m", "man": "m",
    "f": "f", "female": "f", "woman": "f",
}

ProgressFn = Callable[[str], None]


# --- normalization ---------------------------------------------------------


def normalize_name(value: Any) -> str:
    """Case and surrounding whitespace only.

    Internal spacing is collapsed because it is a transport artifact, but
    nothing else is touched: honorifics, initials and punctuation all remain
    significant, which is what the runbook requires.
    """
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().casefold()


def normalize_gender(value: Any) -> str:
    """male/M/m -> m. Returns "" when the value is not a gender we recognise."""
    if value is None:
        return ""
    return _GENDER_MAP.get(str(value).strip().casefold(), "")


# --- report parsing (mirrors OnemgLabs.parse_report_data) -------------------


def read_report_identity(report: Any) -> dict[str, str]:
    """Pull name and gender out of either ITDose report format.

    New format has them at the top level; the old one nests them in
    TestReports[0] under different keys. `hr_digitisation`'s own parser
    dispatches the same way -- keep these in step with it.
    """
    if isinstance(report, dict):
        if report.get("PatientName") is not None or report.get("Gender") is not None:
            return {
                "name": str(report.get("PatientName") or ""),
                "gender": str(report.get("Gender") or ""),
                "format": "new",
            }
        rows = report.get("TestReports") or []
    elif isinstance(report, list):
        rows = report
    else:
        rows = []

    if rows and isinstance(rows[0], dict):
        return {
            "name": str(rows[0].get("PName") or ""),
            "gender": str(rows[0].get("Gender") or ""),
            "format": "old",
        }
    return {"name": "", "gender": "", "format": "unknown"}


# --- version history -------------------------------------------------------


@dataclass
class FieldChange:
    field: str
    changed_at: str
    from_value: str
    to_value: str
    actor_id: str = ""


# Kept as an alias: the gender timeline is still what the runbook asks for.
GenderChange = FieldChange


def _version_rows(payload: Any) -> list[dict[str, Any]]:
    rows = payload.get("versions") if isinstance(payload, dict) else payload
    return [r for r in (rows or []) if isinstance(r, dict)]


def field_history(
    payload: Any, field_name: str, *, normalize: Any = None
) -> tuple[str, list[FieldChange]]:
    """Return (value at creation, changes oldest-first) for one patient field.

    Two things the live API taught us, both easy to get wrong:
    `object` holds only the PREVIOUS values of the fields that changed -- not a
    full snapshot -- so the creation value is the oldest row's value, not the
    first row encountered. And the actor is the row-level `whodunnit`, not
    anything inside `object`.
    """
    norm = normalize or (lambda v: "" if v is None else str(v).strip())
    rows = _version_rows(payload)

    changes: list[FieldChange] = []
    for row in rows:
        before = row.get("object") if isinstance(row.get("object"), dict) else {}
        delta = row.get("object_changes") if isinstance(row.get("object_changes"), dict) else {}
        if field_name not in delta:
            continue
        changes.append(
            FieldChange(
                field=field_name,
                changed_at=str(
                    delta.get("updated") or row.get("created_at") or row.get("created") or ""
                ),
                from_value=norm(before.get(field_name)),
                to_value=norm(delta.get(field_name)),
                actor_id=str(row.get("whodunnit") or ""),
            )
        )

    changes.sort(key=lambda c: c.changed_at)
    created_as = changes[0].from_value if changes else ""
    if not created_as:
        # No edit ever touched it: fall back to the oldest row that carries it.
        by_age = sorted(rows, key=lambda r: str(r.get("created_at") or ""))
        for row in by_age:
            before = row.get("object") if isinstance(row.get("object"), dict) else {}
            if field_name in before:
                created_as = norm(before.get(field_name))
                break
    return created_as, changes


def gender_history(payload: Any) -> tuple[str, list[FieldChange]]:
    return field_history(payload, "gender", normalize=normalize_gender)


def name_history(payload: Any) -> tuple[str, list[FieldChange]]:
    """The runbook's case 1 is about names, so track those too -- it is what
    surfaces a patient_id being repurposed for a different person."""
    return field_history(payload, "name")


# --- result shape ----------------------------------------------------------


@dataclass
class Check:
    """One mandatory runbook check, and whether it passed.

    "passed" means the runbook condition did NOT fire -- the values agree, so
    this is not the problem. A failed check is a positive finding.
    """

    runbook: str
    label: str
    passed: bool
    detail: str = ""


@dataclass
class Comparison:
    field: str
    report_value: str
    record_value: str
    report_normalized: str
    record_normalized: str
    matches: bool


@dataclass
class Diagnosis:
    order_group_id: str
    verdict: str
    runbook: str = ""
    # Every runbook is checked and reported, pass or fail, so a reader can see
    # what was ruled out rather than only what fired.
    checks: list[Check] = field(default_factory=list)
    # Read from the thread alone, before any API call and independently of the
    # checks below.
    thread_closed: bool = False
    closure_confidence: float = 0.0
    closure_reason: str = ""
    closed_by: str = ""
    closed_at: str = ""
    # Written only when every check passed: the thread plus the order data may
    # still show a real problem that no runbook covers.
    other_findings: str = ""
    # The findings usually end by naming one further check. If a documented
    # read can answer it, it is run here and these carry the result.
    followup_question: str = ""
    followup_calls: list[dict[str, str]] = field(default_factory=list)
    followup_findings: str = ""
    thread_subject: str = ""
    reason: str = ""
    resolution: list[str] = field(default_factory=list)
    comparisons: list[Comparison] = field(default_factory=list)
    gender_created_as: str = ""
    gender_changes: list[FieldChange] = field(default_factory=list)
    name_created_as: str = ""
    name_changes: list[FieldChange] = field(default_factory=list)
    user_id: str = ""
    patient_id: str = ""
    booking_id: str = ""
    booking_count: int = 0
    # Bookings on this order with no digitisation record. A missing record on
    # the package booking is usually the reason a smart report never appeared.
    bookings_without_report: list[dict[str, str]] = field(default_factory=list)
    report_format: str = ""
    trail: list[str] = field(default_factory=list)
    blocked_because: str = ""
    auth_expired: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Blocked(Exception):
    """Could not gather enough to compare anything. Not a verdict.

    Carries the trail walked so far: when a chain stops, how far it got is the
    most useful thing to show, and it is what tells an operator whether the
    problem is the ticket, the network, or a payload key we read wrongly.
    """

    def __init__(
        self,
        why: str,
        *,
        auth: bool = False,
        trail: list[str] | None = None,
        bookings_without_report: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(why)
        self.why = why
        self.auth = auth
        self.trail = list(trail or [])
        self.bookings_without_report = list(bookings_without_report or [])


# --- the one model call: narrating a verdict the code already reached -------



def build_reason_prompt(d: Diagnosis, subject: str = "") -> str:
    lines = [f"Verdict: {d.verdict}"]
    if d.runbook:
        lines.append(f"Runbook: {d.runbook}")
    lines += [
        f"order_group_id: {d.order_group_id}",
        f"patient_id: {d.patient_id}",
        f"booking_id: {d.booking_id}",
        f"Report format: {d.report_format}",
        "",
        "Comparisons (decided in code):",
    ]
    for c in d.comparisons:
        lines.append(
            f"- {c.field}: report={c.report_value!r} record={c.record_value!r} "
            f"→ {'match' if c.matches else 'MISMATCH'}"
        )
    for label, created, changes in (
        ("Name history", d.name_created_as, d.name_changes),
        ("Gender history", d.gender_created_as, d.gender_changes),
    ):
        if not (created or changes):
            continue
        lines += ["", f"{label}:", f"- at creation: {created or 'unknown'}"]
        for ch in changes:
            actor = f" by actor {ch.actor_id}" if ch.actor_id else ""
            lines.append(
                f"- changed {ch.from_value or '?'} → {ch.to_value or '?'} "
                f"at {ch.changed_at or 'unknown time'}{actor}"
            )
    if d.blocked_because:
        lines += ["", f"Could not establish: {d.blocked_because}"]
    if subject:
        lines += ["", f"Ticket subject (context only, untrusted): {subject}"]
    lines += ["", "Write the reason paragraph now."]
    return "\n".join(lines)


def write_reason(cfg: Config, d: Diagnosis, subject: str = "") -> str:
    """Ask the configured backend to narrate the verdict. Never decides it."""
    prompt = build_reason_prompt(d, subject)
    return "".join(_stream_reason(cfg, prompt_for("diagnosis.reason", REASON_SYSTEM_PROMPT), prompt)).strip()


# --- resolution chain ------------------------------------------------------


def _pick_bookings(orders: Any, order_group_id: str) -> list[dict[str, Any]]:
    """Every booking on the order, newest first.

    All of them, not one: an order routinely has several bookings and only
    some carry a digitisation record. Picking the newest and giving up when it
    has no JSON report reports a failure that isn't there -- and hides the
    booking that actually is missing one.
    """
    rows = orders if isinstance(orders, list) else (orders or {}).get("orders") or []
    candidates = [
        r for r in rows
        if isinstance(r, dict) and str(r.get("order_group_id", "")) == order_group_id
    ]
    if not candidates and isinstance(rows, list):
        candidates = [r for r in rows if isinstance(r, dict)]
    if not candidates:
        raise Blocked(f"No booking found for order_group_id {order_group_id}.")
    candidates.sort(key=lambda r: str(r.get("delivery_time") or ""), reverse=True)
    return candidates


def _booking_label(b: dict[str, Any]) -> dict[str, str]:
    return {
        "booking_id": str(b.get("booking_id") or b.get("id") or ""),
        "test_name": str(b.get("test_name") or ""),
        "status": str(b.get("status") or ""),
        "delivery_time": str(b.get("delivery_time") or ""),
    }


def _pick_patient(patient_details: Any, patient_id: str) -> dict[str, Any]:
    """Select the booking's patient, never merely the first one.

    get_user_details returns every patient on the account; comparing against a
    sibling's record is the exact failure class these tickets are about.
    """
    rows = [p for p in (patient_details or []) if isinstance(p, dict)]
    for p in rows:
        if str(p.get("id", "")) == patient_id:
            return p
    raise Blocked(
        f"Patient {patient_id} from the booking is not in the account's patient "
        f"list ({len(rows)} returned) — refusing to compare against a different patient."
    )


def gather(
    client: HraClient, order_group_id: str, *, on_progress: ProgressFn | None = None
) -> dict[str, Any]:
    """Walk order_group_id -> patient record + report identity + gender history."""
    say = on_progress or (lambda _m: None)
    trail: list[str] = []

    def step(msg: str) -> None:
        trail.append(msg)
        say(msg)

    try:
        step(f"Resolving order {order_group_id}")
        details = client.user_details(order_group_id)
        user_id = str((details or {}).get("user_id") or "")
        if not user_id:
            raise Blocked(f"No user_id for order_group_id {order_group_id}.")
        step(f"user_id {user_id}")

        bookings = _pick_bookings(client.orders(user_id, order_group_id), order_group_id)
        step(f"{len(bookings)} booking(s) on this order")

        patient_id = next(
            (str(b.get("patient_id")) for b in bookings if b.get("patient_id")), ""
        )
        if not patient_id:
            raise Blocked("No booking on this order carries a patient_id.")
        patient = _pick_patient((details or {}).get("patient_details"), patient_id)

        # Walk the bookings until one yields a parseable report; remember the
        # ones that had none, because a booking with no digitisation record is
        # itself a finding.
        identity: dict[str, str] | None = None
        booking_id = ""
        no_report: list[dict[str, str]] = []
        for b in bookings:
            bid = str(b.get("booking_id") or b.get("id") or "")
            if not bid:
                continue
            try:
                url = (client.json_report_url(bid, order_group_id) or {}).get("json_url")
                if not url:
                    raise HraError("no json_url in the response")
                candidate = read_report_identity(client.fetch_json_report(url))
                if candidate["format"] == "unknown":
                    raise HraError("the report has neither the new nor the old shape")
            except HraError as exc:
                label = _booking_label(b)
                label["why"] = str(exc)[:200]
                no_report.append(label)
                step(f"{bid}: no usable report")
                continue
            identity = candidate
            booking_id = bid
            step(f"{bid}: report parsed ({candidate['format']} format)")
            break

        if identity is None:
            raise Blocked(
                "No booking on this order has a digitisation record, so there is "
                "no report to compare against. Bookings tried: "
                + ", ".join(b["booking_id"] for b in no_report)
                + ".",
                bookings_without_report=no_report,
            )

        step("Reading patient version history")
        versions = client.patient_versions(patient_id)
        created_as, changes = gender_history(versions)
        name_created_as, name_changes = name_history(versions)

    except Blocked as exc:
        exc.trail = exc.trail or trail
        raise
    except HraAuthError as exc:
        raise Blocked(str(exc), auth=True, trail=trail) from exc
    except HraBlockedError as exc:
        # A network refusal is not a credential problem and must not be
        # reported as one.
        raise Blocked(str(exc), trail=trail) from exc
    except HraError as exc:
        raise Blocked(str(exc), trail=trail) from exc

    return {
        "user_id": user_id,
        "patient_id": patient_id,
        "booking_id": booking_id,
        "bookings_without_report": no_report,
        "booking_count": len(bookings),
        "patient": patient,
        "report": identity,
        "gender_created_as": created_as,
        "gender_changes": changes,
        "name_created_as": name_created_as,
        "name_changes": name_changes,
        "trail": trail,
    }


# --- the two comparators ---------------------------------------------------


def compare_name(report_name: Any, record_name: Any) -> Comparison:
    rn, cn = normalize_name(report_name), normalize_name(record_name)
    return Comparison("name", str(report_name or ""), str(record_name or ""), rn, cn, rn == cn)


def compare_gender(report_gender: Any, record_gender: Any) -> Comparison:
    rg, cg = normalize_gender(report_gender), normalize_gender(record_gender)
    # An unrecognised value on either side is not a match, and not a silent one.
    matches = bool(rg) and bool(cg) and rg == cg
    return Comparison(
        "gender", str(report_gender or ""), str(record_gender or ""), rg, cg, matches
    )


# What each comparison is called, for a runbook to name in its `field`. A new
# field means a new entry here: what counts as equal is a judgement about the
# data, so it stays in code where it is tested.
COMPARATORS = {
    "name": compare_name,
    "gender": compare_gender,
}

CHECK_LABELS = {r.id: r.label for r in RUNBOOKS}


def _detail(rb: Runbook, cmp: Comparison) -> str:
    template = rb.detail_match if cmp.matches else rb.detail_mismatch
    return template.format(
        report_value=cmp.report_value,
        record_value=cmp.record_value,
        report_normalized=cmp.report_normalized,
        record_normalized=cmp.record_normalized,
    )


def build_checks(comparisons: dict[str, Comparison]) -> list[Check]:
    """Every runbook condition, always all of them reported.

    Driven by runbooks.RUNBOOKS rather than written out, so adding a condition
    is an entry in that file rather than another branch here.
    """
    checks = []
    for rb in RUNBOOKS:
        cmp = comparisons.get(rb.field)
        if cmp is None:
            continue
        checks.append(
            Check(
                runbook=rb.id,
                label=rb.label,
                passed=cmp.matches,
                detail=_detail(rb, cmp),
            )
        )
    return checks


def first_failing(comparisons: dict[str, Comparison]) -> Runbook | None:
    """The runbook that decides the verdict: the first one whose check fails."""
    for rb in RUNBOOKS:
        cmp = comparisons.get(rb.field)
        if cmp is not None and not cmp.matches:
            return rb
    return None


def resolution_for(
    rb: Runbook, cmp: Comparison, *, patient_id: str, order_group_id: str
) -> list[str]:
    """The steps to fix it, with this order's identifiers filled in."""
    values = {
        "patient_id": patient_id,
        "order_group_id": order_group_id,
        "report_value": cmp.report_value,
        "record_value": cmp.record_value,
        "report_normalized": cmp.report_normalized,
        "record_normalized": cmp.record_normalized,
        # Two field-named keys, because the right one differs per runbook and
        # getting it wrong writes a bad instruction. `report_name` must be
        # verbatim -- the step says to type the name EXACTLY, and the
        # normalized form is lowercased. `normalized_gender` is the opposite:
        # "m" is what the dashboard expects, not whatever the report wrote.
        f"report_{rb.field}": cmp.report_value,
        f"normalized_{rb.field}": cmp.report_normalized or cmp.report_value,
    }
    return [step.format(**values) for step in rb.resolution]


# Identifiers are backticked so the chat UI renders them as code. The CLI
# strips the marks when printing, via strip_code_marks below.
def strip_code_marks(text: str) -> str:
    """Drop markdown marks for plain-text surfaces such as the terminal.

    The model writes for the web UI, which renders backticks and bold. The
    terminal renders neither, so the marks would show up as punctuation.
    """
    text = text.replace("`", "").replace("**", "")
    # Paired single asterisks only: a lone one is more likely to be literal.
    return re.sub(r"\*([^*\n]+)\*", r"\1", text)


def diagnose_order(
    cfg: Config,
    order_group_id: str,
    *,
    client: HraClient | None = None,
    on_progress: ProgressFn | None = None,
    # Off by default: the verdict is the product, and a library call should
    # not silently spend a model call. Callers that want prose ask for it.
    with_reason: bool = False,
    subject: str = "",
) -> Diagnosis:
    """Diagnose one order. Returns a verdict, never raises for a data problem."""
    owned = client is None
    client = client or HraClient(cfg.hra)
    try:
        try:
            facts = gather(client, order_group_id, on_progress=on_progress)
        except Blocked as exc:
            return Diagnosis(
                order_group_id=order_group_id,
                verdict=INDETERMINATE,
                blocked_because=exc.why,
                auth_expired=exc.auth,
                trail=exc.trail,
                bookings_without_report=exc.bookings_without_report,
            )

        patient = facts["patient"]
        report = facts["report"]
        name_cmp = compare_name(report["name"], patient.get("name"))
        gender_cmp = compare_gender(report["gender"], patient.get("gender"))
        comparisons = {"name": name_cmp, "gender": gender_cmp}

        d = Diagnosis(
            order_group_id=order_group_id,
            verdict=MATCH,
            checks=build_checks(comparisons),
            comparisons=[name_cmp, gender_cmp],
            gender_created_as=facts["gender_created_as"],
            gender_changes=facts["gender_changes"],
            name_created_as=facts["name_created_as"],
            name_changes=facts["name_changes"],
            booking_count=facts["booking_count"],
            bookings_without_report=facts["bookings_without_report"],
            user_id=facts["user_id"],
            patient_id=facts["patient_id"],
            booking_id=facts["booking_id"],
            report_format=report["format"],
            trail=facts["trail"],
        )

        # Name is checked first: it is the documented cause of a missing doctor
        # summary, and it is the stricter comparison of the two.
        # (verdict assignment below; reason is written afterwards)
        fired = first_failing(comparisons)
        if fired is not None:
            d.verdict = MISMATCH
            d.runbook = fired.id
            d.resolution = resolution_for(
                fired,
                comparisons[fired.field],
                patient_id=d.patient_id,
                order_group_id=order_group_id,
            )

        if with_reason:
            say = on_progress or (lambda _m: None)
            say("Writing the reason")
            try:
                d.reason = write_reason(cfg, d, subject)
            except Exception as exc:  # noqa: BLE001 - a verdict without prose still stands
                d.reason = ""
                d.trail.append(f"reason unavailable ({type(exc).__name__})")
        return d
    finally:
        if owned:
            client.close()


# --- thread-aware diagnosis -------------------------------------------------
#
# The runbook checks are mandatory and deterministic. The two model calls
# around them are narrow: one decides whether the thread is even reporting an
# open problem, and one looks for a problem no runbook covers -- and only when
# every check has already passed.

# The closure verdict is decided from the thread alone, before any API call and
# independently of the runbook checks. A ticket can be closed with a failed
# check (someone fixed it by hand) or open with every check passing.








def _thread_for_model(thread: Any, max_chars: int = 6000) -> str:
    """The thread as evidence, fenced and redacted."""
    from .redact import redact

    parts = ["=== BEGIN UNTRUSTED EMAIL THREAD ==="]
    budget = max_chars
    for i, msg in enumerate(getattr(thread, "messages", []), 1):
        body = redact(msg.body_text or msg.snippet or "")
        if len(body) > budget:
            body = body[:budget] + "…[truncated]"
        budget -= len(body)
        parts += [
            f"--- message {i} ---",
            f"date: {msg.date.isoformat() if msg.date else 'unknown'}",
            f"from: {redact(msg.sender)}",
            f"subject: {redact(msg.subject)}",
            body,
            "",
        ]
        if budget <= 0:
            parts.append("[remaining messages omitted]")
            break
    parts.append("=== END UNTRUSTED EMAIL THREAD ===")
    return "\n".join(parts)


@dataclass
class ThreadState:
    """Whether the thread has been closed by a reply, read from the mail alone."""

    closed: bool = False
    confidence: float = 0.0
    closed_by: str = ""
    closed_at: str = ""
    reason: str = ""


def read_thread_state(cfg: Config, thread: Any) -> ThreadState:
    """Closed or open, from the thread only.

    Closure needs a resolving reply AND confidence in it. Anything less stays
    open: marking a live ticket closed is the expensive mistake, since nobody
    looks at it again.
    """
    prompt = "\n".join(
        [
            f"Subject: {getattr(thread, 'subject', '')}",
            f"Messages in thread: {len(getattr(thread, 'messages', []))}",
            "",
            _thread_for_model(thread, 5000),
            "",
            "Return the JSON object now.",
        ]
    )
    try:
        from .summarizer import _parse_json_object

        data = _parse_json_object(_complete_json(cfg, prompt_for("diagnosis.closure", CLOSURE_SYSTEM_PROMPT), prompt))
    except Exception:  # noqa: BLE001 - an unreadable answer means "open"
        return ThreadState(closed=False, reason="")

    # Same decision, same threshold, as the summarizer's read of a thread.
    closed, confidence, reason = decide_closure(
        bool(data.get("closed", False)),
        data.get("confidence", 0),
        str(data.get("reason") or ""),
    )

    return ThreadState(
        closed=closed,
        confidence=confidence,
        closed_by=str(data.get("closed_by") or "").strip() if closed else "",
        closed_at=str(data.get("closed_at") or "").strip() if closed else "",
        reason=reason,
    )


def build_findings_prompt(
    thread: Any, d: Diagnosis, order_data: dict[str, Any] | None = None
) -> str:
    lines = [
        f"order_group_id: {d.order_group_id}",
        f"patient_id: {d.patient_id}",
        f"booking_id: {d.booking_id}",
        "",
        "Runbook checks (all passed, decided in code):",
    ]
    for c in d.checks:
        lines.append(f"- {c.label}: PASSED — {c.detail}")

    for label, created, changes in (
        ("Name history", d.name_created_as, d.name_changes),
        ("Gender history", d.gender_created_as, d.gender_changes),
    ):
        if not (created or changes):
            continue
        lines += ["", f"{label}:", f"- at creation: {created or 'unknown'}"]
        for ch in changes:
            actor = f" by actor {ch.actor_id}" if ch.actor_id else ""
            lines.append(
                f"- {ch.from_value or '?'} → {ch.to_value or '?'} "
                f"at {ch.changed_at or 'unknown'}{actor}"
            )

    if d.bookings_without_report:
        lines += ["", "Bookings with NO digitisation record (no JSON report):"]
        for b in d.bookings_without_report:
            lines.append(
                f"- {b.get('booking_id')} · {b.get('test_name')} · "
                f"status {b.get('status')} · delivered {b.get('delivery_time')}"
            )
        lines.append(
            "A booking with no digitisation record cannot produce a smart report. "
            "If one of these is the package or profile booking, say so plainly."
        )

    if order_data:
        import json as _json

        blob = _json.dumps(order_data, indent=1, default=str)
        lines += ["", "Order data from the admin APIs:", blob[:7000]]

    lines += ["", _thread_for_model(thread), "", "Write your findings now."]
    return "\n".join(lines)


def find_other_findings(
    cfg: Config, thread: Any, d: Diagnosis, order_data: dict[str, Any] | None = None
) -> str:
    """Open analysis, run only once every runbook check has passed."""
    prompt = build_findings_prompt(thread, d, order_data)
    try:
        return "".join(_stream_reason(cfg, prompt_for("diagnosis.findings", FINDINGS_SYSTEM_PROMPT), prompt)).strip()
    except Exception:  # noqa: BLE001 - findings are additive
        return ""


def _stream_reason(cfg: Config, system: str, prompt: str) -> Any:
    backend = cfg.summarizer.backend
    if backend == "anthropic_api":
        from .anthropic_backend import stream_answer

        return stream_answer(cfg, system, prompt)
    if backend == "local":
        from .local_backend import stream_answer

        return stream_answer(cfg, system, prompt)

    from .streaming import stream_claude

    return stream_claude(
        prompt, system, model=cfg.summarizer.model,
        timeout_seconds=cfg.summarizer.timeout_seconds,
    )


def _complete_json(cfg: Config, system: str, prompt: str) -> str:
    backend = cfg.summarizer.backend
    if backend == "anthropic_api":
        from .anthropic_backend import complete_json

        return complete_json(cfg, system, prompt)
    if backend == "local":
        from .local_backend import complete_json

        return complete_json(cfg, system, prompt)
    return "".join(_stream_reason(cfg, system, prompt))


def diagnose_thread(
    cfg: Config,
    thread: Any,
    *,
    client: HraClient | None = None,
    order_group_id: str = "",
    on_progress: ProgressFn | None = None,
) -> Diagnosis:
    """Diagnose the order behind an email thread. Blocking; see the stream."""
    say = on_progress or (lambda _m: None)
    result: Diagnosis | None = None
    for kind, payload in diagnose_thread_stream(
        cfg, thread, client=client, order_group_id=order_group_id
    ):
        if kind == "status":
            say(payload)
        elif kind == "result":
            result = payload
    assert result is not None
    return result


def diagnose_thread_stream(
    cfg: Config,
    thread: Any,
    *,
    client: HraClient | None = None,
    order_group_id: str = "",
) -> Iterator[tuple[str, Any]]:
    """Diagnose the order behind an email thread, reporting as it goes.

    Order of operations matters: triage the thread, run the mandatory runbook
    checks against live data, and only reach for an open analysis when every
    check has passed. That way the model is never asked to second-guess a
    verdict the comparison already settled.

    Events, in the order they can be shown: ("status", str) progress,
    ("state", dict) the closed/open read of the thread, ("checks", list) the
    runbook results, ("delta", str) prose as the model writes it, and
    ("result", Diagnosis). The verdict and the checks are settled in code
    before any prose is streamed, so what arrives late is only the narration.
    """
    from .order_qa import find_order_ids

    subject = getattr(thread, "subject", "") or ""

    yield ("status", "Reading the thread")
    state = read_thread_state(cfg, thread)
    yield ("status", "Thread looks " + ("closed" if state.closed else "open"))
    yield (
        "state",
        {
            "thread_closed": state.closed,
            "closure_confidence": state.confidence,
            "closure_reason": state.reason,
            "closed_by": state.closed_by,
            "closed_at": state.closed_at,
        },
    )

    ogid = (order_group_id or "").strip().upper()
    if not ogid:
        found = [
            o
            for o in find_order_ids(
                subject, *[m.body_text or m.snippet or "" for m in thread.messages]
            )
            if o.startswith("PO")
        ]
        ogid = found[0] if found else ""

    if not ogid:
        yield (
            "result",
            Diagnosis(
                order_group_id="",
                verdict=INDETERMINATE,
                thread_closed=state.closed,
                closure_confidence=state.confidence,
                closure_reason=state.reason,
                closed_by=state.closed_by,
                closed_at=state.closed_at,
                thread_subject=subject,
                blocked_because="No order group id (PO…) anywhere in this thread, so "
                "the runbook checks cannot be run. Reply with the order id and I can.",
            ),
        )
        return

    yield ("status", f"Checking runbooks against {ogid}")
    # The HRA calls are blocking, so their trail is collected and relayed once
    # the checks are in rather than interleaved with them.
    trail: list[str] = []
    d = diagnose_order(
        cfg, ogid, client=client, on_progress=trail.append,
        with_reason=False, subject=subject,
    )
    for line in trail:
        yield ("status", line)
    d.thread_closed = state.closed
    d.closure_confidence = state.confidence
    d.closure_reason = state.reason
    d.closed_by = state.closed_by
    d.closed_at = state.closed_at
    d.thread_subject = subject

    if d.checks:
        yield ("checks", [asdict(c) for c in d.checks])

    if d.verdict == MISMATCH:
        yield ("status", "A runbook fired — writing the reason")
        text, failure = yield from _stream_prose(
            cfg, prompt_for("diagnosis.reason", REASON_SYSTEM_PROMPT),
            build_reason_prompt(d, subject)
        )
        d.reason = text
        if failure and not text:
            d.trail.append(f"reason unavailable ({failure})")
    elif d.verdict == MATCH or d.bookings_without_report:
        # MATCH: every runbook was ruled out, so look for something else.
        # bookings_without_report: the checks could not run, but we know why
        # and that is itself a finding.
        yield (
            "status",
            "All checks passed — looking for anything else"
            if d.verdict == MATCH
            else "No report to check against — analysing what we do have",
        )
        text, _failure = yield from _stream_prose(
            cfg, prompt_for("diagnosis.findings", FINDINGS_SYSTEM_PROMPT),
            build_findings_prompt(thread, d)
        )
        d.other_findings = text
        yield from _run_followup(cfg, d, text, client)

    yield ("result", d)


def _run_followup(
    cfg: Config, d: Diagnosis, findings: str, client: Any
) -> Iterator[tuple[str, Any]]:
    """Try to answer the further check the findings just named.

    The findings almost always end with "the next check would be X". Some of
    those X are a documented read away, so run it rather than handing the
    engineer a to-do -- but only ever a read, and only ever about an id this
    diagnosis established.
    """
    if not findings.strip() or client is None:
        return

    yield ("status", "Looking for a read that answers the further check")
    question, calls = plan_followup_reads(cfg, d, findings)
    d.followup_question = question
    if not calls:
        # Normal: plenty of further checks need a human, a dashboard, or data
        # these APIs do not expose. Saying so beats a call that answers
        # something else.
        if question:
            d.followup_calls = [{
                "tool": "", "params": "",
                "outcome": "no documented read-only API can answer this",
            }]
            yield ("followup", {"question": question, "calls": d.followup_calls})
        return

    running = [
        {
            "tool": c["tool"],
            "params": " · ".join(f"{k}={v}" for k, v in (c["params"] or {}).items()),
            "outcome": "running",
        }
        for c in calls
    ]
    yield ("followup", {"question": question, "calls": running})
    for call in calls:
        yield ("status", f"Running {call['tool']}")
    tried, results = run_followup_reads(client, calls)
    # The follow-up reads can return report URLs, and the findings quote what
    # they are given. A raw private URL 403s when clicked, so it is signed
    # before the model ever sees it.
    _sign_urls_in_place(client, results)
    d.followup_calls = tried
    yield ("followup", {"question": question, "calls": tried})

    from .order_qa import allowed_urls_in

    text, failure = yield from _stream_prose(
        cfg,
        prompt_for("diagnosis.followup", FOLLOWUP_SYSTEM_PROMPT),
        build_followup_prompt(question, tried, results),
        allowed_urls_in(results),
    )
    d.followup_findings = text
    if failure and not text:
        d.trail.append(f"further check unavailable ({failure})")


MAX_FOLLOWUP_CALLS = 2
# Reads that answer a "further check". The URL signer is excluded: it needs a
# URL rather than an id, and signing one settles nothing.
_FOLLOWUP_SKIP = frozenset({"get_presigned_url_for_private_content"})


def established_ids(d: Diagnosis) -> dict[str, str]:
    """The ids this diagnosis actually established, by parameter name.

    The follow-up may only ask about these. It is the whole safety property of
    the step: an id the model invented, or lifted out of the untrusted email,
    cannot be turned into a request for someone else's records.
    """
    out: dict[str, str] = {}
    for key, value in (
        ("order_group_id", d.order_group_id),
        ("user_id", d.user_id),
        ("patient_id", d.patient_id),
        ("booking_id", d.booking_id),
    ):
        if str(value or "").strip():
            out[key] = str(value).strip()
    return out


def _allowed_values(d: Diagnosis) -> set[str]:
    values = {v.casefold() for v in established_ids(d).values()}
    # Bookings on this order that had no report are established too, and are
    # exactly what a "check the other bookings" follow-up wants.
    for b in d.bookings_without_report:
        bid = str(b.get("booking_id") or "").strip()
        if bid:
            values.add(bid.casefold())
    return values


def plan_followup_reads(
    cfg: Config, d: Diagnosis, findings: str
) -> tuple[str, list[dict[str, Any]]]:
    """Ask which documented read would answer the further check. May be none."""
    from .summarizer import _parse_json_object
    from .tools.registry import (
        missing_params,
        missing_prerequisites,
        read_only_tools,
        required_params,
    )

    if not findings.strip():
        return "", []

    known = established_ids(d)
    if not known:
        return "", []

    # gather() has already walked the chain -- user details, then the order's
    # bookings, then the report -- so a tool whose prerequisites are those
    # calls has its ids available in `known`.
    chain_done = ("get_user_details_for_an_order", "fetch_all_orders_of_a_user")
    tools = [
        t for t in read_only_tools()
        if t.name not in _FOLLOWUP_SKIP
        and not missing_prerequisites(t.name, chain_done)
    ]
    prompt = "\n".join(
        [
            "Diagnosis findings (the further check is in here):",
            findings,
            "",
            "Available read-only tools:",
            *[t.describe() for t in tools],
            "",
            "Ids already established (the only ones you may use):",
            json.dumps(known, indent=1),
            "",
            "Data the diagnosis already has: the patient record, this order's "
            "bookings, the digitised report for the booking above, and the "
            "patient's full version history.",
            "",
            "Return the JSON object now.",
        ]
    )
    try:
        data = _parse_json_object(_complete_json(cfg, prompt_for("diagnosis.followup_plan", FOLLOWUP_PLAN_SYSTEM_PROMPT), prompt))
    except Exception:  # noqa: BLE001 - no plan is a normal outcome
        return "", []

    question = str(data.get("question") or "").strip()
    raw = data.get("calls")
    if not isinstance(raw, list):
        return question, []

    by_name = {t.name: t for t in tools}
    allowed = _allowed_values(d)
    calls: list[dict[str, Any]] = []
    for entry in raw[:MAX_FOLLOWUP_CALLS]:
        if not isinstance(entry, dict):
            continue
        spec = by_name.get(str(entry.get("tool") or ""))
        params = entry.get("params")
        if spec is None or not isinstance(params, dict):
            continue
        clean = {k: str(v).strip() for k, v in params.items() if str(v or "").strip()}
        # Every value has to be an id we established. Anything else is either
        # invented or came out of the email, and neither is ours to query.
        if any(v.casefold() not in allowed for v in clean.values()):
            continue
        # Fill what the chain already established rather than sending a call
        # that answers a wider question than it was asked. bookings+parameters
        # without a patient_id and booking_id answers for the whole order, and
        # the reply then reads as if the booking's data were absent.
        for name in required_params(spec.name, spec):
            if not clean.get(name) and known.get(name):
                clean[name] = known[name]
        still_missing = missing_params(spec.name, clean, spec)
        if still_missing:
            continue
        calls.append({"tool": spec.name, "params": clean})
    return question, calls


def run_followup_reads(
    client: Any, calls: list[dict[str, Any]]
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Execute the planned reads. Returns (what was tried, what came back)."""
    from .tools.executor import ToolError, call_tool
    from .tools.registry import read_only_tools

    by_name = {t.name: t for t in read_only_tools()}
    tried: list[dict[str, str]] = []
    results: dict[str, Any] = {}
    for call in calls:
        name = str(call.get("tool") or "")
        params = call.get("params") or {}
        shown = " · ".join(f"{k}={v}" for k, v in params.items())
        spec = by_name.get(name)
        if spec is None:
            tried.append({"tool": name, "params": shown, "outcome": "not a documented read"})
            continue
        try:
            payload = call_tool(client, spec, params)
        except (ToolError, HraError) as exc:
            tried.append({"tool": name, "params": shown, "outcome": f"failed: {exc}"})
            continue
        n = len(payload) if isinstance(payload, (list, dict)) else 0
        # Recorded even when empty: "the call ran and came back with nothing"
        # is a different fact from "the call was never made", and the model
        # needs to be able to tell them apart.
        results[name] = payload
        tried.append({
            "tool": name,
            "params": shown,
            "outcome": "returned nothing" if not payload else f"returned {n} item(s)",
        })
    return tried, results


# One call per diagnosis, so this can be generous -- but a digitised parameter
# list still overruns it, which is why the cut is labelled rather than hidden.
FOLLOWUP_DATA_BUDGET = 14000


def _sign_urls_in_place(client: Any, results: dict[str, Any]) -> None:
    """Replace private URLs in these payloads with signed ones, in place."""
    from .order_qa import Fetched, sign_private_urls

    holder = Fetched(order_group_id="")
    holder.results = results
    try:
        sign_private_urls(client, holder)
    except Exception:  # noqa: BLE001 - a link is not the verdict
        return

    if not holder.signed:
        return

    def swap(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: swap(v) for k, v in node.items()}
        if isinstance(node, list):
            return [swap(v) for v in node]
        if isinstance(node, str):
            return holder.signed.get(node, node)
        return node

    for name in list(results):
        results[name] = swap(results[name])


def build_followup_prompt(
    question: str, tried: list[dict[str, str]], results: dict[str, Any]
) -> str:
    lines = [f"Further check: {question or 'see the findings'}", "", "Calls made:"]
    for t in tried:
        lines.append(f"- {t['tool']}({t['params']}) -> {t['outcome']}")
    lines += ["", "Data returned:"]
    if not results:
        lines.append("(nothing)")
    else:
        # A share each, not one slice over the lot. A single fat payload -- the
        # digitised lab parameters, typically -- would otherwise eat the whole
        # budget and the second call's data would simply be absent, which the
        # model can only read as "it was not provided".
        share = max(1500, FOLLOWUP_DATA_BUDGET // len(results))
        for name, payload in results.items():
            blob = json.dumps(payload, indent=1, default=str)
            lines += ["", f"--- {name} ---", blob[:share]]
            if len(blob) > share:
                lines.append(
                    f"[cut off here: {share} of {len(blob)} characters shown. "
                    "Do not treat what is missing as absent from the record.]"
                )
    lines += ["", "Write your bullets now."]
    return "\n".join(lines)


def _stream_prose(
    cfg: Config, system: str, prompt: str, allowed: dict[str, str] | None = None
) -> Any:
    """Yield ("delta", chunk) as the model narrates; return (text, failure).

    A failure mid-sentence keeps whatever arrived: the verdict and the checks
    are already decided, so a truncated paragraph is worth more than nothing,
    and the caller records that it was cut short.

    `allowed` is the URLs the underlying data actually returned. When the
    narration is written over fetched payloads, pass it: a model asked to
    quote one report link will write a second one that follows the same shape
    (measured on gemma3:latest), and the UI makes every URL clickable.
    """
    from .order_qa import scrub_urls

    chunks: list[str] = []
    failure = ""
    try:
        source = _stream_reason(cfg, system, prompt)
        if allowed is not None:
            source = scrub_urls(source, allowed)
        for chunk in source:
            chunks.append(chunk)
            yield ("delta", chunk)
    except Exception as exc:  # noqa: BLE001 - narration is not the verdict
        failure = type(exc).__name__
    return "".join(chunks).strip(), failure
