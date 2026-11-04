"""Regression matrix for Vibe/Claude backend parity.

The report found places where Vibe and Claude silently diverged: the tool
allowlist append, review read-only, time-limit enforcement, mailbox prompt
gating, and dynamic MCP tool names. For a set of configs — default,
enabled_tools set, review kind, claude backend, mcp disabled, renamed mcp
server — this matrix spawns one session PER BACKEND through the real
``SessionRunner._spawn`` (only ``Popen`` is stubbed) and asserts the built
command AND the built prompt are correct and consistent on both backends
wherever they should be:

- the ``<mcp_name>_``* allowlist entry is appended on BOTH backends
  (Vibe: ``--enabled-tools <name>_*``; Claude: ``--allowedTools
  mcp__<name>__*``) — and on neither when the MCP is disabled;
- review sessions are read-only on both (Vibe: the read-only ``--agent``;
  Claude: ``--tools`` without any write tool + ``--permission-mode
  default``);
- the time limit is enforced on both — native ``--time-limit`` for Vibe,
  the harness watchdog for Claude, never both on one spawn;
- ``mcp_rules()`` and the run prompt use the configured server-name prefix
  (no hardcoded ``harness_*`` when the server is renamed);
- the mailbox prompt text is present for Vibe and absent for Claude (only
  Vibe reads the worktree's ``.vibe/config.toml`` that attaches the mailbox).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.backends import ClaudeBackend, VibeBackend
from app.cards import CardStore
from app.config import Config
from app.runner import SessionRunner
from app.runner.prompts import mcp_rules, prompt_template, review_prompt_template
from app.settings import SettingsStore

#: The matrix: (row id, Config kwargs, session kind). Every row is spawned on
#: BOTH backends (settings backend vibe and claude), so each row is a parity
#: check across the two CLIs for that configuration.
MATRIX = [
    ("default", {}, "run"),
    ("enabled-tools", {"enabled_tools": ["Read", "Bash"]}, "run"),
    ("review", {}, "review"),
    ("claude-backend", {}, "run"),
    ("mcp-disabled", {"mcp_enabled": False, "enabled_tools": ["Read"]}, "run"),
    ("renamed-server", {"mcp_server_name": "board",
                        "enabled_tools": ["Read"]}, "run"),
]

WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")


def _config(tmp_path: Path, **kwargs) -> Config:
    kwargs.setdefault("vibe_bin", str(tmp_path / "vibe"))
    kwargs.setdefault("claude_bin", str(tmp_path / "claude"))
    kwargs.setdefault("time_limit_seconds", 30)
    return Config(**kwargs)


class _FakeProc:
    pid = 4242

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0


def _prompt_for(runner: SessionRunner, config: Config, kind: str,
                card: dict) -> str:
    """The prompt exactly as the runner builds it for this spawn."""
    if kind == "review":
        return review_prompt_template(card["body"], duplication=[],
                                      mailbox=runner._prompt_mailbox("review", card))
    return prompt_template(card["body"], runner._mcp_name(), tdd=config.tdd,
                           mailbox=runner._prompt_mailbox("run", card))


def _spawn_capture(tmp_path: Path, monkeypatch, *, label: str, config: Config,
                   backend_key: str, kind: str) -> SimpleNamespace:
    """Spawn one session through the real runner with Popen stubbed; capture
    the built command, env, on-disk prompt, and the watchdog calls."""
    store = CardStore(tmp_path / f"cards-{label}")
    settings = SettingsStore(tmp_path / f"settings-{label}.json")
    settings.set_backend(backend_key)
    runner = SessionRunner(store, config, sessions_dir=tmp_path / f"sessions-{label}",
                           settings=settings)
    watched: list[str] = []
    monkeypatch.setattr(runner, "_watch_time_limit",
                        lambda card_id, proc: watched.append(card_id))
    monkeypatch.setattr(runner, "_finalize", lambda *a, **k: None)
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr("app.runner.subprocess.Popen", fake_popen)
    card = store.create("Card", "spec body")
    if kind == "review":
        # The real loop reviews a finished run: the card is "running" first.
        store.set_status(card["id"], "running")
    runner._spawn(card["id"], kind=kind, prompt=_prompt_for(runner, config, kind, card))
    rdir = tmp_path / f"sessions-{label}" / card["id"] / f"{kind}-1"
    on_disk = (rdir / "prompt.md").read_text(encoding="utf-8")
    return SimpleNamespace(runner=runner, card=card, cmd=captured["cmd"],
                           env=captured["env"], prompt=on_disk, watched=watched,
                           gated=runner._prompt_mailbox(kind, card))


def _enabled_tools_flags(cmd: list[str]) -> list[str]:
    return [cmd[i + 1] for i, c in enumerate(cmd) if c == "--enabled-tools"]


def _assert_time_limit_parity(cap: SimpleNamespace, config: Config,
                              backend_key: str) -> None:
    """One enforcement per spawn: native flag on Vibe, watchdog on Claude."""
    if backend_key == "vibe":
        assert VibeBackend.native_time_limit is True
        assert cap.cmd[cap.cmd.index("--time-limit") + 1] == str(config.time_limit_seconds)
        assert cap.watched == []
    else:
        assert ClaudeBackend.native_time_limit is False
        assert "--time-limit" not in cap.cmd
        assert cap.watched == [cap.card["id"]]


def _assert_mailbox_gating(cap: SimpleNamespace, kind: str,
                           backend_key: str) -> None:
    """The mailbox prompt text follows the backend: present for Vibe (it
    reads the worktree's .vibe/config.toml), absent for Claude."""
    if backend_key == "vibe":
        assert cap.gated is True
        assert "harness-mailbox" in cap.prompt
        assert "wait_for_message" in cap.prompt
        if kind == "review":
            assert "submit_verdict" in cap.prompt
    else:
        assert cap.gated is False
        assert "harness-mailbox" not in cap.prompt
        assert "wait_for_message" not in cap.prompt
        assert "submit_verdict" not in cap.prompt


def _assert_mcp_prefix(cap: SimpleNamespace, config: Config) -> None:
    """The run prompt's MCP tool names carry the configured server prefix."""
    name = config.mcp_server_name
    if config.mcp_enabled:
        assert cap.runner._mcp_name() == name
        assert f"{name}_list_cards" in cap.prompt
        assert f"mcp_{name}" in cap.prompt
        if name != "harness":
            assert "harness_list_cards" not in cap.prompt
            assert "harness_create_card" not in cap.prompt
    else:
        assert cap.runner._mcp_name() is None
        assert "_list_cards" not in cap.prompt
        assert "_create_card" not in cap.prompt


def _assert_allowlist(cap: SimpleNamespace, config: Config,
                      backend_key: str) -> None:
    """The allowlist keeps the board MCP tools on BOTH backends — each in its
    own naming scheme — and neither adds a pattern when the MCP is off."""
    name = config.mcp_server_name
    if backend_key == "vibe":
        flags = _enabled_tools_flags(cap.cmd)
        assert flags[:len(config.enabled_tools)] == config.enabled_tools
        if config.enabled_tools and config.mcp_enabled:
            assert f"{name}_*" in flags
            if name != "harness":
                assert "harness_*" not in flags
        else:
            assert f"{name}_*" not in flags
    else:
        if not config.enabled_tools:
            assert "--allowedTools" not in cap.cmd
            return
        tools = cap.cmd[cap.cmd.index("--allowedTools") + 1].split(",")
        assert tools[:len(config.enabled_tools)] == config.enabled_tools
        if config.mcp_enabled:
            assert f"mcp__{name}__*" in tools
            if name != "harness":
                assert "mcp__harness__*" not in tools
        else:
            assert not any(t.startswith("mcp__") for t in tools)


def _assert_review_read_only(cap: SimpleNamespace, config: Config,
                             backend_key: str) -> None:
    """Review sessions cannot write on either backend."""
    if backend_key == "vibe":
        assert cap.cmd[cap.cmd.index("--agent") + 1] == config.review_agent
    else:
        tools = cap.cmd[cap.cmd.index("--tools") + 1].split(",")
        for write in WRITE_TOOLS:
            assert write not in tools
        assert "Read" in tools
        assert cap.cmd[cap.cmd.index("--permission-mode") + 1] == "default"
        # Read-only via --tools, not via an allowlist.
        assert "--allowedTools" not in cap.cmd


@pytest.mark.parametrize("row,kwargs,kind", MATRIX, ids=[m[0] for m in MATRIX])
@pytest.mark.parametrize("backend_key", ["vibe", "claude"])
def test_backend_parity_matrix(tmp_path, monkeypatch, row, kwargs, kind,
                               backend_key):
    """One spawn per backend per config row; the parity assertions below run
    against the captured command and prompt."""
    config = _config(tmp_path, **kwargs)
    cap = _spawn_capture(tmp_path, monkeypatch, label=f"{row}-{backend_key}",
                         config=config, backend_key=backend_key, kind=kind)
    # The settings-selected backend really drives the spawn.
    assert cap.runner._role_backend(kind).key == backend_key
    expected_bin = config.vibe_bin if backend_key == "vibe" else config.claude_bin
    assert cap.cmd[0] == expected_bin
    _assert_time_limit_parity(cap, config, backend_key)
    _assert_mailbox_gating(cap, kind, backend_key)
    if kind == "run":
        _assert_mcp_prefix(cap, config)
        _assert_allowlist(cap, config, backend_key)
    else:
        _assert_review_read_only(cap, config, backend_key)


def test_mcp_rules_use_the_configured_server_name():
    """No hardcoded `harness_*` tool names: every tool is built from the
    configured server name."""
    renamed = mcp_rules("board")
    assert "board_list_cards" in renamed
    assert "board_create_card" in renamed
    assert "mcp_board" in renamed
    assert "harness_" not in renamed
    default = mcp_rules("harness")
    assert "harness_list_cards" in default


def test_mailbox_kill_switch_hides_the_prompt_on_both_backends(tmp_path,
                                                               monkeypatch):
    """With the mailbox disabled, neither backend's prompt may mention it."""
    config = _config(tmp_path, mailbox=False)
    for backend_key in ("vibe", "claude"):
        cap = _spawn_capture(tmp_path, monkeypatch, label=f"nomail-{backend_key}",
                             config=config, backend_key=backend_key, kind="run")
        assert cap.gated is False
        assert "harness-mailbox" not in cap.prompt
        assert "wait_for_message" not in cap.prompt
