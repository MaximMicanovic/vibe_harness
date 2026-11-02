"""The growth signal on a card: the `+412 −38` diff-stat chip (card 26671e).

Spec: "Record `session.diff_stat` (lines added / removed ...) when a run
finalizes. Show it as a card chip (e.g. `+412 −38`). A card that adds 900 lines
for a small spec becomes visible at a glance instead of only in review."

The chip is rendered by the served front-end `app/static/app.js`, so the check
boots the *served* app.js under Node with a minimal DOM stub
(`tests/diff_stat_chip_check.js`) and asserts what a card renders.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "diff_stat_chip_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js diff-stat chip")


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def _run_chip_check(env) -> dict:
    r = env.client.get("/app.js")
    assert r.status_code == 200, r.text
    app_js = env.tmp_path / "app.js"
    app_js.write_text(r.text)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"diff_stat_chip_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_card_shows_its_diff_stat(env):
    result = _run_chip_check(env)
    assert result["ok"], "diff-stat chip check failed: " + "; ".join(result["failures"])


def test_diff_stat_chip_is_styled(env):
    """The chip has a style of its own, so it reads as a signal, not as one
    more grey pill."""
    css = env.client.get("/style.css").text
    assert ".chip-diff-stat" in css


def test_diff_stat_chip_check_script_is_hermetic():
    assert HARNESS_JS.exists()
    src = HARNESS_JS.read_text()
    assert "buildCard" in src and "process.exit" in src
