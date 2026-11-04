"""Code docstrings must describe the merged behaviour, not the old one.

Report §6 fixes the markdown docs (owned by the docs card); this file pins
the *code-side* half of the same claims — module/function docstrings and the
MCP tool descriptions the server publishes — so they cannot keep describing
the pre-merge behaviour:

- four roles incl. ``orchestrator`` (app/models.py), and settings.json's
  ``backend`` key reconciled as the fallback, not denied;
- the run workdir is the per-card linked worktree ``sessions/<id>/worktree``
  (sandbox cards: ``sessions/<id>/workspace``), not the project folder
  (app/runner/__init__.py);
- ``/api/duplication`` scans the harness root (BASE_DIR), not the selected
  project (app/main.py);
- the verdict channel is primarily ``submit_verdict``
  (``verdicts/review-<n>.json``); ``VERDICT:`` text is the fallback
  (app/runner/review.py);
- ``update_card`` resets a finished card to draft only on a title/body
  change (the served tool description);
- ``ask_user`` returns the raw question record (key ``id``) on the
  answered/cancelled path (the tool docstring).

The behavioural side of §6 (nine failure kinds, MAX_LIMIT=500, the
force/exhausted accept gate, the opt-in test gate, ...) is already pinned by
tests/test_failures.py, tests/test_activity.py, tests/test_cards.py, ... —
only the docstrings drifted.
"""
from __future__ import annotations

import re

from app import main, models, runner
from app.mcp_server import TOOLS, tool_ask_user
from app.runner.review import ReviewMixin


def _tool(name: str) -> dict:
    return next(t for t in TOOLS if t["name"] == name)


def _endpoint_doc(path: str) -> str:
    for route in main.app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint.__doc__ or ""
    raise AssertionError(f"no route for {path}")


def test_models_docstring_lists_four_roles_including_orchestrator():
    doc = models.__doc__ or ""
    assert "three kinds of session" not in doc
    for role in ("worker", "review", "resolver", "orchestrator"):
        assert role in doc, f"app.models docstring does not mention the {role} role"


def test_models_docstring_reconciles_the_settings_backend_key():
    # settings.json DOES carry a `backend` key: it is the internal fallback
    # for a role with no model picked — not a second global switch, and not
    # something the docstring may deny exists.
    doc = models.__doc__ or ""
    assert "no separate global backend control" not in doc
    assert "fallback" in doc


def test_runner_docstring_says_linked_worktree_not_project_folder():
    doc = runner.__doc__ or ""
    assert "sessions/<card_id>/worktree" in doc
    # The old claim — running in the card's project folder — must be gone.
    assert "or the card's project folder" not in doc
    # Sandbox cards (no project) still run in the isolated workspace.
    assert "sessions/<card_id>/workspace" in doc


def test_duplication_docstring_says_harness_root_not_selected_project():
    doc = _endpoint_doc("/api/duplication")
    assert "project" not in doc.lower() or "harness root" in doc.lower()
    assert "harness root" in doc.lower() or "BASE_DIR" in doc
    # app/runner.py no longer exists (split into the app/runner package).
    assert "`app/runner.py`" not in doc


def test_read_verdict_docstring_marks_verdict_text_as_the_fallback():
    doc = ReviewMixin._read_verdict.__doc__ or ""
    assert "fallback" in doc.lower()
    assert "submit_verdict" in doc


def test_update_card_description_qualifies_the_draft_reset():
    desc = _tool("update_card")["description"]
    # A finished card resets to draft ONLY on a title/body change;
    # settings-only edits (auto_run/auto_accept) must not reset it.
    assert re.search(r"only.{0,40}title.{0,10}body|title.{0,10}body.{0,40}only",
                     desc, re.I), desc


def test_ask_user_docstring_documents_the_raw_record_id_key():
    doc = re.sub(r"\s+", " ", tool_ask_user.__doc__ or "")
    # On the answered/cancelled path the tool returns the raw question
    # record, whose key is `id` — not `question_id`.
    assert "raw question record" in doc
    assert re.search(r"`id`", doc)
    assert "question_id" in doc  # the timeout summary key, named as such
