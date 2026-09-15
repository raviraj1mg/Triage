"""Counts belong to the code, not the model.

Asked "how many of these are closed" over four rows, `gemma3:latest` read
`closed: false` as "not known" and reported two settled-open threads as
undiagnosed. `claude_cli` got the same prompt right, which is the worse
failure: two people running the same tool saw different numbers for the same
screen. None of it is a judgement, so none of it is the model's to make.
"""

from oncallbot.chat.facts import (
    dedupe,
    facts_block,
    plain_answer,
    strip_echo,
    tally,
)

ROWS = [
    {"thread_id": "t1", "severity": "p1", "category": "record_not_visible", "closed": False},
    {"thread_id": "t2", "severity": "p2", "category": "duplicate_record", "closed": True},
    {"thread_id": "t3", "severity": "p1", "category": "upload_failure"},
    {"thread_id": "t4", "severity": "p0", "category": "wrong_patient_mapping", "closed": False},
]


def test_absent_and_false_are_not_the_same_thing():
    """`closed: false` is "diagnosed, still open". Absent is "nobody looked"."""
    t = tally(ROWS)
    assert [r["thread_id"] for r in t["closed"]] == ["t2"]
    assert [r["thread_id"] for r in t["open"]] == ["t1", "t4"]
    assert [r["thread_id"] for r in t["unknown"]] == ["t3"]


def test_a_thread_shown_twice_is_counted_once():
    t = tally([*ROWS, dict(ROWS[0])])
    assert t["rows"] == 4


def test_the_later_row_wins():
    """Diagnose may have run between the two turns that showed it."""
    rows = [{"thread_id": "t9"}, {"thread_id": "t9", "closed": True}]
    assert [r["thread_id"] for r in tally(rows)["closed"]] == ["t9"]


def test_rows_without_a_thread_id_are_still_counted():
    assert tally([{"severity": "p1"}])["rows"] == 1
    assert len(dedupe([{"a": 1}, {"a": 2}])) == 2


def test_the_block_states_each_group_unambiguously():
    block = facts_block(ROWS)
    assert "closed: 1" in block
    assert "WERE diagnosed" in block, "open must not read as undiagnosed"
    assert "NEVER been diagnosed" in block
    assert "by severity: p0 1, p1 2, p2 1" in block


def test_no_rows_means_no_block():
    assert facts_block([]) == ""


# --- keeping the scaffolding out of the reply ------------------------------


def run(text, prompt):
    return "".join(strip_echo(iter([text]), prompt))


PROMPT = "rows displayed (4):\n" + facts_block(ROWS)


def test_a_copied_counted_block_is_dropped():
    """It reproduced the block as its "Breakdown"."""
    copied = [x for x in facts_block(ROWS).splitlines() if x.startswith("- ")]
    out = run("Two threads are P1.\n\nBreakdown:\n" + "\n".join(copied) + "\n", PROMPT)
    assert "Two threads are P1." in out
    for line in copied:
        assert line not in out


def test_a_copy_that_dropped_the_list_marker_is_still_a_copy():
    """The observed echo re-listed "- threads on screen: 4" without its dash."""
    out = run("Two are P1.\nthreads on screen: 4\n", PROMPT)
    assert "threads on screen" not in out


def test_a_line_that_merely_resembles_the_block_is_kept():
    """Its own phrasing is an answer, even when it looks like the input."""
    out = run("- closed: just one, `t2`\n", PROMPT)
    assert "just one" in out


def test_a_pasted_row_dump_is_dropped():
    out = run('1 is closed.\n[{"thread_id": "t1", "closed": false}]\n', PROMPT)
    assert "1 is closed." in out
    assert "thread_id" not in out


def test_a_label_left_with_nothing_under_it_goes_too():
    copied = [x for x in facts_block(ROWS).splitlines() if x.startswith("- ")]
    out = run("Two are P1.\n\nBreakdown:\n" + "\n".join(copied) + "\n", PROMPT)
    assert "Breakdown" not in out


def test_a_label_with_a_real_body_is_kept():
    out = run("Here.\n\nBreakdown:\n- `t1` is open\n", PROMPT)
    assert "Breakdown:" in out
    assert "`t1` is open" in out


def test_the_model_s_own_sentences_survive():
    text = "1 thread is closed. `t2` is closed.\n"
    assert run(text, PROMPT) == text


def test_it_behaves_the_same_arriving_one_character_at_a_time():
    """The real path is a token stream."""
    text = "Here.\n\nBreakdown:\n- `t1` is open\n"
    assert "".join(strip_echo(iter(list(text)), PROMPT)) == run(text, PROMPT)


def test_the_fallback_states_the_counts_plainly():
    """Only reached when filtering leaves nothing at all."""
    said = plain_answer(ROWS)
    assert "1 closed" in said and "2 open" in said
    assert "`t3`" in said
