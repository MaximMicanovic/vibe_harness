"""Behavior tests for search in the served front-end (card 49f4ec).

The search UI lives in `app/static/app.js`, so the reproducer boots the
*served* app.js under Node with a minimal DOM stub (`tests/search_ui_check.js`)
and drives it: typing filters the board, the results list groups hits by
location, "/" focuses, Esc clears, and clicking a result leads somewhere.

Spec: "Find a card by text, on the board and in the archives." Board search is
client-side (the cards are already in the browser); archive search asks the
server (GET /api/search).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "search_ui_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js search UI")


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


def _run_search_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"search_ui_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_search_ui_behaves(env):
    result = _run_search_check(env)
    assert result["ok"], "search UI check failed: " + "; ".join(result["failures"])


def test_top_bar_has_a_search_input(env):
    html = env.client.get("/").text
    assert 'id="search-input"' in html
    assert 'id="search-results"' in html
    assert 'id="search-hint"' in html


def test_search_styles_are_served(env):
    css = env.client.get("/style.css").text
    for selector in (".search-box", ".search-results", ".search-hit", ".archive-item--hit"):
        assert selector in css, f"{selector} is missing from style.css"
