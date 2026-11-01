"""Behavior tests for the failure badge and the board's failure filter.

The badge and the filter live in the served front-end `app/static/app.js`, so
the reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/failure_badge_check.js`) and checks that a card carrying
`session.failure` shows the kind with the detail as a tooltip, and that the
board can be narrowed to one failure kind.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "failure_badge_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js failure badge model")


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


def _run_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"failure_badge_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_failure_badge_and_filter(env):
    result = _run_check(env)
    assert result["ok"], "failure badge check failed: " + "; ".join(result["failures"])


def test_the_board_offers_the_filter(env):
    """The filter control is part of the served page, next to the project picker."""
    html = env.client.get("/").text
    assert 'id="failure-filter"' in html


def test_check_script_is_hermetic():
    assert HARNESS_JS.exists()
    src = HARNESS_JS.read_text()
    assert "buildCard" in src and "visibleCards" in src
    assert "process.exit" in src
