"""Behavior tests for desktop notifications on card completion (card 66effc).

Spec: "Tell me when a card finishes while I am in another window."

The notification logic lives in the served front-end `app/static/app.js`, so the
reproducer boots the *served* app.js under Node with a minimal DOM stub
(`tests/notifications_check.js`) and drives `refresh()` through status
transitions. It fails on the code that had no notification code at all and
passes with the status diff, the Notification/WebAudio fallback, the title
badge and the persisted Settings toggle.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_api import make_env

TESTS_DIR = Path(__file__).resolve().parent
HARNESS_JS = TESTS_DIR / "notifications_check.js"
DOCS = TESTS_DIR.parent / "docs" / "how-it-works.md"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node is required to run the app.js notification model")


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


def _run_notifications_check(env) -> dict:
    app_js = _served_app_js(env)
    proc = subprocess.run(
        [node, str(HARNESS_JS), str(app_js)],
        capture_output=True, text=True, timeout=60,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"notifications_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )


def test_finished_card_raises_a_desktop_notification(env):
    # Spec (card 66effc): a card reaching done/failed/stopped from
    # running/reviewing produces a desktop notification naming the card and
    # its outcome — but not on the first load and not for the open card.
    result = _run_notifications_check(env)
    assert result["ok"], "notifications check failed: " + "; ".join(result["failures"])


def test_title_badge_and_settings_toggle(env):
    # Spec (card 66effc): "(N) vibe-harness" while unread and hidden, cleared
    # on focus; the sidebar Settings toggle persists in localStorage.
    result = _run_notifications_check(env)
    assert result["ok"], "notifications check failed: " + "; ".join(result["failures"])


def test_settings_panel_is_reachable_from_the_sidebar(env):
    # The toggle lives in a Settings panel, so the rail needs a button for it.
    html = env.client.get("/").text
    assert 'data-panel="settings"' in html
    js = env.client.get("/app.js").text
    assert "settings: { title: 'Settings'" in js
    assert "renderSettingsPanel" in js


def test_notification_permission_is_never_requested_on_load(env):
    # Spec (card 66effc): request permission on the first user gesture (a
    # click), never on load.
    js = env.client.get("/app.js").text
    assert "Notification.requestPermission" in js
    assert "addEventListener('click', askNotificationPermission" in js


def test_how_it_works_documents_notifications(env):
    # Spec (card 66effc): update docs/how-it-works.md.
    text = DOCS.read_text()
    assert "notification" in text.lower()
    assert "vibe-harness:notifications" in text


def test_notifications_check_script_is_hermetic():
    # The check script must exist next to the tests and boot any app.js path.
    assert HARNESS_JS.exists()
    src = HARNESS_JS.read_text()
    assert "refresh" in src and "Notification" in src
    assert "process.exit" in src
