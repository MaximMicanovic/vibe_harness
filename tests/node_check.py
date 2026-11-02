"""Run one of the node checks in this folder against the *served* app.js.

Every front-end check boots the app.js the browser actually gets — fetched
through the API, not read from disk — so a check can never pass against a file
the server does not serve. `run_node_check` returns the JSON the check prints
(`{ok, failures}`); a check that crashes or prints something else fails loudly
instead of silently passing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

node = shutil.which("node")

#: Skip marker for a module whose checks need node.
requires_node = pytest.mark.skipif(
    node is None, reason="node is required to run the app.js checks")


def served_app_js(env) -> Path:
    """Fetch app.js through the API (what the browser actually gets)."""
    r = env.client.get("/app.js")
    assert r.status_code == 200, r.text
    out = env.tmp_path / "app.js"
    out.write_text(r.text)
    return out


def run_node_check(env, script: Path) -> dict:
    """Boot the served app.js under `script` and return its JSON verdict."""
    app_js = served_app_js(env)
    proc = subprocess.run(
        [node, str(script), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"{script.name} did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
