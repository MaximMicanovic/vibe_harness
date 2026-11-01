"""Reproducer for card a64cea: updating the token count must not scroll the
window to the top.

While any card is live, the dashboard polls `refresh()` every 2 s — that poll
is what updates the token count — and `renderBoard()` tears down every board
column before rebuilding the cards. The board re-render must keep the window's
scroll position.

- `test_token_count_poll_keeps_window_scroll` runs the behavioral reproducer
  `tests/token_scroll_repro.cjs` (the real `app/static/app.js` in a stub DOM;
  it exits 1 when a token-count poll resets the window scroll to the top).
- `test_board_rerender_pins_scroll_in_served_assets` asserts the shipped
  `app.js`/`style.css` carry the fix: `renderBoard()` pins the window scroll
  across the rebuild and the document opts out of CSS scroll anchoring.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_token_count_poll_keeps_window_scroll():
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node is required for the app.js stub-DOM reproducer")
    r = subprocess.run(
        [node, str(ROOT / "tests" / "token_scroll_repro.cjs")],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, (
        "token-count poll scrolled the window to the top:\n"
        f"stdout: {r.stdout}\nstderr: {r.stderr}"
    )


def test_board_rerender_pins_scroll_in_served_assets():
    # StaticFiles serves app/static verbatim, so reading the files is the same
    # as GET /app.js and GET /style.css.
    js = (ROOT / "app" / "static" / "app.js").read_text()
    m = re.search(r"function renderBoard\(\) \{(.*?)\n\}", js, re.S)
    assert m, "renderBoard() not found in app.js"
    body = m.group(1)
    # The window scroll offset is captured before the columns are torn down
    # and restored after the rebuild, so the 2 s live poll (which updates the
    # token count) cannot yank the window to the top.
    assert re.search(r"window\.scrollY", body), (
        "renderBoard() must capture window.scrollY before rebuilding the board")
    assert re.search(r"window\.scrollTo\(", body), (
        "renderBoard() must restore the window scroll after rebuilding the board")

    css = (ROOT / "app" / "static" / "style.css").read_text()
    assert re.search(r"overflow-anchor:\s*none", css), (
        "style.css must opt the document out of scroll anchoring, which "
        "re-anchors (and can jump to the top) when the polled board re-render "
        "replaces the anchored card nodes")
