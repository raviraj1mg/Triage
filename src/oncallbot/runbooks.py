"""The runbook conditions phase-2 diagnosis checks — one entry each.

This is the file to edit when the team writes a new runbook. Every condition
is checked on every diagnosis and every result is reported, pass or fail, so
a reader can see what was examined rather than only what fired.

Order matters: the first entry whose condition FAILS becomes the verdict, and
its resolution is what the engineer is told to do. Name is first because it is
the documented cause of a missing doctor summary and the stricter of the two
comparisons.

--- adding a runbook -------------------------------------------------------

If it compares two values that `diagnose.py` already extracts, this file is
the only edit:

    Runbook(
        id="dob_mismatch",
        label="Report date of birth matches the patient record",
        field="dob",
        detail_mismatch="report {report_value!r} vs record {record_value!r}",
        detail_match="both {record_value!r}",
        resolution=(
            'Open the "Patients Info" tab and find patient `{patient_id}`.',
            'Click "Edit patient" and set the date of birth to `{report_dob}`.',
        ),
    )

`field` names the comparison to read, and a comparator for it must exist in
`diagnose.COMPARATORS`. A new field means one small function there too: what
counts as equal is a judgement about the data, and it stays in code where it
can be tested — `compare_name` treats case as insignificant and punctuation as
significant, which no config format would express honestly.

Templates are formatted with: `patient_id`, `order_group_id`, `report_value`,
`record_value`, `report_normalized`, `record_normalized`, plus two field-named
keys -- `report_<field>` for what the report literally said, and
`normalized_<field>` for the cleaned-up form. Pick deliberately: the name step
says to type the name EXACTLY, so it uses `{report_name}`, while the gender
step wants `{normalized_gender}` because `m` is what the dashboard expects
rather than whatever the report wrote. A missing key raises rather than
rendering a half-written instruction.

What does NOT belong here: the verdict logic. A runbook says what to compare
and what to do about it; `diagnose.py` decides whether it fired, by exact
comparison, never by asking a model.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Runbook:
    """One condition, its wording, and the fix when it fires."""

    id: str
    label: str
    field: str
    detail_mismatch: str
    detail_match: str
    resolution: tuple[str, ...]


# Identifiers are backticked so the chat UI renders them as code. The CLI
# strips the marks when printing, via diagnose.strip_code_marks.
_REGENERATE = (
    'Click "Regenerate Smart Report" and enter order_group_id `{order_group_id}`.'
)
_FIND_PATIENT = 'Open the "Patients Info" tab and find patient `{patient_id}`.'


RUNBOOKS: tuple[Runbook, ...] = (
    Runbook(
        id="name_mismatch",
        label="Report name matches the patient record",
        field="name",
        detail_mismatch="report {report_value!r} vs record {record_value!r}",
        detail_match="both {record_value!r} (case-insensitive)",
        resolution=(
            _FIND_PATIENT,
            'Click "Edit patient" and set the name to exactly `{report_name}`.',
            _REGENERATE,
        ),
    ),
    Runbook(
        id="gender_mismatch",
        label="Report gender matches the patient record",
        field="gender",
        detail_mismatch="report {report_value!r} vs record {record_value!r}",
        detail_match="both resolve to {record_normalized!r}",
        resolution=(
            _FIND_PATIENT,
            'Click "Edit patient" and set gender to `{normalized_gender}`.',
            _REGENERATE,
        ),
    ),
)


def by_field(field: str) -> Runbook | None:
    return next((r for r in RUNBOOKS if r.field == field), None)


def by_id(runbook_id: str) -> Runbook | None:
    return next((r for r in RUNBOOKS if r.id == runbook_id), None)
