"""Behavior tests for the diff panel and the Keys panel (card b4e872).

The panel logic lives in the served front-end `app/static/app.js`, so the
reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/diff_panel_check.js`) and checks that a diff renders line by line with
the right classifier, that every line is a TEXT NODE (the content is
model-written — never innerHTML), that the verdict sits next to the diff, and
that the Keys panel lists N, Esc and D from the one KEYBINDINGS list.
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
                   "scrolledIntoView", "not git-backed"):
        assert needle in source, needle
