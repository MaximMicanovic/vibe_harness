"""Real-browser tests for the live activity popup (card e02cd4).

The node harness (`tests/activity_popup_check.js`) checks the popup's logic
under a DOM stub; these drive the *served* front-end in headless Chromium, so
the parts a stub cannot see are covered too: the panel really floats next to
the card (a fixed box, not a full-screen overlay), the feed really scrolls, and
the C + click gesture works against real key events.

Skipped when Playwright or its Chromium build is not installed.
"""
from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sync_api = pytest.importorskip(
    "playwright.sync_api", reason="playwright is required for the real-browser popup test"
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"

CARD = {
    "id": "card-1", "title": "Live card", "body": "watch me work", "status": "running",
    "created_at": "2026-11-02T10:00:00Z", "updated_at": "2026-11-02T10:00:00Z",
    "project": None, "workdir": None, "depends_on": [], "parent_id": None,
    "model": None, "auto_run": False, "auto_accept": False, "accepted": False,
    "questions": [],
    "session": {"started_at": "2026-11-02T10:00:00Z", "live_tokens": 1234,
                "pid": 4242},
}

#: The activity feed the page sees: the first poll carries the assistant text
#: and a tool call, every later poll one more line of text.
FIRST_FEED = {"cursor": 2, "events": [
    {"kind": "text", "at": "2026-11-02T10:00:01Z", "text": "I will run the tests."},
    {"kind": "tool", "at": "2026-11-02T10:00:02Z", "tool": "file_system.bash",
     "summary": "pytest -q"},
]}
LATER_FEED = {"cursor": 3, "events": [
    {"kind": "text", "at": "2026-11-02T10:00:03Z", "text": "the suite is green"},
]}

ROUTES = {
    "/api/cards": [CARD],
    "/api/stacks": [],
    "/api/projects": [],
    "/api/models": {},
    "/api/settings": {},
    "/api/version": {"version": "test"},
    "/api/templates": [],
    "/api/archives": [],
    "/api/stats": {"total_tokens": 0, "total_runtime_seconds": 0,
                   "avg_runtime_seconds": 0, "token_breakdown": {}},
}


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass


@pytest.fixture(scope="module")
def static_server():
    handler = partial(_QuietHandler, directory=str(STATIC_DIR))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            launched = p.chromium.launch()
        except Exception as exc:  # no browser build installed
            pytest.skip(f"chromium is not available: {exc}")
        try:
            yield launched
        finally:
            launched.close()


@pytest.fixture()
def page(browser, static_server):
    page = browser.new_page(viewport={"width": 1280, "height": 900})
    polls = {"n": 0}

    def handle(route):
        path = route.request.url.split(static_server, 1)[-1].split("?")[0]
        if path == "/api/cards/card-1/activity":
            polls["n"] += 1
            body = FIRST_FEED if polls["n"] == 1 else LATER_FEED
        else:
            body = ROUTES.get(path, [])
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", handle)
    page.goto(static_server + "/index.html")
    page.wait_for_selector(".card")
    try:
        yield page
    finally:
        page.close()


def _open_with_c(page) -> None:
    page.keyboard.down("c")
    page.click(".card-title")
    page.keyboard.up("c")
    page.wait_for_selector("#activity-popup:not([hidden])")


def test_hold_c_and_click_opens_a_floating_popup(page):
    _open_with_c(page)
    popup = page.locator("#activity-popup")
    assert popup.is_visible()

    box = popup.bounding_box()
    assert box is not None
    # A small panel, not a full-screen overlay: it covers a fraction of the
    # viewport and sits inside it.
    assert box["width"] <= 420 and box["height"] <= 520
    assert box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= 1280
    assert box["y"] + box["height"] <= 900
    # ...and the board behind it is still there (no backdrop).
    assert page.locator(".card").is_visible()

    # The header carries the card title, its status and the live token count.
    assert "Live card" in page.inner_text("#activity-title")
    assert "running" in page.inner_text("#activity-status").lower()
    assert "1.2k" in page.inner_text("#activity-tokens")

    # The feed shows the assistant text and the tool call.
    feed = page.inner_text("#activity-feed")
    assert "I will run the tests." in feed
    assert "file_system.bash" in feed
    assert "pytest -q" in feed


def test_the_popup_polls_new_events(page):
    _open_with_c(page)
    page.wait_for_function(
        "() => document.getElementById('activity-feed').textContent.includes('the suite is green')",
        timeout=5000,
    )


def test_plain_click_still_opens_the_editor(page):
    page.click(".card-title")
    page.wait_for_selector("#editor-overlay:not([hidden])")
    assert page.locator("#activity-popup").is_hidden()


def test_esc_closes_the_popup(page):
    _open_with_c(page)
    page.keyboard.press("Escape")
    page.wait_for_function(
        "() => document.getElementById('activity-popup').hidden", timeout=2000)
    assert page.locator("#activity-popup").is_hidden()


def test_scrolling_up_offers_jump_to_latest(page):
    _open_with_c(page)
    # Fill the feed so it really overflows, then scroll to the top.
    page.evaluate("""() => {
      const feed = document.getElementById('activity-feed');
      for (let i = 0; i < 60; i++) {
        const row = document.createElement('div');
        row.className = 'activity-event activity-event--text';
        row.textContent = 'line ' + i;
        feed.appendChild(row);
      }
      feed.scrollTop = 0;
      feed.dispatchEvent(new Event('scroll'));
    }""")
    jump = page.locator("#activity-jump")
    assert jump.is_visible()
    jump.click()
    page.wait_for_function(
        "() => { const f = document.getElementById('activity-feed');"
        " return f.scrollTop + f.clientHeight >= f.scrollHeight - 2; }",
        timeout=2000,
    )
    assert jump.is_hidden()
