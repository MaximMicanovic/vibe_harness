"""Behavior test for the green-gate board UI (issue #14 part a) and the
force-accept button (part b).

The banner, the card warning chips and the Accept button's force payload live
in the served front-end `app/static/app.js`, so the check boots the *served*
app.js under Node with the shared DOM stub (`tests/gate_banner_check.js`) and
asserts what the browser would show: with `test_cmd_configured: false` the
#gate-banner and the settings warning are visible and a done card renders its
server `warnings` as chips; the Accept button sends `{"force": true}` for an
exhausted card and no body for an approved one.
"""
from __future__ import annotations

from pathlib import Path

from node_check import requires_node, run_node_check
from test_api import make_env

import pytest

TESTS_DIR = Path(__file__).resolve().parent
GATE_BANNER_JS = TESTS_DIR / "gate_banner_check.js"


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


@requires_node
def test_gate_banner_card_warnings_and_force_accept_button(env):
    result = run_node_check(env, GATE_BANNER_JS)
    assert result.get("ok") is True, result.get("failures")
