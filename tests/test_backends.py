"""Tests for the session backends: which agent CLI the harness drives.

The harness is a board, not an agent. A backend owns three things — the command
line, where its output lands, and how to normalize that output into the shape
the rest of the harness speaks (`usage`, `session_id`, `config.active_model`,
`stop_reason`). Everything downstream is backend-agnostic.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.backends import (
    BACKENDS,
    DEFAULT_BACKEND,
    BackendError,
    ClaudeBackend,
    VibeBackend,
    describe_backends,
    get_backend,
)
from app.config import Config
from app.main import create_app
from app.cards import CardStore
from app.projects import ProjectStore
from app.runner import SessionRunner
from app.settings import SettingsStore

FAKE_VIBE = "#!/bin/sh\nexit 0\n"


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(vibe_bin=str(tmp_path / "vibe"), claude_bin=str(tmp_path / "claude"),
                  time_limit_seconds=30)


# ----------------------------------------------------------------------
# the registry
# ----------------------------------------------------------------------

def test_both_backends_are_registered():
    assert set(BACKENDS) == {"vibe", "claude"}
    assert DEFAULT_BACKEND == "vibe"


def test_unknown_backend_falls_back_to_the_default():
    assert get_backend("nope").key == DEFAULT_BACKEND
    assert get_backend(None).key == DEFAULT_BACKEND
    assert get_backend("CLAUDE").key == "claude"


def test_describe_reports_availability(config):
    described = {b["key"]: b for b in describe_backends(config)}
    assert described["vibe"]["label"] == "Mistral Vibe"
    assert described["claude"]["label"] == "Claude Code"
    # Neither fake binary exists, so neither is available.
    assert described["vibe"]["available"] is False
    assert described["claude"]["available"] is False


# ----------------------------------------------------------------------
# the command lines
# ----------------------------------------------------------------------

def test_vibe_command_carries_its_own_flags(config, tmp_path):
    cmd = VibeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="run",
                                  resume=None, mcp_config=None)
    assert cmd[0] == config.vibe_bin
    assert "--prompt-file" in cmd and "--output-dir" in cmd
    assert "--time-limit" in cmd
    assert "--auto-approve" in cmd
    # Vibe enforces its own time limit; the runner must not double up.
    assert VibeBackend().native_time_limit is True
    assert VibeBackend().prompt_on_stdin is False


def test_vibe_review_uses_the_read_only_agent(config, tmp_path):
    cmd = VibeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="review",
                                  resume=None, mcp_config=None)
    assert "--agent" in cmd and config.review_agent in cmd


def test_claude_command_is_print_mode_json(config, tmp_path):
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume=None, mcp_config=None)
    assert cmd[0] == config.claude_bin
    assert cmd[1:3] == ["-p", "--output-format"]
    assert "json" in cmd
    # Claude has no --time-limit: the runner watches the clock instead.
    assert ClaudeBackend().native_time_limit is False
    assert ClaudeBackend().prompt_on_stdin is True
    assert "--time-limit" not in cmd


def test_claude_run_is_unattended(config, tmp_path):
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume=None, mcp_config=None)
    assert "--permission-mode" in cmd
    assert "bypassPermissions" in cmd


def test_claude_review_cannot_write(config, tmp_path):
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="review",
                                    resume=None, mcp_config=None)
    tools = cmd[cmd.index("--tools") + 1]
    assert "Read" in tools and "Grep" in tools
    # The whole point of the review pass: it cannot edit what it judges.
    assert "Edit" not in tools and "Write" not in tools


def test_claude_resume_and_budget(config, tmp_path):
    config.max_price_usd = 2.5
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume="abc-123", mcp_config=None)
    assert "--resume" in cmd and "abc-123" in cmd
    assert "--max-budget-usd" in cmd and "2.5" in cmd


def test_claude_mcp_config_flag(config, tmp_path):
    mcp = tmp_path / "mcp.json"
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume=None, mcp_config=mcp)
    assert "--mcp-config" in cmd and str(mcp) in cmd


# ----------------------------------------------------------------------
# an allowlist must not hide the board MCP tools (on either backend)
# ----------------------------------------------------------------------

def _enabled_tools_flags(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, c in enumerate(cmd) if c == "--enabled-tools"]


def test_vibe_allowlist_keeps_the_board_mcp_tools(config, tmp_path):
    config.enabled_tools = ["Read", "Bash"]
    cmd = VibeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="run",
                                  resume=None, mcp_config=None)
    flags = _enabled_tools_flags(cmd)
    assert "Read" in flags and "Bash" in flags
    assert f"{config.mcp_server_name}_*" in flags


def test_claude_allowlist_keeps_the_board_mcp_tools(config, tmp_path):
    """Claude names MCP tools `mcp__<server>__<tool>`, so the allowlist entry
    that keeps the board tools alive is the `mcp__<name>__*` equivalent of
    Vibe's `<name>_*`."""
    config.enabled_tools = ["Read", "Bash"]
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume=None, mcp_config=None)
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert tools[:2] == ["Read", "Bash"]
    assert f"mcp__{config.mcp_server_name}__*" in tools


