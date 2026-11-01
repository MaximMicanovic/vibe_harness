"""Behavior tests for the token-counter hover tooltip (card 240088).

The tooltip logic lives in the served front-end `app/static/app.js`, so the
reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/token_tooltip_check.js`) and checks that hovering the top-bar token
counter and the per-card token chips shows the token types (cached, output,
input) from the API payloads. This fails on the original code (no hover
title carried the breakdown) and passes with the fix.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "token_tooltip_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js tooltip model")


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


def _run_tooltip_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"token_tooltip_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_token_counter_hover_shows_token_types(env):
    # Spec (card 240088): "in the counter when I hover my mouse on it I want
    # it to show me what types of tokens it was. Cached, output, input."
    result = _run_tooltip_check(env)
    assert result["ok"], "token tooltip check failed: " + "; ".join(result["failures"])


def test_token_tooltip_check_script_is_hermetic():
    # The check script must exist next to the tests and boot any app.js path.
    assert HARNESS_JS.exists()
    src = HARNESS_JS.read_text()
    assert "renderStats" in src and "buildCard" in src
    assert "process.exit" in src
