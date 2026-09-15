"""Runbooks are data in runbooks.py, not branches in diagnose.py.

The point of the split is that the team can add a condition without touching
the diagnosis engine. These tests hold that promise: a third runbook is
registered here and has to be checked, reported and able to decide a verdict
without a line changing anywhere else.
"""

import pytest

from oncallbot import diagnose as dg
from oncallbot.runbooks import RUNBOOKS, Runbook, by_field, by_id


def cmp_of(field, report, record, matches, normalized=""):
    return dg.Comparison(
        field=field,
        report_value=report,
        record_value=record,
        report_normalized=normalized or report.lower(),
        record_normalized=record.lower(),
        matches=matches,
    )


# --- what ships ------------------------------------------------------------


def test_the_shipped_runbooks_are_registered():
    assert [r.id for r in RUNBOOKS] == ["name_mismatch", "gender_mismatch"]
    assert by_id("name_mismatch").field == "name"
    assert by_field("gender").id == "gender_mismatch"


def test_every_runbook_has_a_comparator():
    """A runbook naming a field nothing can compare would never fire."""
    for rb in RUNBOOKS:
        assert rb.field in dg.COMPARATORS, rb.id


def test_order_decides_which_verdict_wins():
    """Name is checked first: the documented cause, and the stricter test."""
    both_wrong = {
        "name": cmp_of("name", "A", "B", False),
        "gender": cmp_of("gender", "m", "f", False),
    }
    assert dg.first_failing(both_wrong).id == "name_mismatch"


def test_every_condition_is_reported_pass_or_fail():
    checks = dg.build_checks({
        "name": cmp_of("name", "A", "A", True),
        "gender": cmp_of("gender", "m", "f", False),
    })
    assert [c.runbook for c in checks] == ["name_mismatch", "gender_mismatch"]
    assert [c.passed for c in checks] == [True, False]


# --- the instructions ------------------------------------------------------


def test_the_name_step_is_verbatim_not_normalized():
    """The step says to type the name EXACTLY; the normalized form is
    lowercased, so rendering that would have them enter the wrong value."""
    rb = by_id("name_mismatch")
    steps = dg.resolution_for(
        rb, cmp_of("name", "Mr. Ravi Kumar", "Ravi Kumar", False),
        patient_id="p-1", order_group_id="PO1-1",
    )
    assert "`Mr. Ravi Kumar`" in steps[1]


def test_the_gender_step_uses_the_normalized_value():
    """`m` is what the dashboard expects, not whatever the report wrote."""
    rb = by_id("gender_mismatch")
    steps = dg.resolution_for(
        rb, cmp_of("gender", "Male", "f", False, normalized="m"),
        patient_id="p-1", order_group_id="PO1-1",
    )
    assert "`m`" in steps[1]


def test_the_order_identifiers_reach_every_step():
    rb = by_id("name_mismatch")
    steps = dg.resolution_for(
        rb, cmp_of("name", "A", "B", False),
        patient_id="p-42", order_group_id="PO9-9",
    )
    assert "`p-42`" in steps[0]
    assert "`PO9-9`" in steps[2]


def test_a_step_with_an_unknown_placeholder_raises():
    """Better than rendering a half-written instruction to an engineer."""
    rb = Runbook(
        id="broken", label="x", field="name",
        detail_mismatch="", detail_match="",
        resolution=("set it to `{nonexistent_key}`",),
    )
    with pytest.raises(KeyError):
        dg.resolution_for(rb, cmp_of("name", "A", "B", False),
                          patient_id="p-1", order_group_id="PO1-1")


# --- adding one ------------------------------------------------------------


def test_a_new_runbook_needs_no_change_to_the_engine(monkeypatch):
    """The whole point of the split: register it, and it is checked, reported
    and able to decide a verdict without diagnose.py knowing it exists."""
    dob = Runbook(
        id="dob_mismatch",
        label="Report date of birth matches the patient record",
        field="dob",
        detail_mismatch="report {report_value!r} vs record {record_value!r}",
        detail_match="both {record_value!r}",
        resolution=('Set the date of birth to `{report_dob}`.',),
    )
    monkeypatch.setattr(dg, "RUNBOOKS", (*RUNBOOKS, dob))
    monkeypatch.setitem(dg.COMPARATORS, "dob", lambda a, b: None)

    comparisons = {
        "name": cmp_of("name", "A", "A", True),
        "gender": cmp_of("gender", "m", "m", True),
        "dob": cmp_of("dob", "1990-01-01", "1991-01-01", False),
    }
    checks = dg.build_checks(comparisons)
    assert [c.runbook for c in checks][-1] == "dob_mismatch"
    assert checks[-1].passed is False
    assert "1990-01-01" in checks[-1].detail

    fired = dg.first_failing(comparisons)
    assert fired.id == "dob_mismatch"
    steps = dg.resolution_for(fired, comparisons["dob"],
                              patient_id="p-1", order_group_id="PO1-1")
    assert steps == ["Set the date of birth to `1990-01-01`."]
