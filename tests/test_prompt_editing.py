"""The prompt behind an answer is visible and editable from the UI.

A prompt is behaviour: most of what this tool gets right or wrong is decided
by the wording, so the fastest way to fix a bad answer is to change the words
and ask again.

Edits are held by the CLIENT and travel with each request. Nothing is stored
server-side and nothing is written down, so a refresh resets them and one
person's experiment cannot outlive their tab or reach anyone else.
"""

from pathlib import Path

from conftest import _Collapsed, _flat

from oncallbot.prompts import chat as chat_prompts
from oncallbot.prompts.live import CATALOG, catalog_for, prompt_for, set_overrides

INDEX = Path("src/oncallbot/chat/static/index.html")


def page() -> _Collapsed:
    """Whitespace-insensitive, so a re-indent does not fail these."""
    return _Collapsed(INDEX.read_text())


# --- the override itself ---------------------------------------------------


def test_an_edit_is_used_for_its_own_key_only():
    """Editing the routing prompt must not change how a diagnosis narrates."""
    set_overrides({"chat.context": "THREE WORDS ONLY."})
    assert prompt_for("chat.context", "default") == "THREE WORDS ONLY."
    assert prompt_for("routing", "default") == "default"
    set_overrides({})


def test_an_unknown_key_is_ignored():
    """The client sends what it likes; only known keys are honoured."""
    set_overrides({"not.a.key": "hello"})
    assert prompt_for("not.a.key", "default") == "default"
    set_overrides({})


def test_a_blank_edit_falls_back_to_the_shipped_prompt():
    set_overrides({"chat.context": "   "})
    assert prompt_for("chat.context", chat_prompts.CONTEXT_SYSTEM_PROMPT) is (
        chat_prompts.CONTEXT_SYSTEM_PROMPT
    )
    set_overrides({})


def test_every_catalogued_prompt_belongs_to_a_surface():
    """A key no CTA offers would be editable by nobody."""
    surfaces = {"routing", "summary", "diagnosis", "followup"}
    for key, (where, _label, text) in CATALOG.items():
        assert where in surfaces, key
        assert text.strip(), key


def test_each_cta_offers_its_own_prompts():
    assert [c["key"] for c in catalog_for("routing")] == ["routing"]
    assert [c["key"] for c in catalog_for("summary")] == ["summarize"]
    assert all(c["key"].startswith("diagnosis.") for c in catalog_for("diagnosis"))


# --- the endpoint ----------------------------------------------------------


def test_the_catalog_carries_the_real_text():
    routing = catalog_for("routing")[0]
    assert routing["key"] == "routing"
    assert "You route" in routing["text"], "the real prompt, not a placeholder"


def test_the_request_models_accept_edits():
    """Rejecting them would make the feature silently do nothing."""
    from oncallbot.chat.server import ChatRequest, DiagnoseRequest

    assert ChatRequest(message="hi", prompts={"routing": "X"}).prompts == {"routing": "X"}
    assert DiagnoseRequest(order_group_id="PO1-1", prompts={"a": "b"}).prompts


# --- the UI ----------------------------------------------------------------


def test_the_edits_ride_along_with_every_request():
    html = page()
    assert "body: JSON.stringify({ ...body, prompts: promptEdits })" in html
    assert "prompts: promptEdits }" in html, "the chat turn carries them too"


def test_the_edits_are_never_written_down():
    """In memory only: a refresh is what resets them, which is the point."""
    html = _flat(page())
    i = html.index(_flat("const promptEdits = {}"))
    # No localStorage anywhere near the store's declaration or its writes.
    assert _flat("localStorage.setItem(promptEdits") not in html
    assert i > 0


def test_every_surface_has_a_cta():
    html = page()
    assert 'id="routingprompt"' in html, "the header CTA"
    assert "promptCta('summary')" in html
    assert "promptCta('diagnosis')" in html
    assert "promptCta('followup', 'Prompt used')" in html


def test_reset_restores_the_shipped_wording():
    html = page()
    assert "area.value = mbody._defaults[i];" in html
    assert "delete promptEdits[key];" in html


def test_an_empty_prompt_is_refused_rather_than_saved():
    """Saving "" would silently mean "use the default", which reads as a bug."""
    html = page()
    assert "An empty prompt would just use the default." in html


# --- task 2: telling the panels apart --------------------------------------


def test_the_panels_say_which_is_which():
    """Readers could not tell where a summary ended and a diagnosis began."""
    html = page()
    assert '<span class="panelname">Summary</span>' in html
    assert '<span class="panelname">Diagnosis</span>' in html
    assert ".panelhead{" in html
