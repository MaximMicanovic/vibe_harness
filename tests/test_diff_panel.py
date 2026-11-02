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


def test_diff_popup_styles_are_floating(env):
    css = env.client.get("/style.css").text
    assert ".diff-popup" in css
    block = css[css.index(".diff-popup {"):]
    block = block[: block.index("}")]
    assert "position: fixed" in block
    assert "720px" in block
    assert "640px" in block
    # The old full-screen modal sizing is gone.
    assert ".diff-modal" not in css
