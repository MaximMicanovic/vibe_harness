"""Behavior tests for the token-type bubbles (card 240088, round 2).

Spec: "Instead of having one counter for tokens in total I want cached, input,
output in the same little bubble. The same for total tokens but there we have
more space so we can have three bubbles."

Interpretation: the per-card token chip (the little bubble) shows cached /
input / output inside the *same* single chip, while the top-bar total-tokens
stat renders *three separate bubbles* (one per token type) next to the total.
The bubble logic lives in the served front-end `app/static/app.js`, so the
reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/token_bubbles_check.js`) and asserts the rendered bubbles. This fails
on the code that only had one total counter (plus a hover-only breakdown) and
passes with the bubble rendering.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "token_bubbles_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js bubble model")


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


def _run_bubbles_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"token_bubbles_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_card_bubble_shows_cached_input_output_in_same_bubble(env):
    # Spec (card 240088): the little per-card bubble carries all three token
    # types in one chip instead of a single total counter.
    result = _run_bubbles_check(env)
    assert result["ok"], "token bubbles check failed: " + "; ".join(result["failures"])


def test_top_bar_total_shows_three_type_bubbles(env):
    # Spec (card 240088): for total tokens there is more space, so cached /
    # input / output each get their own bubble in the top bar.
    result = _run_bubbles_check(env)
    assert result["ok"], "token bubbles check failed: " + "; ".join(result["failures"])


def test_token_bubbles_check_script_is_hermetic():
    # The check script must exist next to the tests and boot any app.js path.
    assert HARNESS_JS.exists()
    src = HARNESS_JS.read_text()
    assert "renderStats" in src and "buildCard" in src
    assert "process.exit" in src
