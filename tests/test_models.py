"""The structured read of a thread, and the closure verdict on it.

The threshold is applied in code, never by the model, and both readers of a
thread go through the same helper -- so these pin the behaviour rather than
the wording of a prompt.
"""

from __future__ import annotations

# --- the closure read on a summary -----------------------------------------


def _thread_for_closure():
    from datetime import datetime, timezone

    from oncallbot.models import EmailMessage, EmailThread

    m = EmailMessage(
        id="m1", thread_id="t1",
        date=datetime(2026, 9, 8, tzinfo=timezone.utc),
        sender="Asha <asha@x.com>", to="hr@1mg.com", cc="", subject="s",
        body_text="b", snippet="b",
    )
    return EmailThread(id="t1", subject="s", messages=[m])


def _summary(**over):
    from oncallbot.models import IssueSummary

    data = {"summary": "s", "issue": "i", "category": "other", "severity": "p2"}
    data.update(over)
    return IssueSummary.from_model_output(data, _thread_for_closure(), "sonnet")


def test_a_confident_closure_is_closed():
    s = _summary(closed=True, closure_confidence=0.9, closed_by="Support",
                 closed_at="2026-09-09", closure_reason="report attached")
    assert s.closed is True
    assert s.closed_by == "Support"
    assert s.closed_at == "2026-09-09"
    assert s.closure_reason == "report attached"


def test_a_low_confidence_closure_stays_open():
    """Marking a live ticket closed is the expensive mistake."""
    s = _summary(closed=True, closure_confidence=0.6, closure_reason="maybe fixed")
    assert s.closed is False
    assert "not clearly enough" in s.closure_reason
    assert "0.60" in s.closure_reason
    assert s.closed_by == "", "nobody closed it, so nobody is credited"


def test_an_open_thread_carries_no_closer():
    s = _summary(closed=False, closure_confidence=0.1, closed_by="Support",
                 closed_at="2026-09-09")
    assert s.closed is False
    assert (s.closed_by, s.closed_at) == ("", "")


def test_a_missing_closure_read_is_not_open():
    """A summary cached before this existed must claim nothing."""
    s = _summary()
    assert s.closed is None
    assert s.to_dict()["closed"] is None


def test_the_threshold_is_the_same_one_the_diagnosis_uses():
    from oncallbot.diagnose import CLOSURE_CONFIDENCE_THRESHOLD as a
    from oncallbot.models import CLOSURE_CONFIDENCE_THRESHOLD as b

    assert a is b


def test_decide_closure_clamps_a_nonsense_confidence():
    from oncallbot.models import decide_closure

    assert decide_closure(True, 5.0)[0] is True      # clamped to 1.0
    assert decide_closure(True, "junk") == (False, 0.0, "A reply may have "
                                            "resolved this, but not clearly "
                                            "enough to call it closed "
                                            "(confidence 0.00)")
    assert decide_closure(False, 0.99)[0] is False


def test_the_summary_prompt_asks_for_the_closure_read():
    from oncallbot.prompts import SYSTEM_PROMPT

    assert '"closed": false' in SYSTEM_PROMPT
    assert "closure_confidence" in SYSTEM_PROMPT
    assert "is NOT closure" in SYSTEM_PROMPT
    assert "unanswered complaint" in SYSTEM_PROMPT


def test_the_api_schema_requires_the_closure_fields():
    from oncallbot.anthropic_backend import SUMMARY_SCHEMA

    for f in ("closed", "closure_confidence", "closed_by", "closed_at",
              "closure_reason"):
        assert f in SUMMARY_SCHEMA["properties"], f
        assert f in SUMMARY_SCHEMA["required"], f


def test_the_local_schema_inherits_them():
    from oncallbot.local_backend import _summary_schema

    assert "closed" in _summary_schema(["other"])["properties"]
