"""Behavior tests for the diff popup and the Keys panel (cards b4e872, 6b29b4).

The panel logic lives in the served front-end `app/static/app.js`, so the
reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/diff_panel_check.js`) and checks that a diff renders line by line with
the right classifier, that every line is a TEXT NODE (the content is
model-written — never innerHTML), that the verdict sits next to the diff, that
the panel floats next to the clicked card instead of covering the board, that
D + click opens it while a plain click still opens the editor, and that the
Keys panel lists N, Esc, D + click and C + click from the one KEYBINDINGS list.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "diff_panel_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js diff model")


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def _served_app_js(env) -> Path:
    """Fetch app.js through the API (what the browser actually gets)."""
    r = env.client.get("/app.js")
    assert r.status_code == 200, r.text
    out = env.tmp_path / "app.js"
    out.write_text(r.text)
    return out


def _run_diff_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"diff_panel_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_diff_panel_renders_lines_as_text_nodes(env):
    result = _run_diff_check(env)
    assert result["ok"] is True, result["failures"]


def test_diff_panel_check_covers_the_spec(env):
    """Guard against the reproducer silently checking nothing."""
    source = HARNESS_JS.read_text()
    for needle in ("classifyDiffLine", "TEXT NODE", "KEYBINDINGS",
                   "scrolledIntoView", "not git-backed", "D + click",
                   "diff-popup", "pointermove", "hoverCardId"):
        assert needle in source, needle


def test_diff_popup_markup_is_a_floating_panel(env):
    """The diff is its own small panel — not an .overlay modal."""
    html = env.client.get("/").text
    assert 'id="diff-popup"' in html
    assert 'id="diff-popup-head"' in html
    assert 'id="diff-ranges"' in html
    assert 'id="diff-verdict"' in html
    assert 'id="diff-files"' in html
    assert 'id="diff-body"' in html
    assert 'id="diff-close"' in html
    # It is not wrapped in the full-screen overlay the modals use.
    popup_at = html.index('id="diff-popup"')
    assert 'class="overlay"' not in html[popup_at - 200:popup_at]
    # ...and the old full-screen diff modal is gone.
    assert 'id="diff-overlay"' not in html
    assert "diff-modal" not in html


def test_hover_then_press_d_gesture_is_gone(env):
    """D is a held modifier now: no hover tracking, no bare-D keydown branch."""
    js = env.client.get("/app.js").text
    for gone in ("hoverCardId", "hoveredCardId"):
        assert gone not in js, gone
    # The board no longer tracks the card under the pointer.
    assert "boardEl.addEventListener('mouseover'" not in js
    assert "const boardEl = $('board')" not in js
    # D is tracked as a held key, like C.
    assert "dKeyDown" in js
    assert "cKeyDown" in js


def _css_rule(css: str, selector: str) -> str:
    """The declarations of one CSS rule (the first exact `selector {` match)."""
    at = css.index(selector + " {")
    return css[at + len(selector) + 2: css.index("}", at)]


def test_diff_popup_styles_are_floating(env):
    css = env.client.get("/style.css").text
    # The floating behaviour lives in the shared base...
    base = _css_rule(css, ".floating-panel")
    assert "position: fixed" in base
    # ...and the diff popup only adds its own size on top of it.
    block = _css_rule(css, ".diff-popup")
    assert "720px" in block
    assert "640px" in block
    # The old full-screen modal sizing is gone.
    assert ".diff-modal" not in css


def test_floating_panels_share_one_base(env):
    """The diff popup is the activity popup's sibling, not a second copy of it.

    Both carry the `.floating-panel` base; each rule adds only what differs
    (its size), so a change to the panel chrome lands in one place.
    """
    html = env.client.get("/").text
    assert 'class="floating-panel activity-popup"' in html
    assert 'class="floating-panel diff-popup"' in html
    assert 'class="floating-panel-head activity-head"' in html
    assert 'class="floating-panel-head diff-popup-head"' in html
    assert 'class="floating-panel-title"' in html

    css = env.client.get("/style.css").text
    base = _css_rule(css, ".floating-panel")
    for prop in ("position: fixed", "display: flex", "flex-direction: column",
                 "background: var(--surface)", "border-radius: 10px",
                 "box-shadow:", "overflow: hidden"):
        assert prop in base, prop
    # Each popup adds its size and nothing the base already says.
    for selector, size in ((".activity-popup", "420px"), (".diff-popup", "720px")):
        block = _css_rule(css, selector)
        assert size in block, selector
        for prop in ("position: fixed", "background:", "border-radius",
                     "box-shadow", "overflow:"):
            assert prop not in block, f"{selector} repeats {prop} from .floating-panel"
    # The header chrome is shared too; each head only says how it lays out.
    head = _css_rule(css, ".floating-panel-head")
    for prop in ("display: flex", "gap: 8px", "padding: 8px 10px",
                 "border-bottom: 1px solid var(--border-soft)",
                 "background: var(--surface-2)"):
        assert prop in head, prop
    for selector in (".activity-head", ".diff-popup-head"):
        block = _css_rule(css, selector)
        for prop in ("padding:", "border-bottom", "background:"):
            assert prop not in block, f"{selector} repeats {prop} from .floating-panel-head"
    # The title truncation is shared too — no per-popup copy of it.
    title = _css_rule(css, ".floating-panel-title")
    for prop in ("white-space: nowrap", "text-overflow: ellipsis",
                 "font-weight: 600"):
        assert prop in title, prop
    for clone in (".activity-title {", ".diff-popup-title {"):
        assert clone not in css, clone
