"""Configuration for the vibe-harness dashboard.

All values have defaults and can be overridden via VIBE_HARNESS_* env vars.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CARDS_DIR = BASE_DIR / "cards"
SESSIONS_DIR = BASE_DIR / "sessions"
ARCHIVES_DIR = BASE_DIR / "archives"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
PROJECTS_FILE = BASE_DIR / "projects.json"
MODELS_FILE = BASE_DIR / "models.json"
SETTINGS_FILE = BASE_DIR / "settings.json"


def resolve_path(raw: str) -> Path:
    """Resolve a user-supplied path: `~` expanded, relative -> harness root."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = BASE_DIR / p
    return p.resolve()


#: Default VIBE_HARNESS_REVIEW_AGENT. Vibe's read-only builtin `ask` agent; the
#: setting is Vibe-only (--agent) — Claude review sessions are read-only via
#: --tools/--permission-mode and reject a non-default value instead of
#: silently ignoring it (see app/backends.py).
DEFAULT_REVIEW_AGENT = "ask"


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8734
    time_limit_seconds: int = 1800
    max_price_usd: float | None = None      # None = no price cap
    agent: str | None = None                # Vibe agent: builtin or ~/.vibe/agents/NAME.toml
    approve: str = "auto"                   # "auto" -> --auto-approve | "smart" -> --smart-approve
    enabled_tools: list[str] = field(default_factory=list)  # allowlist -> repeated --enabled-tools
    test_cmd: str | None = None             # test command run in the workspace; gates review + accept
    test_timeout_seconds: int = 600         # timeout for the test command (separate from vibe's time limit)
    # Compat shim: the deprecated VIBE_HARNESS_ACCEPT_CMD alias for test_cmd
    # (docs/configuration.md). No app code reads it — __post_init__ folds it
    # into test_cmd — so it stays only for configs written before the rename.
    accept_cmd: str | None = None
    review_agent: str = DEFAULT_REVIEW_AGENT  # read-only agent for the review pass (vibe only)
    max_review_rounds: int = 5              # cap on review/fix loop rounds per card
    git_branch_prefix: str = "harness"      # per-card git branch prefix (<prefix>/<card_id>)
    accept_mode: str = "local"              # "local" -> merge on accept | "pr" -> push + open a PR
    vibe_bin: str = "vibe"
    claude_bin: str = "claude"            # Claude Code CLI (backend "claude")
    allowed_workdirs: list[str] = field(default_factory=list)  # absolute prefixes; empty = allow all
    mcp_server_name: str = "harness"         # MCP server alias injected into spawned vibe sessions
    mcp_enabled: bool = True                 # inject the harness MCP server (VIBE_MCP_SERVERS) into run sessions
    ask_user_timeout_seconds: int = 600      # default wait for the MCP ask_user tool (per question)
    worker_resume: bool = True              # resume the worker session across fix rounds (vibe --resume)
    mailbox: bool = True                    # per-card MCP mailbox (worker <-> reviewer) via .vibe/config.toml
    review_diff: bool = True                # fresh reviewer only gets git diff since last review + test output
    tdd: bool = True                        # worker prompts (run + fixer rounds) instruct test-driven development
    prevent_sleep: bool = True              # hold an idle-sleep inhibitor while cards run or wait
    max_concurrent: int = 3                 # concurrent agent sessions; 0 = unlimited (sidebar setting overrides)

    def __post_init__(self) -> None:
        # Backward compat: accept_cmd is an alias for test_cmd.
        if self.accept_cmd and not self.test_cmd:
            self.test_cmd = self.accept_cmd

    def vibe_available(self) -> bool:
        return shutil.which(self.vibe_bin) is not None

    def api_base_url(self) -> str:
        """URL the MCP server (and anything else local) uses to reach this dashboard."""
        host = self.host if self.host not in ("0.0.0.0", "::", "") else "127.0.0.1"
        return f"http://{host}:{self.port}"

    def workdir_allowed(self, path: Path) -> bool:
        """True if `path` (already resolved) is permitted as a custom workdir."""
        if not self.allowed_workdirs:
            return True
        p = str(path)
        return any(p == a or p.startswith(a.rstrip("/") + "/") for a in self.allowed_workdirs)


