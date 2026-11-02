"""Behavior tests for steering a running session from the chat popup (card f7462f).

The popup lives in the served front-end `app/static/app.js`, so the reproducer
boots the *served* app.js under Node with a minimal DOM stub
(`tests/steering_check.js`) and checks that the footer input + Send posts the
message and shows it immediately, that worker replies appear as replies, that
reviewer traffic stays out, that the input is disabled with a hint while the
card is not running, and that every message is a text node.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "steering_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js steering model")


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


def _run_steering_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"steering_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_steering_from_the_popup_works(env):
    result = _run_steering_check(env)
    assert result["ok"] is True, result["failures"]


def test_steering_check_covers_the_spec(env):
    """Guard against the reproducer silently checking nothing."""
    source = HARNESS_JS.read_text()
    for needle in ("sendActivityMessage", "activity-message--user",
                   "activity-message--worker", "not running", "innerHTML",
                   "activity-compose"):
        assert needle in source, needle


def test_popup_markup_has_the_compose_footer(env):
    html = env.client.get("/").text
    assert 'id="activity-compose"' in html
    assert 'id="activity-input"' in html
    assert 'id="activity-send"' in html
    assert 'id="activity-hint"' in html
    # The hint is the spec's wording.
    assert "this card is not running" in html


def test_popup_styles_cover_the_compose_footer(env):
    css = env.client.get("/style.css").text
    for selector in (".activity-compose", ".activity-input", ".activity-send",
                     ".activity-hint", ".activity-message--user",
                     ".activity-message--worker"):
        assert selector in css, selector
