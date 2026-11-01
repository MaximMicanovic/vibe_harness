"""Behavior tests for the board's card stacking (card 5fe1eb).

The stacking logic lives in the served front-end `app/static/app.js`, so the
reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/stack_model_check.js`) and drives the same functions drag&drop calls
(`stackCards`). This fails on the original code (the dragged card landed on
top of the stack, burying the card below) and passes with the fix (the drop
target — the card below — stays on top of the stack).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "stack_model_check.js"
DRAG_POLL_JS = TESTS_DIR / "stack_drag_poll_repro.cjs"
REUSE_JS = TESTS_DIR / "stack_reuse_check.cjs"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js stack model")


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


def _run_stack_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"stack_model_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_multiple_cards_stack_with_card_below_on_top(env):
    # Spec (card 5fe1eb): "multiple cards should be able to be stacked and
    # when stacked the card below should be on top of the stack."
    # Dropping b, c, d onto a must pile all four into ONE stack whose top is
    # the drop target `a` (the card below), i.e. last in ids and last
    # .stack-card in the DOM (painted on top).
    result = _run_stack_check(env)
    assert result["ok"], "stack model check failed: " + "; ".join(result["failures"])
    assert result["ids"][-1] == "a"
    assert len(result["ids"]) == 4
    assert result["domOrder"][-1] == "a"


def _run_drag_poll_check(env) -> dict:
    """Boot the served app.js and drive a real drag while the 2 s live poll
    re-renders the board mid-drag (see stack_drag_poll_repro.cjs)."""
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(DRAG_POLL_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"stack_drag_poll_repro.cjs did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_poll_during_drag_does_not_cancel_stacking(env):
    # Card 2526b0: while any card is live the 2 s poll re-renders the board;
    # a mid-drag re-render removes the dragged card from the DOM, which
    # cancels the HTML5 drag, so the drop (and the stack) never happened.
    # The drop on the target must still land and form the stack, with the
    # card below (the drop target) on top.
    result = _run_drag_poll_check(env)
    assert result["ok"], "drag/poll repro failed: " + "; ".join(result["failures"])
    assert result["dropDelivered"] is True
    assert result["stacks"] == [["b", "a"]]


def test_poll_reuses_unchanged_nodes_and_stack_offers_unstack_controls(env):
    # Graphical-bug fix for stacked cards (overlap + shake while a card is
    # live): the 2 s poll must reuse unchanged card/stack DOM nodes instead
    # of tearing the column down (recreating nodes snaps in-flight
    # transitions, so fanned stacks jumped/shake), and expanding a stacked
    # card's title must re-measure the stack box height (--card-h)
    # immediately, else the absolutely positioned card overflows the stack
    # and overlaps the next item until the next poll. The same script also
    # pins the unstack affordances: a fanned stack carries an unstack-all
    # button and every stacked card carries its own, so leaving a stack no
    # longer requires knowing that dragging onto the column background works.
    # Driven against the served app.js by tests/stack_reuse_check.cjs (red on
    # the old code).
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(REUSE_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"stack_reuse_check.cjs did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    assert result["ok"], "stack reuse/geometry check failed: " + "; ".join(result["failures"])


def test_stack_model_check_script_is_hermetic():
    # The check script must exist next to the tests and boot any app.js path.
    assert HARNESS_JS.exists()
    src = HARNESS_JS.read_text()
    assert "stackCards" in src
    assert "process.exit" in src
