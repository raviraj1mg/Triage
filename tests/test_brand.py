"""The mark in the header.

Three bars sorted by length -- the priority lanes an oncall gets put into --
with the top one carrying an ECG pulse for the health records they are about.
Drawn from --p0/--p1/--p2, the same tokens the severity pills use, so the mark
is made of the severities the tool sorts by rather than a colour invented for
it.

Read from the static files rather than through a client: they are what the
server hands back verbatim (FileResponse, no templating), and the mark is in
both pages while the chat fixture only serves one.
"""

from pathlib import Path

from conftest import _Collapsed, _flat

STATIC = Path("src/oncallbot/chat/static")


def page(name: str) -> _Collapsed:
    """Whitespace-insensitive, so a re-indent does not fail these."""
    return _Collapsed((STATIC / name).read_text())


def test_the_header_carries_the_mark_beside_the_name():
    html = page("index.html")
    assert '<div class="brand">' in html
    assert 'class="mark"' in html
    # It still reads "Triage / oncallbot" next to it.
    assert "<h1>Triage</h1>" in html
    assert '<span class="addr">oncallbot</span>' in html


def test_the_mark_has_all_three_lanes():
    html = page("index.html")
    for var in ("--p0", "--p1", "--p2"):
        assert f"var({var}," in html, f"the {var} lane is missing"


def test_the_mark_is_decorative_to_a_screen_reader():
    """"Triage" is already the h1 beside it, so announcing the mark too would
    read the name twice."""
    # Both offset and slice on the flattened text: index() searches the
    # flattened form, so slicing the raw string would use the wrong offsets.
    flat = _flat((STATIC / "index.html").read_text())
    i = flat.index(_flat('class="mark"'))
    assert _flat('aria-hidden="true"') in flat[i:i + 300]


def test_every_lane_carries_a_literal_fallback():
    """login.html never defined --p2, so `stroke="var(--p2)"` resolved to no
    stroke at all and the third bar was invisible. The token is defined now;
    the fallback means a missing one can never silently drop a lane again."""
    for name in ("index.html", "login.html"):
        html = page(name)
        assert "var(--p0, #c0243a)" in html, name
        assert "var(--p1, #d9722b)" in html, name
        assert "var(--p2, #a8871f)" in html, name


def test_the_login_page_uses_the_same_mark():
    html = page("login.html")
    assert 'class="mark"' in html
    assert '<div class="brand">' in html


def test_the_login_page_defines_every_lane_token():
    """The regression: --p2 was absent here, in light and in both dark blocks."""
    assert page("login.html").count("--p2:") == 3


def test_both_pages_have_a_favicon():
    """There was none at all, so the tab showed a blank document icon."""
    for name in ("index.html", "login.html"):
        assert 'rel="icon"' in page(name), name