def _env_bool(env: dict[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _accept_mode(raw: str | None) -> str:
    """`pr` opts into pull-request accept; anything else is the local default.

    Lenient on purpose: a typo in the env var must not stop the server from
    starting — it just keeps the current (local) behaviour. The sidebar setting
    is strict instead (PUT /api/settings answers 400 on an unknown value).
    """
    return "pr" if (raw or "").strip().lower() == "pr" else "local"


def load_config(env: dict[str, str] | None = None) -> Config:
    env = os.environ if env is None else env

    raw_tools = env.get("VIBE_HARNESS_ENABLED_TOOLS", "")
    enabled_tools = [t.strip() for t in raw_tools.split(",") if t.strip()]

    raw_allowed = env.get("VIBE_HARNESS_ALLOWED_WORKDIRS", "")
    allowed_workdirs = [str(Path(a).expanduser().resolve())
                       for a in raw_allowed.split(",") if a.strip()]

    raw_price = env.get("VIBE_HARNESS_MAX_PRICE", "")
    max_price = float(raw_price) if raw_price else None

    return Config(
        host=env.get("VIBE_HARNESS_HOST", "127.0.0.1"),
        port=int(env.get("VIBE_HARNESS_PORT", "8734")),
        time_limit_seconds=int(env.get("VIBE_HARNESS_TIME_LIMIT", "1800")),
        max_price_usd=max_price,
        agent=env.get("VIBE_HARNESS_AGENT") or None,
        approve=env.get("VIBE_HARNESS_APPROVE", "auto"),
        enabled_tools=enabled_tools,
        test_cmd=env.get("VIBE_HARNESS_TEST_CMD") or None,
        test_timeout_seconds=int(env.get("VIBE_HARNESS_TEST_TIMEOUT", "600")),
        accept_cmd=env.get("VIBE_HARNESS_ACCEPT_CMD") or None,
        review_agent=env.get("VIBE_HARNESS_REVIEW_AGENT", DEFAULT_REVIEW_AGENT),
        max_review_rounds=int(env.get("VIBE_HARNESS_MAX_REVIEW_ROUNDS", "5")),
        git_branch_prefix=env.get("VIBE_HARNESS_GIT_BRANCH_PREFIX", "harness"),
        accept_mode=_accept_mode(env.get("VIBE_HARNESS_ACCEPT_MODE")),
        vibe_bin=env.get("VIBE_HARNESS_VIBE_BIN", "vibe"),
        claude_bin=env.get("VIBE_HARNESS_CLAUDE_BIN", "claude"),
        allowed_workdirs=allowed_workdirs,
        mcp_server_name=env.get("VIBE_HARNESS_MCP_SERVER_NAME", "harness"),
        mcp_enabled=_env_bool(env, "VIBE_HARNESS_MCP_ENABLED", True),
        ask_user_timeout_seconds=int(env.get("VIBE_HARNESS_ASK_USER_TIMEOUT", "600")),
        worker_resume=_env_bool(env, "VIBE_HARNESS_WORKER_RESUME", True),
        mailbox=_env_bool(env, "VIBE_HARNESS_MAILBOX", True),
        review_diff=_env_bool(env, "VIBE_HARNESS_REVIEW_DIFF", True),
        tdd=_env_bool(env, "VIBE_HARNESS_TDD", True),
        max_concurrent=int(env.get("VIBE_HARNESS_MAX_CONCURRENT", "3")),
        prevent_sleep=_env_bool(env, "VIBE_HARNESS_PREVENT_SLEEP", True),
    )
