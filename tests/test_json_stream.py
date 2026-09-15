"""Reading a JSON object while it is still being written.

The scanner decides only what can be shown early. Whatever it reports must
agree with what json.loads sees at the end -- these tests pin that, including
the shapes that would tempt it into reporting nested or half-written values.
"""

from __future__ import annotations

import json

from oncallbot.json_stream import JsonFieldStream


def _run(*chunks):
    js = JsonFieldStream()
    events = []
    for c in chunks:
        events += js.feed(c)
    return events, js


def _fields(events):
    return {k: v for kind, k, v in events if kind == "field"}


def _deltas(events, key):
    return "".join(v for kind, k, v in events if kind == "delta" and k == key)


def test_a_field_is_reported_when_its_closing_quote_arrives():
    events, _ = _run('{"issue": "Report missing", "severity": "p1"}')
    assert _fields(events) == {"issue": "Report missing", "severity": "p1"}


def test_deltas_arrive_before_the_field_and_add_up_to_it():
    events, _ = _run('{"summary": "Asha ', "cannot see ", 'her report."}')
    assert _deltas(events, "summary") == "Asha cannot see her report."
    assert _fields(events)["summary"] == "Asha cannot see her report."
    # Order matters: the reader sees the text, then the field closes.
    kinds = [kind for kind, k, _ in events if k == "summary"]
    assert kinds[-1] == "field"
    assert kinds[:-1] == ["delta"] * (len(kinds) - 1)


def test_a_field_split_across_chunks_at_every_boundary():
    text = '{"a": "one", "b": "two"}'
    for cut in range(1, len(text)):
        events, js = _run(text[:cut], text[cut:])
        assert _fields(events) == {"a": "one", "b": "two"}, f"cut at {cut}"
        assert json.loads(js.text) == {"a": "one", "b": "two"}


def test_keys_are_never_reported_as_values():
    events, _ = _run('{"issue": "x"}')
    assert [k for kind, k, _ in events if kind == "field"] == ["issue"]
    assert "issue" not in [v for _kind, _k, v in events]


def test_nested_objects_and_arrays_are_left_alone():
    events, _ = _run(
        '{"asks": ["fix it", "call me"], '
        '"affected_entities": {"order_ids": ["PO1"], "user_emails": []}, '
        '"issue": "x"}'
    )
    # Only the top-level string is reported; the nested strings are structure
    # for the final parse to handle.
    assert _fields(events) == {"issue": "x"}


def test_escapes_are_decoded_in_both_deltas_and_fields():
    events, js = _run(r'{"summary": "line one.\nline \"two\".\tdone \\ ok"}')
    want = 'line one.\nline "two".\tdone \\ ok'
    assert _fields(events)["summary"] == want
    assert _deltas(events, "summary") == want
    assert json.loads(js.text)["summary"] == want


def test_an_escape_split_across_chunks_is_still_decoded():
    events, _ = _run('{"summary": "a\\', 'nb"}')
    assert _fields(events)["summary"] == "a\nb"


def test_a_unicode_escape_split_across_chunks():
    events, _ = _run('{"summary": "caf\\u0', '0e9"}')
    assert _fields(events)["summary"] == "café"


def test_a_quote_inside_a_value_does_not_end_the_field():
    events, _ = _run(r'{"issue": "the \"trends\" tab", "severity": "p2"}')
    assert _fields(events)["issue"] == 'the "trends" tab'
    assert _fields(events)["severity"] == "p2"


def test_text_before_the_object_does_not_become_a_field():
    events, _ = _run('Here you go:\n{"issue": "x"}')
    assert _fields(events) == {"issue": "x"}


def test_a_markdown_fence_does_not_become_a_field():
    events, js = _run('```json\n{"issue": "x"}\n```')
    assert _fields(events) == {"issue": "x"}
    assert "```" in js.text, "the raw text is kept verbatim for the final parse"


def test_text_keeps_everything_fed_for_the_authoritative_parse():
    _events, js = _run('{"issue": "x", ', '"confidence": 0.8}')
    assert json.loads(js.text) == {"issue": "x", "confidence": 0.8}


def test_a_truncated_stream_reports_only_what_closed():
    """A cut-off value is never reported as a field -- only as deltas."""
    events, _ = _run('{"issue": "done", "summary": "half writ')
    assert _fields(events) == {"issue": "done"}
    assert _deltas(events, "summary") == "half writ"


def test_one_character_at_a_time_matches_one_whole_chunk():
    text = '{"reporter": "Asha <a@b.com>", "summary": "Two\\nlines.", "confidence": 0.9}'
    whole, _ = _run(text)
    piecemeal, _ = _run(*text)
    assert _fields(whole) == _fields(piecemeal)
    assert _deltas(whole, "summary") == _deltas(piecemeal, "summary")
