"""Behavior test for the board's red dependency strings (card f577f3).

Spec: "The arrows that set draft cards in que are pointing in the wrong
direction. They should point from draft towards the running/reviewing cards."

The strings are drawn by the served front-end `app/static/app.js`
(`renderStrings()`), so the reproducer boots the *served* app.js under Node
with a minimal DOM stub (`tests/string_direction_check.js`), lays out a queued
draft card and the running/reviewing card it depends on at two known positions,
and reads the drawn SVG path back.

This fails on the original code (the path ran from the running/reviewing card to
the draft, so the arrowhead landed on the draft) and passes with the fix (the
string starts at the draft and the arrowhead lands on the running/reviewing
card).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
CHECK_JS = TESTS_DIR / "string_direction_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js string renderer")


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


def _run_string_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(CHECK_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"string_direction_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_arrows_point_from_the_queued_draft_to_the_live_card(env):
    # A draft card queued behind a running/reviewing card must be drawn with
    # the string pinned to the draft and the arrowhead on the live card.
    result = _run_string_check(env)
    assert result["ok"], "string direction check failed: " + "; ".join(result["failures"])
    assert [c["label"] for c in result["cases"]] == [
        "draft queued behind a running card",
        "draft queued behind a reviewing card",
    ]
    for case in result["cases"]:
        assert case["start"] == case["draftAnchor"]
        assert case["end"] == case["liveAnchor"]
        assert case["pin"] == case["draftAnchor"]


def test_string_direction_check_script_is_hermetic():
    # The check script must exist next to the tests and boot any app.js path.
    assert CHECK_JS.exists()
    src = CHECK_JS.read_text()
    assert "renderStrings" in src
    assert "process.exit" in src