def test_claude_allowlist_uses_the_configured_server_name(config, tmp_path):
    config.enabled_tools = ["Read"]
    config.mcp_server_name = "board"
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume=None, mcp_config=None)
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert "mcp__board__*" in tools
    assert "mcp__harness__*" not in tools


def test_claude_allowlist_without_the_mcp_adds_no_pattern(config, tmp_path):
    config.enabled_tools = ["Read"]
    config.mcp_enabled = False
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume=None, mcp_config=None)
    tools = cmd[cmd.index("--allowedTools") + 1].split(",")
    assert tools == ["Read"]


def test_claude_review_never_gets_allowed_tools(config, tmp_path):
    """The review pass is read-only via --tools, not via an allowlist."""
    config.enabled_tools = ["Read"]
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="review",
                                    resume=None, mcp_config=None)
    assert "--allowedTools" not in cmd


# ----------------------------------------------------------------------
# APPROVE: a typo must fail loudly, not silently downgrade the approval mode
# ----------------------------------------------------------------------

def test_vibe_rejects_an_unrecognised_approve_value(config, tmp_path):
    config.approve = "atuo"  # typo: must not silently become --smart-approve
    with pytest.raises(BackendError, match="APPROVE"):
        VibeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                ws=tmp_path, rdir=tmp_path, kind="run",
                                resume=None, mcp_config=None)


def test_vibe_smart_approve_is_an_explicit_value(config, tmp_path):
    config.approve = "smart"
    cmd = VibeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="run",
                                  resume=None, mcp_config=None)
    assert "--smart-approve" in cmd
    assert "--auto-approve" not in cmd


def test_claude_run_rejects_an_unrecognised_approve_value(config, tmp_path):
    config.approve = "yes-please"
    with pytest.raises(BackendError, match="APPROVE"):
        ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="run",
                                  resume=None, mcp_config=None)


def test_claude_review_keeps_default_permission_mode_for_any_approve(config, tmp_path):
    """Review sessions are always --permission-mode default regardless of
    APPROVE — that behaviour is kept, so a stray value must not error there."""
    config.approve = "yes-please"
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="review",
                                    resume=None, mcp_config=None)
    assert cmd[cmd.index("--permission-mode") + 1] == "default"


# ----------------------------------------------------------------------
# REVIEW_AGENT is a Vibe-only flag (--agent): on Claude it must not be
# silently ignored
# ----------------------------------------------------------------------

def test_claude_review_rejects_a_configured_review_agent(config, tmp_path):
    config.review_agent = "my-reviewer"
    with pytest.raises(BackendError, match="REVIEW_AGENT"):
        ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="review",
                                  resume=None, mcp_config=None)


def test_claude_review_runs_with_the_default_review_agent(config, tmp_path):
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="review",
                                    resume=None, mcp_config=None)
    assert cmd[cmd.index("--permission-mode") + 1] == "default"
    assert "--tools" in cmd


def test_vibe_review_uses_the_configured_review_agent(config, tmp_path):
    config.review_agent = "my-reviewer"
    cmd = VibeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="review",
                                  resume=None, mcp_config=None)
    assert cmd[cmd.index("--agent") + 1] == "my-reviewer"


# ----------------------------------------------------------------------
# the time limit: native on Vibe, watched by the harness on Claude — never both
# ----------------------------------------------------------------------

