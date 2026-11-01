"""Real-browser tests for the archive drop-down fold (card b270d9, round 2).

Round 1 was rejected: the fold pinned a start height and changed it on the next
frame without forcing a style flush, so the browser could coalesce the two
style changes and the body popped open/closed instead of transitioning.

These tests drive the *served* front-end in headless Chromium and assert a REAL
transition, not inline style values:

- `transitionrun` / `transitionstart` / `transitionend` fire for `height`,
- the transition's `elapsedTime` shows it ran over time (not a pop),
- a style flush (an `offsetHeight` read) happens while the start height is
  pinned, which is what gives the transition its start value.

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
    "playwright.sync_api", reason="playwright is required for the real-browser fold test"
)

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "static"

ARCHIVE = {
    "id": "a1", "name": "Project pile", "type": "project", "description": "",
    "project": "Alpha", "created_at": "2026-10-30T10:00:00Z",
    "updated_at": "2026-10-30T12:00:00Z", "archived_projects": [],
    "path": "/tmp/archives/project-pile-a1", "item_count": 3,
}


def _card(card_id: str, title: str, project: str | None) -> dict:
    return {"id": card_id, "title": title, "project": project, "status": "accepted",
            "body": "b", "created_at": "2026-10-30T10:00:00Z",
            "updated_at": "2026-10-30T10:00:00Z", "archived": True,
            "archive": "a1", "archived_at": "2026-10-30T12:00:00Z"}


FULL = dict(ARCHIVE, items=[], projects=[
    {"name": "Alpha", "count": 2, "archived_at": None,
     "cards": [_card("alpha1", "Alpha one", "Alpha"), _card("alpha2", "Alpha two", "Alpha")]},
    {"name": "Beta", "count": 0, "archived_at": "2026-10-30T11:00:00Z", "cards": []},
    {"name": None, "count": 1, "archived_at": None, "cards": [_card("loose", "Loose card", None)]},
])

ROUTES = {
    "/api/archives": [ARCHIVE],
    "/api/archives/a1": FULL,
    "/api/cards": [],
    "/api/stacks": [],
    "/api/projects": [{"name": "Alpha"}],
    "/api/models": {},
    "/api/stats": {"total_tokens": 0, "total_runtime_seconds": 0,
                   "avg_runtime_seconds": 0, "token_breakdown": {}},
}

# Records every transition event on an archive body, and every offsetHeight read
# on one (the forced style flush) together with the inline height at that
# moment — so the test can tell *when* the flush happened.
INSTRUMENT = """() => {
  window.__fold = { events: [], flushes: [] };
  const isBody = (el) => el && el.classList && (
    el.classList.contains('archive-project-body') || el.classList.contains('archive-folder-body'));
  for (const type of ['transitionrun', 'transitionstart', 'transitionend']) {
    document.addEventListener(type, (e) => {
      if (!isBody(e.target)) return;
      window.__fold.events.push({ type, prop: e.propertyName, elapsed: e.elapsedTime });
    }, true);
  }
  const desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight');
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get() {
      const value = desc.get.call(this);
      if (isBody(this)) window.__fold.flushes.push({ inline: this.style.height, value });
      return value;
    },
  });
}"""


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

    def handle(route):
        path = route.request.url.split(static_server, 1)[-1].split("?")[0]
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(ROUTES.get(path, [])))

    page.route("**/api/**", handle)
    page.goto(static_server + "/index.html")
    page.wait_for_timeout(400)
    page.click('[data-panel="archive"]')
    page.wait_for_selector(".archive-project-head")
    page.evaluate(INSTRUMENT)
    try:
        yield page
    finally:
        page.close()


def _fold(page) -> dict:
    return page.evaluate("() => window.__fold")


def _height_events(fold: dict, kind: str) -> list[dict]:
    return [e for e in fold["events"] if e["prop"] == "height" and e["type"] == kind]


def test_fold_out_runs_a_real_height_transition(page):
    page.click(".archive-project-head")
    page.wait_for_timeout(600)
    fold = _fold(page)

    assert _height_events(fold, "transitionrun"), (
        "opening the drop-down must start a real height transition, got "
        + json.dumps(fold["events"]))
    assert _height_events(fold, "transitionstart"), (
        "the height transition must actually start, got " + json.dumps(fold["events"]))
    ends = _height_events(fold, "transitionend")
    assert ends, "the height transition must run to the end, got " + json.dumps(fold["events"])
    assert ends[0]["elapsed"] >= 0.1, (
        "the fold must take time instead of popping open, elapsedTime was "
        + str(ends[0]["elapsed"]))

    # The transition needs a start value: the body must be flushed while its
    # height is pinned to 0, before the target height is applied.
    assert any(f["inline"] == "0px" for f in fold["flushes"]), (
        "the fold-out must force a style flush while the start height is pinned "
        "(height: 0), got " + json.dumps(fold["flushes"]))


def test_fold_away_runs_a_real_height_transition(page):
    page.click(".archive-project-head")          # open it first
    page.wait_for_timeout(600)
    page.evaluate("() => { window.__fold = { events: [], flushes: [] }; }")

    page.click(".archive-project-head")          # and fold it away
    page.wait_for_timeout(600)
    fold = _fold(page)

    assert _height_events(fold, "transitionrun"), (
        "folding the drop-down away must start a real height transition, got "
        + json.dumps(fold["events"]))
    ends = _height_events(fold, "transitionend")
    assert ends, "the fold-away transition must run to the end, got " + json.dumps(fold["events"])
    assert ends[0]["elapsed"] >= 0.1, (
        "the fold-away must take time instead of popping shut, elapsedTime was "
        + str(ends[0]["elapsed"]))

    # The pinned start height is the body's natural height, not 0.
    assert any(f["inline"] not in ("", "0px") for f in fold["flushes"]), (
        "the fold-away must force a style flush while the start height is pinned, got "
        + json.dumps(fold["flushes"]))

    assert page.evaluate("() => !document.querySelector('.archive-project-body')"), (
        "the folded body leaves the DOM when the transition ends")


def test_archive_folder_level_also_transitions(page):
    page.click(".archive-project-head")          # open the top level
    page.wait_for_timeout(600)
    page.evaluate("() => { window.__fold = { events: [], flushes: [] }; }")

    page.click(".archive-folder-head")           # open the archive folder
    page.wait_for_timeout(600)
    fold = _fold(page)

    assert _height_events(fold, "transitionrun"), (
        "the archive folder must fold out with a real height transition, got "
        + json.dumps(fold["events"]))
    assert any(f["inline"] == "0px" for f in fold["flushes"]), (
        "the archive folder fold-out must force a style flush while pinned at height 0, got "
        + json.dumps(fold["flushes"]))
