"""Behavior tests for the live activity popup (card e02cd4).

The popup lives in the served front-end `app/static/app.js`, so the reproducer
boots the *served* app.js under Node with a minimal DOM stub
(`tests/activity_popup_check.js`) and checks that the activity feed renders one
row per journal event, that reasoning is collapsed behind a "thinking…" toggle,
that every piece of journal text is a TEXT NODE (the journal is model-written —
never innerHTML), that the panel floats near the card instead of covering the
board, that C + click opens it while a plain click still opens the editor, that
Esc closes it, and that the old chat-mode toggle is gone.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "activity_popup_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js activity model")


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


def _run_activity_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"activity_popup_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_activity_popup_renders_the_feed_and_the_gesture(env):
    result = _run_activity_check(env)
    assert result["ok"] is True, result["failures"]


def test_activity_popup_check_covers_the_spec(env):
    """Guard against the reproducer silently checking nothing."""
    source = HARNESS_JS.read_text()
    for needle in ("renderActivityEvents", "thinking", "innerHTML",
                   "openActivity", "activity-jump", "KEYBINDINGS", "chatMode"):
        assert needle in source, needle


def test_activity_popup_markup_is_a_floating_panel(env):
    """The popup is its own small panel — not an .overlay modal."""
    html = env.client.get("/").text
    assert 'id="activity-popup"' in html
    assert 'id="activity-feed"' in html
    assert 'id="activity-close"' in html
    assert 'id="activity-jump"' in html
    # It is not wrapped in the full-screen overlay the modals use.
    popup_at = html.index('id="activity-popup"')
    assert 'class="overlay"' not in html[popup_at - 200:popup_at]


def test_chat_mode_is_gone(env):
    """The 💬 rail button, its panel and its state are removed."""
    html = env.client.get("/").text
    assert 'data-panel="chat"' not in html
    assert "💬" not in html

    js = env.client.get("/app.js").text
    for gone in ("chatMode", "loadChatMode", "saveChatMode",
                 "renderChatPanel", "chat-mode"):
        assert gone not in js, gone

    css = env.client.get("/style.css").text
    assert "chat-mode" not in css
    assert "rail-btn--mode-on" not in css


def test_activity_popup_styles_are_floating(env):
    css = env.client.get("/style.css").text
    # Fixed positioning, no backdrop: the board stays usable behind it. That
    # chrome is the shared .floating-panel base (see test_diff_panel.py); the
    # activity popup only adds its own size.
    base = css[css.index(".floating-panel {"):]
    base = base[: base.index("}")]
    assert "position: fixed" in base
    block = css[css.index(".activity-popup {"):]
    block = block[: block.index("}")]
    assert "420px" in block
    assert "520px" in block