def test_time_limit_is_watched_only_without_a_native_limit(tmp_path, monkeypatch):
    """One enforcement per spawn: Vibe gets --time-limit and no watcher;
    Claude gets no flag and exactly one harness watcher (not double-applied)."""
    for key, expect_watch in (("vibe", False), ("claude", True)):
        store = CardStore(tmp_path / f"cards-{key}")
        settings = SettingsStore(tmp_path / f"settings-{key}.json")
        settings.set_backend(key)
        config = Config(vibe_bin=str(tmp_path / "vibe"),
                        claude_bin=str(tmp_path / "claude"),
                        time_limit_seconds=30, mailbox=False)
        runner = SessionRunner(store, config,
                               sessions_dir=tmp_path / f"sessions-{key}",
                               settings=settings)
        watched: list[str] = []
        monkeypatch.setattr(runner, "_watch_time_limit",
                            lambda card_id, proc: watched.append(card_id))
        captured: dict = {}

        class FakeProc:
            pid = 4242
            def poll(self):
                return 0
            def wait(self, timeout=None):
                return 0

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProc()

        monkeypatch.setattr("app.runner.subprocess.Popen", fake_popen)
        monkeypatch.setattr(runner, "_finalize", lambda *a, **k: None)
        card = store.create("C", "spec")
        runner._spawn(card["id"], kind="run", prompt="P")
        if expect_watch:
            assert watched == [card["id"]]
            assert "--time-limit" not in captured["cmd"]
        else:
            assert watched == []
            assert "--time-limit" in captured["cmd"]


# ----------------------------------------------------------------------
# the model: a flag for Claude, an env var for Vibe
# ----------------------------------------------------------------------

def test_claude_command_carries_the_model(config, tmp_path):
    """The picker's choice must reach the CLI: Claude takes `--model`."""
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume=None, mcp_config=None,
                                    model="claude-sonnet-4-6")
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_claude_command_without_a_model_has_no_flag(config, tmp_path):
    """A role left on inherit adds nothing: Claude keeps its own default."""
    cmd = ClaudeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind="run",
                                    resume=None, mcp_config=None)
    assert "--model" not in cmd


def test_vibe_command_ignores_the_model(config, tmp_path):
    """Vibe has no `--model` flag: the runner pins VIBE_ACTIVE_MODEL instead."""
    cmd = VibeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="run",
                                  resume=None, mcp_config=None, model="glm-5-3")
    assert "--model" not in cmd
    assert "glm-5-3" not in cmd


def test_claude_names_a_rejected_model():
    """An unknown model is an API 404: the log says so, and the detail names
    the model that was refused."""
    log = ('API Error: 404 {"type":"error","error":{"type":"not_found_error",'
           '"message":"model: claude-nope"}}\n')
    detail = ClaudeBackend().model_error(log, "claude-nope")
    assert detail is not None
    assert "claude-nope" in detail
    assert "\n" not in detail


def test_claude_model_error_ignores_other_failures():
    assert ClaudeBackend().model_error("API Error: 500 overloaded", "sonnet") is None
    assert ClaudeBackend().model_error("", "sonnet") is None
    # No model was asked for, so nothing can have been rejected.
    assert ClaudeBackend().model_error("not_found_error model: x", None) is None
    # Vibe never rejects a model this way: it has no --model flag at all.
    assert VibeBackend().model_error("not_found_error model: x", "x") is None


# ----------------------------------------------------------------------
# export normalization
# ----------------------------------------------------------------------

def test_vibe_export_passes_through(tmp_path):
    raw = {"usage": {"total_tokens": 10}, "session_id": "s1"}
    assert VibeBackend().normalize_export(raw) == raw


def test_claude_result_is_normalized(tmp_path):
    raw = {
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "abc", "num_turns": 7, "total_cost_usd": 0.25,
        "usage": {"input_tokens": 100, "output_tokens": 40,
                  "cache_read_input_tokens": 900, "cache_creation_input_tokens": 60},
        "modelUsage": {"claude-sonnet-4-6": {}},
        "terminal_reason": "completed",
    }
    out = ClaudeBackend().normalize_export(raw)
    # Anthropic's input_tokens excludes the cache counters, so the real prompt
    # size is the sum of all three.
    assert out["usage"]["input_tokens"] == 100 + 900 + 60
    assert out["usage"]["cached_input_tokens"] == 900
    assert out["usage"]["output_tokens"] == 40
    assert out["usage"]["total_tokens"] == 1060 + 40
    assert out["session_id"] == "abc"
    assert out["steps"] == 7
    assert out["cost_usd"] == 0.25
    assert out["config"]["active_model"] == "claude-sonnet-4-6"
    assert out["outcome"] == "finished"


def test_claude_error_result_is_flagged():
    out = ClaudeBackend().normalize_export({
        "type": "result", "is_error": True, "result": "OAuth access token is invalid.",
        "usage": {}, "session_id": "x",
    })
    assert out["outcome"] == "error"
    assert "OAuth" in out["error"]


