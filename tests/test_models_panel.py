"""Behavior tests for the Models panel: one list, both platforms.

The panel lives in the served front-end (`app/static/app.js`), so the
reproducer boots the *served* app.js under Node with the shared DOM stub
(`tests/models_panel_check.js`) and checks that the panel has no Backend
dropdown, that the picker lists the Vibe models and the Claude aliases each
tagged with the backend that runs it, that a model of a missing CLI is disabled
with the reason, that saving a role sends `{role, model, backend}`, and that the
token counter follows the resolved backend rather than `settings.backend`.
"""
from __future__ import annotations

import pytest

from node_check import TESTS_DIR, requires_node, run_node_check
from test_api import make_env

HARNESS_JS = TESTS_DIR / "models_panel_check.js"

pytestmark = requires_node


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def test_models_panel_has_one_list_for_both_platforms(env):
    result = run_node_check(env, HARNESS_JS)
    assert result["ok"] is True, result["failures"]


def test_models_panel_check_covers_the_spec(env):
    """Guard against the reproducer silently checking nothing."""
    source = HARNESS_JS.read_text()
    for needle in ("backend-select", "OPTGROUP", "resolved_backend",
                   "put.role", "put.backend", "Claude Sonnet",
                   "Mistral Large 4", "not on PATH"):
        assert needle in source, needle


def test_the_backend_dropdown_is_gone_from_the_frontend(env):
    """No `#backend-select` row and no `saveBackend()` remain: the model choice
    is the only control for the platform."""
    js = env.client.get("/app.js").text
    assert "backend-select" not in js
    assert "saveBackend" not in js
    # ...while the per-role pickers and the accept-mode control stay.
    assert "function renderModelsPanel(" in js
    assert "accept-mode-select" in js
