"""The Thinking accordion: what the turn actually did.

A TRACE, not the model's private reasoning. We stream content deltas and
nothing else, and the local models emit no reasoning channel at all -- so the
panel holds what we genuinely know: which prompt each call used, which reads
went out with what parameters, and the steps around them.

Two choke points cover every feature: `prompt_for` is how every model call
resolves its system prompt, and `call_tool` is how every admin read goes out.
Instrumenting those two means a new feature is traced without anyone
remembering to add a line.
"""

from pathlib import Path

import pytest
from conftest import _Collapsed, _flat

from oncallbot.prompts.live import CATALOG, prompt_for
from oncallbot.tools.executor import call_tool
from oncallbot.tools.registry import read_only_tools
from oncallbot.trace import MAX_VALUE, note, set_sink

INDEX = Path("src/oncallbot/chat/static/index.html")


def page() -> _Collapsed:
    return _Collapsed(INDEX.read_text())


@pytest.fixture
def seen():
    out: list[dict] = []
    set_sink(out.append)
    yield out
    set_sink(None)


# --- the recording ---------------------------------------------------------


def test_nothing_is_recorded_when_nobody_is_listening():
    """The CLI and every test that has not opted in pay nothing."""
    set_sink(None)
    note("prompt", "x")  # must not raise


def test_a_sink_that_throws_cannot_break_the_answer(seen):
    def bad(_entry):
        raise RuntimeError("boom")

    set_sink(bad)
    note("prompt", "x")  # must not raise


def test_every_model_call_records_the_prompt_it_used(seen):
    prompt_for("routing", "SOME PROMPT TEXT")
    assert seen[0]["kind"] == "prompt"
    assert seen[0]["key"] == "routing"
    assert seen[0]["chars"] == len("SOME PROMPT TEXT")


def test_an_edited_prompt_is_marked_as_edited(seen):
    from oncallbot.prompts.live import set_overrides

    set_overrides({"routing": "MINE"})
    prompt_for("routing", "SHIPPED")
    set_overrides({})
    assert seen[0]["edited"] is True
    assert seen[0]["text"] == "MINE", "the trace shows what was actually sent"


def test_every_admin_read_records_its_parameters(seen):
    class Fake:
        def get(self, path, query=None):
            return {}

    spec = next(t for t in read_only_tools() if t.name == "get_user_details_for_an_order")
    call_tool(Fake(), spec, {"order_group_id": "PO1-1"})
    tools = [e for e in seen if e["kind"] == "tool"]
    assert tools[0]["label"] == "get_user_details_for_an_order"
    assert tools[0]["params"] == {"order_group_id": "PO1-1"}
    assert tools[0]["method"] == "GET"


def test_a_read_is_recorded_before_it_is_attempted(seen):
    """A call that fails is the one you most want to see in the panel."""
    from oncallbot.hra_client import HraError

    class Broken:
        def get(self, path, query=None):
            raise HraError("nope")

    spec = next(t for t in read_only_tools() if t.name == "get_user_details_for_an_order")
    with pytest.raises(HraError):
        call_tool(Broken(), spec, {"order_group_id": "PO1-1"})
    assert [e for e in seen if e["kind"] == "tool"], "recorded despite failing"


def test_a_huge_value_is_trimmed(seen):
    """A parameter can be a whole payload; the panel is a summary."""
    note("tool", "x", params={"blob": "y" * 5000})
    assert len(seen[0]["params"]["blob"]) <= MAX_VALUE + 1


def test_empty_fields_are_left_out(seen):
    note("tool", "x", params={}, method="")
    assert "params" not in seen[0] and "method" not in seen[0]


# --- every feature is covered ----------------------------------------------


def test_every_catalogued_prompt_is_traced_by_construction():
    """Because the trace is recorded inside prompt_for, a feature is covered
    the moment it resolves a prompt -- there is no per-feature wiring to
    forget."""
    assert len(CATALOG) >= 12
    for key in ("routing", "summarize", "grouping", "suggest",
                "chat.context", "chat.answer",
                "order.plan", "order.answer",
                "diagnosis.reason", "diagnosis.findings",
                "diagnosis.closure", "diagnosis.followup"):
        assert key in CATALOG, key


# --- the panel -------------------------------------------------------------


def test_all_three_streams_render_the_accordion():
    """The chat turn injects a node; the cards render it as part of the panel,
    because a panel is rebuilt on every delta and an injected node would be
    wiped by the next one."""
    html = page()
    assert "function traceInto(turnEl, entry)" in html, "the chat turn"
    assert html.count("traceBlock(live)") >= 3, "both panels, plus its definition"
    assert html.count("live.trace.push(payload); paint();") == 2, "summary and diagnosis"


def test_the_cards_show_the_trace_inside_the_panel_under_its_header():
    """Asked for: the steps sit with the summary or diagnosis they produced,
    not floating above the card."""
    html = _Collapsed(str(page()))
    for name in ("Summary", "Diagnosis"):
        i = html.index(_flat(f'<span class="panelname">{name}</span>'))
        after = _flat(str(page()))[i:i + 260]
        assert _flat("traceBlock(live)") in after, name


def test_the_finished_panel_keeps_its_trace():
    """Passing null was how "not streaming" was said, and it would throw the
    steps away at the moment they are most worth reading."""
    html = page()
    assert "function done(live)" in html
    assert "summaryPanel(payload.summary, done(live))" in html


def test_the_open_state_survives_a_repaint():
    """The <details> element itself does not survive one, so the flag lives on
    the object that does."""
    html = page()
    assert "el._live.traceOpen = !det.open;" in html
    assert "slot._live = live;" in html


def test_it_is_closed_until_asked_for():
    """It explains an answer rather than being one."""
    html = page()
    assert "<details" in html and "class='thinking'" in html or 'className = \'thinking\'' in html


def test_the_full_prompt_is_available_behind_a_second_click():
    html = page()
    assert '<details class="tdetail"><summary>show</summary><pre>' in html
    assert ".tdetail pre{" in html
