"""Behavior tests for the editor's conflict handling (card 960bec).

Two people editing the same card must not silently overwrite each other. The
editor lives in `app/static/app.js`, so the reproducer boots the *served*
app.js under Node with a minimal DOM stub (`tests/editor_conflict_check.js`)
and drives it: openEditor remembers the loaded `updated_at`, saveCard sends it
as `expected_updated_at`, a 409 keeps the user's text and shows the conflict
box, "overwrite anyway" re-sends without it, "reload theirs" replaces the
fields.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "editor_conflict_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js editor")


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


def test_editor_handles_a_lost_race(env):
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"editor_conflict_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    assert result["ok"], "\n".join(result["failures"])


def test_editor_markup_has_the_conflict_box(env):
    """The editor offers both ways out of a conflict."""
    html = env.client.get("/").text
    assert 'id="edit-conflict"' in html
    assert 'id="btn-reload-theirs"' in html
    assert 'id="btn-overwrite"' in html
