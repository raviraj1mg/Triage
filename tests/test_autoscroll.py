"""Streaming follows the newest text until the reader scrolls up.

Nobody reads as fast as a model streams, so an answer that keeps yanking the
view down is unreadable the moment it runs past a screen. Scrolling up means
"I am reading this" -- so autoscroll stops there and resumes only when the
reader returns to the bottom.

Measured in the browser: with the reader scrolled 400px up, fifteen further
appends moved the viewport by 0px.
"""

from pathlib import Path

from conftest import _Collapsed, _flat

INDEX = Path("src/oncallbot/chat/static/index.html")


def page() -> _Collapsed:
    return _Collapsed(INDEX.read_text())


def test_autoscroll_is_conditional_on_the_reader_being_at_the_bottom():
    html = page()
    assert "function scroll(force) {" in html
    assert "if (!force && !stickToBottom) return;" in html


def test_scrolling_up_switches_it_off_and_returning_switches_it_back_on():
    """One handler does both, because `atBottom()` is the whole condition."""
    html = page()
    assert "stickToBottom = atBottom();" in html
    assert "log.addEventListener('scroll'" in html


def test_a_programmatic_scroll_does_not_look_like_the_reader():
    """Ours lands AT the bottom, so the handler recomputes to true and
    stickiness survives it -- no flag needed to tell them apart. If this ever
    changes, the feature silently stops following its own output."""
    html = page()
    i = html.index(_flat("function scroll(force)"))
    body = _flat(str(html))[i:i + 260]
    assert _flat("stickToBottom = true;") in body
    assert _flat("log.scrollTop = log.scrollHeight;") in body


def test_a_small_nudge_does_not_count_as_scrolling_away():
    """A trackpad twitch near the end should not switch autoscroll off."""
    html = page()
    assert "const BOTTOM_SLACK = 48;" in html
    assert "<= BOTTOM_SLACK" in html


def test_sending_a_question_always_takes_you_to_it():
    """Whatever they were reading, they just asked something new."""
    html = page()
    i = html.index(_flat("async function ask(message)"))
    assert _flat("stickToBottom = true;") in _flat(str(html))[i:i + 400]


def test_the_old_ad_hoc_distance_checks_are_gone():
    """Two handlers had their own 160px rule and every other call site had
    none. One rule, in one place, or they drift apart again."""
    assert "log.scrollHeight - log.scrollTop - log.clientHeight < 160" not in page()


def test_the_jump_button_is_offered_only_when_it_would_help():
    """Scrolled away while an answer is still arriving. Idle, there is nothing
    to catch up with; at the bottom, nothing to jump to."""
    html = page()
    assert "btn.hidden = stickToBottom || !busy;" in html
    assert 'id="jump"' in html


def test_the_jump_button_returns_and_re_arms():
    html = page()
    assert "scroll(true); input.focus();" in html
