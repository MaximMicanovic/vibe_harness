"""Behavior tests for the project menu in the served front-end (card 239e2d).

The "＋ Project" button used to sit in the top bar, next to the project
dropdown. It now lives *inside* that dropdown, as the last entry: picking
"Add project…" opens the projects modal, where a project is added.

The menu lives in `app/static/app.js`, so the reproducer boots the *served*
app.js under Node with the shared DOM stub (`tests/dom_stub.js`) and drives it:
the menu lists All projects + every project + the add entry last, picking the
add entry opens the modal without touching the board filter, and a project
added there shows up in the menu.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "project_menu_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js project menu")


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


def _run_project_menu_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"project_menu_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_project_menu_behaves(env):
    result = _run_project_menu_check(env)
    assert result["ok"], "project menu check failed: " + "; ".join(result["failures"])


def test_top_bar_has_no_separate_add_project_button(env):
    """The button moved into the dropdown — the top bar must not keep a copy."""
    html = env.client.get("/").text
    assert 'id="project-select"' in html
    assert "manage-projects-btn" not in html
    assert "＋ Project" not in html
