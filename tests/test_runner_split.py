"""Structural tests for the app/runner.py split (issue #12).

`app/runner/` (was `app/runner.py`) used to hold everything (queue, review loop, accept/revert git
orchestration, process supervision) in one 3000-line module. The split moves
those concerns into cohesive modules as mixins — `app/queue.py`, `app/review.py`,
`app/accept.py`, `app/supervise.py`, plus `app/prompts.py` for the prompt
templates — while `app/runner/__init__.py` keeps the `SessionRunner` core and re-exports
every name the rest of the app (and the tests) import from it. These tests pin
that structure: the public `SessionRunner` API is unchanged and each concern
lives in its own module.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import app.runner as runner_mod
from app.runner import (  # noqa: F401  (re-export surface, pinned below)
    MCP_SERVER_PATH,
    REVIEW_DUPLICATION_MAX_CLONES,
    TOKEN_BREAKDOWN_KEYS,
    NotRunningError,
    SessionRunner,
    TestFailedError,
    WorkdirNotAllowed,
    fix_prompt_template,
    mcp_rules,
    prompt_template,
    resolve_prompt_template,
    resolve_workdir,
    review_prompt_template,
)

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Which SessionRunner methods must live in which module after the split.
METHOD_MODULES = {
    "app.runner.queue": {
        "_max_concurrent", "has_pending_work", "running_count",
        "_drain_queued_runs", "unmet_dependencies", "run_if_ready",
        "_drain_dependents", "_merge_in_progress_for", "_release_slot",
    },
    "app.runner.review": {
        "_read_verdict", "_review_diff", "_reviewed_commit", "_run_tests",
        "_green_gate", "_loop_reject",
    },
    "app.runner.accept": {
        "accept_mode", "accept_card", "_accept_card_locked", "_claim_accept_repo",
        "_release_accept_repo", "_resolver_owns_merge", "_queue_accept",
        "revert_card", "accept_all", "_start_accept_drain",
        "_start_accept_drain_locked", "_next_accept_queued", "_drain_accepts",
        "_drop_from_accept_queue", "_wait_for_resolve", "_accept_with_pr",
        "_start_resolver", "_spawn_resolver", "_finalize_resolver",
        "_record_resolver_outcome", "_conclude_resolved_merge",
        "_cancel_questions", "_recover_resolving",
    },
    "app.runner.supervise": {
        "backend", "_adopt_session", "_pid_alive", "_signal_session", "stop",
        "delete_card", "is_running", "_build_cmd", "_claude_mcp_config",
        "_watch_time_limit", "_spawn", "_next_round", "_read_export",
        "_model_error", "_finalize", "_record_outcome", "recover_stale",
        "log_tail", "activity", "_activity_session_id", "messages",
        "post_message",
    },
}

# Module-level helpers moved with the code; app.runner re-exports them.
PROMPT_NAMES = {
    "prompt_template", "fix_prompt_template", "review_prompt_template",
    "resolve_prompt_template", "parse_verdict", "mcp_rules", "tdd_rules",
    "reuse_rules", "mailbox_rules", "worker_rules", "duplication_section",
    "resolve_workdir", "now_iso", "export_token_breakdown",
    "merge_token_breakdown", "WorkdirNotAllowed",
}


def test_split_modules_exist():
    for mod in ["app.runner.prompts", "app.runner.queue", "app.runner.review", "app.runner.accept",
                "app.runner.supervise"]:
        importlib.import_module(mod)


def test_methods_live_in_their_module():
    for mod_name, methods in METHOD_MODULES.items():
        mod = importlib.import_module(mod_name)
        for name in methods:
            func = getattr(SessionRunner, name, None)
            assert func is not None, f"SessionRunner lost {name}"
            assert func.__module__ == mod_name, (
                f"{name} should live in {mod_name}, lives in {func.__module__}")
            # and the mixin class in that module defines it
            mixin = [c for c in vars(mod).values()
                     if isinstance(c, type) and name in vars(c)]
            assert mixin, f"{mod_name} has no class defining {name}"


def test_runner_keeps_the_core_and_the_public_api():
    # The core lifecycle stays on SessionRunner in app.runner.
    for name in ["run", "__init__", "live_usage", "annotate_card",
                 "transcripts_for", "_prepare_git", "_resolve_ws"]:
        func = getattr(SessionRunner, name)
        assert func.__module__ == "app.runner", (
            f"{name} should stay in app.runner, lives in {func.__module__}")
    # Every method resolves (the mixins are wired into the class).
    for methods in METHOD_MODULES.values():
        for name in methods:
            assert callable(getattr(SessionRunner, name))


def test_runner_reexports_the_old_import_surface():
    # Everything app/main.py and the tests imported from app.runner before the
    # split must still be importable from app.runner.
    for name in (PROMPT_NAMES | {
            "SessionRunner", "NotRunningError", "TestFailedError",
            "WorkdirNotAllowed", "MCP_SERVER_PATH", "TOKEN_BREAKDOWN_KEYS",
            "REVIEW_DUPLICATION_MAX_CLONES", "LIVE_USAGE_SCAN_INTERVAL",
            "prompt_template", "fix_prompt_template", "review_prompt_template",
            "resolve_prompt_template", "mcp_rules", "resolve_workdir"}):
        assert hasattr(runner_mod, name), f"app.runner no longer exports {name}"
    # Tests monkeypatch these through the app.runner namespace, so the module
    # must still expose them.
    assert hasattr(runner_mod, "subprocess")
    assert isinstance(runner_mod.LIVE_USAGE_SCAN_INTERVAL, float)


def test_prompts_module_owns_the_templates():
    prompts = importlib.import_module("app.runner.prompts")
    for name in PROMPT_NAMES:
        assert hasattr(prompts, name), f"app.prompts missing {name}"
        assert getattr(runner_mod, name) is getattr(prompts, name), (
            f"app.runner.{name} is not the app.prompts object")


def test_main_py_still_imports_the_same_names_from_app_runner():
    # main.py must not need edits: it still imports its runner names from
    # app.runner (not from the new modules).
    src = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert "from app.runner import" in src
    assert "from app.runner.queue import" not in src
    assert "from app.runner.accept import" not in src
    assert "from app.runner.supervise import" not in src
    assert "from app.runner.review import" not in src
    import app.main  # noqa: F401  (must still import cleanly)
