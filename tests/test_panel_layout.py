"""Behavior tests for the movable, resizable, remembered floating panels.

The panels live in the served front-end (`app/static/app.js` + `style.css`), so
the reproducer boots the *served* app.js under Node with the shared DOM stub
(`tests/panel_layout_check.js`) and checks that both popups drag by their
header, that a native resize — which writes the inline width/height *and* the
used box — is saved once (debounced, not per frame), that opening a panel is
not mistaken for a resize, that the layout round-trips through `localStorage`
under `vibe-harness:panel:<key>`, that a corrupt or unreadable value falls back
to the defaults, and that clearing `localStorage` returns both panels to their
defaults. The CSS side — the resize grip, the size floors and the strip of
padding the grip sits in — is asserted here.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from node_check import TESTS_DIR, requires_node, run_node_check
from test_api import make_env

HARNESS_JS = TESTS_DIR / "panel_layout_check.js"

pytestmark = requires_node


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def test_panels_move_resize_and_remember_their_layout(env):
    result = run_node_check(env, HARNESS_JS)
    assert result["ok"] is True, result["failures"]


def test_panel_layout_check_covers_the_spec(env):
    """Guard against the reproducer silently checking nothing."""
    source = HARNESS_JS.read_text()
    for needle in ("positionFloatingPanel", "panelLayout", "loadPanelLayout",
                   "savePanelLayout", "makePanelDraggable", "onEnd",
                   "ResizeObserver", "vibe-harness:panel:", "openActivity",
                   "openDiff", "private mode", "reload"):
        assert needle in source, needle


def _css_rule(css: str, selector: str) -> str:
    """The declarations of one CSS rule (the first exact `selector {` match)."""
    at = css.index(selector + " {")
    return css[at + len(selector) + 2: css.index("}", at)]


def test_both_panels_are_resizable(env):
    css = env.client.get("/style.css").text
    # The grip is part of the shared panel chrome: both popups get it.
    assert "resize: both" in _css_rule(css, ".floating-panel")
    # Each popup sets its own floor.
    activity = _css_rule(css, ".activity-popup")
    assert "min-width: 320px" in activity
    assert "min-height: 240px" in activity
    diff = _css_rule(css, ".diff-popup")
    assert "min-width: 420px" in diff
    assert "min-height: 320px" in diff


def test_the_activity_grip_does_not_cover_send(env):
    """The native grip is drawn in the bottom-right corner, over Send."""
    css = env.client.get("/style.css").text
    assert "padding-bottom" in _css_rule(css, ".activity-popup")


def test_both_headers_are_drag_handles(env):
    """The grab cursor is shared chrome, not a per-popup copy."""
    css = env.client.get("/style.css").text
    head = _css_rule(css, ".floating-panel-head")
    assert "cursor: grab" in head
    assert "user-select: none" in head
    assert "cursor: grabbing" in _css_rule(css, ".floating-panel-head:active")

    html = env.client.get("/").text
    assert 'id="activity-head"' in html
    js = env.client.get("/app.js").text
    assert "makePanelDraggable(activityPopup, activityHead" in js
    assert "makePanelDraggable(diffPopup, diffPopupHead" in js


def test_the_layout_is_stored_per_panel(env):
    js = env.client.get("/app.js").text
    assert "vibe-harness:panel:" in js
    # Both panels read their own saved layout on open.
    assert "loadPanelLayout('activity')" in js
    assert "loadPanelLayout('diff')" in js


def test_the_open_path_marks_its_own_positioning(env):
    """The observer must not mistake the harness placing a panel for a resize.

    Comparing the rendered box with the inline style cannot tell them apart: a
    native `resize: both` drag writes the inline width/height too, so after any
    real resize the two match — that wrong premise is why the size was never
    saved. The open path marks the panel instead.
    """
    js = env.client.get("/app.js").text
    assert "same(layout.width, parseFloat(panel.style.width))" not in js
    assert "dataset.positioning" in js
    for opener in ("openActivity", "openDiff"):
        body = js[js.index("function " + opener + "("):]
        body = body[:body.index("\n}\n")]
        assert "markPanelPositioning(" in body, opener
