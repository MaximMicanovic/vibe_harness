"""Behavior tests for the archive browser's drop-down animation (card b270d9).

Spec: "Make a nice animation for the drop down menu on the archive menu."

The archive browser (the sidebar's Archive panel) is a tree of drop-downs:
project header -> archive folder -> project group. Each level folds out under
its header instead of the whole panel being rebuilt, so the fold is animated:
the body starts at height 0, grows to its natural height on the next frame
(CSS transitions the height), and folds back the same way — the cards leave the
DOM at once, the space collapses smoothly, and the body is dropped when the
transition ends. The chevron rotates instead of swapping glyphs.

The browser logic lives in the served front-end `app/static/app.js`, so the
reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/archive_drop_animation_check.js`) and drives the fold-out/fold-away.
The animation itself is CSS in `app/static/style.css`, checked separately.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "archive_drop_animation_check.js"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js archive drop-downs")


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


def _run_drop_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"archive_drop_animation_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_archive_dropdowns_fold_out_and_away(env):
    result = _run_drop_check(env)
    assert result["ok"], "archive drop-down check failed: " + "; ".join(result["failures"])


def _served_css(env) -> str:
    r = env.client.get("/style.css")
    assert r.status_code == 200, r.text
    return r.text


def _rules(css: str, selector: str) -> list[str]:
    """The declaration blocks of every rule whose selector list contains it."""
    out = []
    for m in re.finditer(r"([^{}]+)\{([^}]*)\}", css):
        if selector in m.group(1):
            out.append(m.group(2))
    assert out, f"style.css has no rule for {selector}"
    return out


def test_style_defines_the_drop_animation(env):
    css = _served_css(env)

    # One duration drives the fold and the chevron turn.
    assert re.search(r"--archive-drop-ms:\s*\d+m?s", css), \
        "style.css should define --archive-drop-ms (the fold duration)"

    # The body transitions its height while it folds.
    for selector in (".archive-drop--open", ".archive-drop--closing"):
        blocks = _rules(css, selector)
        assert any("transition" in b and "height" in b for b in blocks), \
            f"{selector} should transition its height"
        assert any("overflow: hidden" in b for b in blocks), \
            f"{selector} should clip its content while it folds"

    # The fold fades as well as slides, so it reads as a drop-down.
    assert re.search(r"@keyframes\s+archive-drop-fade", css), \
        "style.css should define the archive-drop-fade keyframes"
    assert any("archive-drop-fade" in b for b in _rules(css, ".archive-drop--open")), \
        ".archive-drop--open should run the fade keyframes"

    # The chevron rotates instead of swapping glyphs.
    assert any("transition" in b and "transform" in b for b in _rules(css, ".archive-chevron")), \
        ".archive-chevron should transition its rotation"
    assert any("rotate(90deg)" in b for b in _rules(css, ".archive-chevron--open")), \
        ".archive-chevron--open should rotate the chevron"

    # Motion is a preference: honour it.
    assert re.search(r"@media\s*\(prefers-reduced-motion:\s*reduce\)", css), \
        "style.css should honour prefers-reduced-motion"