def test_claude_reads_the_last_result_object_from_the_log(tmp_path):
    rdir = tmp_path / "run-1"
    rdir.mkdir()
    (rdir / "session.log").write_text(
        "some noise\n"
        + json.dumps({"type": "result", "is_error": False, "session_id": "s9",
                      "usage": {"input_tokens": 5, "output_tokens": 2}}) + "\n",
        encoding="utf-8")
    out = ClaudeBackend().read_export(rdir)
    assert out["session_id"] == "s9"
    assert out["usage"]["total_tokens"] == 7


def test_claude_read_export_is_empty_without_output(tmp_path):
    rdir = tmp_path / "run-1"
    rdir.mkdir()
    assert ClaudeBackend().read_export(rdir) == {}


def test_vibe_read_export_reads_export_json(tmp_path):
    rdir = tmp_path / "run-1"
    (rdir / "export").mkdir(parents=True)
    (rdir / "export" / "export.json").write_text(
        json.dumps({"usage": {"total_tokens": 42}, "session_id": "v1"}), encoding="utf-8")
    assert VibeBackend().read_export(rdir)["session_id"] == "v1"


# ----------------------------------------------------------------------
# the settings store
# ----------------------------------------------------------------------

def test_settings_default_and_roundtrip(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.backend() == DEFAULT_BACKEND
    assert store.set_backend("claude") == "claude"
    assert store.backend() == "claude"
    assert SettingsStore(tmp_path / "settings.json").backend() == "claude"


def test_settings_rejects_an_unknown_backend(tmp_path):
    from app.backends import BackendError
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(BackendError):
        store.set_backend("gpt")
    assert store.backend() == DEFAULT_BACKEND


def test_settings_ignores_a_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert SettingsStore(path).backend() == DEFAULT_BACKEND


# ----------------------------------------------------------------------
# the runner picks the backend
# ----------------------------------------------------------------------

def test_runner_uses_the_selected_backend(tmp_path, config):
    store = CardStore(tmp_path / "cards")
    settings = SettingsStore(tmp_path / "settings.json")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           settings=settings)
    assert runner.backend().key == "vibe"
    settings.set_backend("claude")
    assert runner.backend().key == "claude"


def test_runner_without_settings_uses_the_default(tmp_path, config):
    store = CardStore(tmp_path / "cards")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions")
    assert runner.backend().key == DEFAULT_BACKEND


def test_build_cmd_follows_the_backend(tmp_path, config):
    store = CardStore(tmp_path / "cards")
    settings = SettingsStore(tmp_path / "settings.json")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           settings=settings)
    vibe_cmd = runner._build_cmd(tmp_path / "p.md", tmp_path, tmp_path, "run")
    assert vibe_cmd[0] == config.vibe_bin
    settings.set_backend("claude")
    claude_cmd = runner._build_cmd(tmp_path / "p.md", tmp_path, tmp_path, "run")
    assert claude_cmd[0] == config.claude_bin


# ----------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------

def make_env(tmp_path: Path) -> SimpleNamespace:
    store = CardStore(tmp_path / "cards")
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(FAKE_VIBE)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), claude_bin=str(tmp_path / "claude"),
                    time_limit_seconds=30)
    projects = ProjectStore(tmp_path / "projects.json")
    settings = SettingsStore(tmp_path / "settings.json")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects, settings=settings)
    app = create_app(config=config, store=store, runner=runner, projects=projects,
                     settings=settings)
    return SimpleNamespace(client=TestClient(app), settings=settings, tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def test_api_lists_backends(env):
    data = env.client.get("/api/settings").json()
    assert data["backend"] == "vibe"
    assert {b["key"] for b in data["backends"]} == {"vibe", "claude"}


def test_api_switches_backend(env):
    r = env.client.put("/api/settings", json={"backend": "claude"})
    assert r.status_code == 200, r.text
    assert r.json()["backend"] == "claude"
    assert env.settings.backend() == "claude"
    assert env.client.get("/api/settings").json()["backend"] == "claude"


def test_api_rejects_an_unknown_backend(env):
    r = env.client.put("/api/settings", json={"backend": "gpt"})
    assert r.status_code == 400
    assert env.settings.backend() == "vibe"


def test_api_empty_patch_keeps_the_backend(env):
    env.settings.set_backend("claude")
    r = env.client.put("/api/settings", json={})
    assert r.status_code == 200
    assert r.json()["backend"] == "claude"
