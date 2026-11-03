"""Session backends: which agent CLI the harness spawns.

The harness is a board, not an agent. It drives a headless CLI and reads back
what that CLI reports. Two are supported:

- **vibe** — the Mistral Vibe CLI (`vibe --prompt-file … --output-dir …`). It
  writes `export.json` with usage, cost and the session id, and enforces its own
  `--time-limit`.
- **claude** — Claude Code (`claude -p --output-format json`). It prints one
  result object on stdout and has no `--time-limit`, so the harness enforces the
  time limit itself.

A backend owns exactly three things: the command line, where its output lands,
and how to normalize that output into the shape the rest of the harness already
speaks (`usage`, `session_id`, `config.active_model`, `stop_reason`). Everything
downstream — the review loop, git, archives, the board — is backend-agnostic.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.config import Config


class BackendError(Exception):
    """The backend cannot run (missing CLI, unusable config)."""


class Backend:
    """One agent CLI. Subclasses fill in the three hooks below."""

    key = ""
    label = ""
    #: The CLI enforces `--time-limit` itself; when False the runner watches the
    #: clock and terminates the process.
    native_time_limit = False
    #: The prompt is fed on stdin instead of a `--prompt-file` flag.
    prompt_on_stdin = False

    def binary(self, config: Config) -> str:
        raise NotImplementedError

    def available(self, config: Config) -> bool:
        return shutil.which(self.binary(config)) is not None

    def build_cmd(self, *, config: Config, prompt_path: Path, ws: Path,
                  rdir: Path, kind: str, resume: str | None,
                  mcp_config: Path | None,
                  model: str | None = None) -> list[str]:
        raise NotImplementedError

    def model_error(self, text: str, model: str | None) -> str | None:
        """The one-line reason this CLI refused `model`, or None.

        A rejected model exits non-zero like any other crash, so the cause has
        to be read back out of the round's log — otherwise the dropped choice
        looks like a random failure (or, worse, like a success on the default
        model). Backends that cannot reject a model return None.
        """
        return None

    def export_path(self, rdir: Path) -> Path:
        """Where this backend's raw output lands for one round."""
        raise NotImplementedError

    def normalize_export(self, raw: dict) -> dict:
        """Map the backend's own output onto the harness's export shape."""
        raise NotImplementedError

    def read_export(self, rdir: Path) -> dict:
        """Read + normalize one round's output. Never raises."""
        path = self.export_path(rdir)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        return self.normalize_export(self._parse(text))

    def _parse(self, text: str) -> dict:
        try:
            loaded = json.loads(text)
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}


class VibeBackend(Backend):
    key = "vibe"
    label = "Mistral Vibe"
    native_time_limit = True

    def binary(self, config: Config) -> str:
        return config.vibe_bin

    def build_cmd(self, *, config: Config, prompt_path: Path, ws: Path,
                  rdir: Path, kind: str, resume: str | None,
                  mcp_config: Path | None,
                  model: str | None = None) -> list[str]:
        # `model` is ignored on purpose: Vibe has no `--model` flag. The runner
        # pins the choice for this one session with VIBE_ACTIVE_MODEL in the
        # spawned environment instead (see SessionRunner._model_env).
        cmd = [self.binary(config)]
        if resume:
            cmd += ["--resume", resume]
        cmd += ["--prompt-file", str(prompt_path),
                "--workdir", str(ws),
                "--output-dir", str(rdir / "export"),
                "--time-limit", str(config.time_limit_seconds)]
        if config.max_price_usd is not None:
            cmd += ["--max-price", str(config.max_price_usd)]
        if kind == "review":
            # The reviewer is a read-only pass; never the configured agent.
            cmd += ["--agent", config.review_agent]
        elif config.agent:
            cmd += ["--agent", config.agent]
        cmd += (["--auto-approve"] if config.approve == "auto"
                else ["--smart-approve"])
        enabled = list(config.enabled_tools)
        if enabled and config.mcp_enabled:
            # Keep the harness MCP tools (published as <name>_<tool>) available
            # even under a tool allowlist.
            enabled.append(f"{config.mcp_server_name}_*")
        cmd += [flag for t in enabled for flag in ("--enabled-tools", t)]
        return cmd

    def export_path(self, rdir: Path) -> Path:
        return rdir / "export" / "export.json"

    def normalize_export(self, raw: dict) -> dict:
        # Vibe already writes the harness's shape; pass it through.
        return raw


class ClaudeBackend(Backend):
    key = "claude"
    label = "Claude Code"
    native_time_limit = False
    prompt_on_stdin = True

    #: Tools a reviewer may use. No Edit/Write/NotebookEdit, so the review pass
    #: cannot change the code it is judging — the closest equivalent of Vibe's
    #: read-only `--agent ask`.
    REVIEW_TOOLS = ("Read", "Grep", "Glob", "Bash")

    #: What Claude prints when it refuses a model name. The API answers
    #: `not_found_error` for an unknown model; the CLI itself says "issue with
    #: the selected model" when the alias is not one it knows.
    MODEL_ERROR_MARKERS = (
        "not_found_error",
        "model_not_found",
        "invalid model",
        "unknown model",
        "issue with the selected model",
        "may not exist or you may not have access",
    )

    def binary(self, config: Config) -> str:
        return config.claude_bin

    def build_cmd(self, *, config: Config, prompt_path: Path, ws: Path,
                  rdir: Path, kind: str, resume: str | None,
                  mcp_config: Path | None,
                  model: str | None = None) -> list[str]:
        cmd = [self.binary(config), "-p", "--output-format", "json"]
        if model:
            # Claude takes the model as a flag; without it the CLI silently
            # runs on its own default and the picker's choice is dropped.
            cmd += ["--model", model]
        if resume:
            cmd += ["--resume", resume]
        if config.max_price_usd is not None:
            # Claude's own dollar cap (print mode only).
            cmd += ["--max-budget-usd", str(config.max_price_usd)]
        if kind == "review":
            # Read-only: no tool that can write.
            cmd += ["--tools", ",".join(self.REVIEW_TOOLS)]
            cmd += ["--permission-mode", "default"]
        else:
            # Runs are unattended, so they need the same "approve everything"
            # the harness gives Vibe.
            cmd += ["--permission-mode",
                    "bypassPermissions" if config.approve == "auto" else "auto"]
            if config.enabled_tools:
                cmd += ["--allowedTools", ",".join(config.enabled_tools)]
        if mcp_config is not None:
            cmd += ["--mcp-config", str(mcp_config)]
        return cmd

    def export_path(self, rdir: Path) -> Path:
        # Claude prints its result object on stdout, which the runner tees into
        # session.log (the reviewer's VERDICT line has to stay greppable there).
        return rdir / "session.log"

    def model_error(self, text: str, model: str | None) -> str | None:
        if not model:
            return None
        for line in text.splitlines():
            lowered = line.lower()
            if any(marker in lowered for marker in self.MODEL_ERROR_MARKERS):
                return (f"the backend rejected the model {model!r}: "
                        f"{' '.join(line.split())[:300]}")
        return None

    def _parse(self, text: str) -> dict:
        """The last JSON object in the log — Claude prints one result object."""
        for line in reversed(text.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                loaded = json.loads(line)
            except ValueError:
                continue
            if isinstance(loaded, dict) and loaded.get("type") == "result":
                return loaded
        return {}

    def normalize_export(self, raw: dict) -> dict:
        if not raw:
            return {}
        usage = raw.get("usage") or {}
        # Anthropic's `input_tokens` excludes the cache counters, so the real
        # prompt size is the sum of all three.
        cache_read = int(usage.get("cache_read_input_tokens") or 0)
        cache_write = int(usage.get("cache_creation_input_tokens") or 0)
        fresh = int(usage.get("input_tokens") or 0)
        output = int(usage.get("output_tokens") or 0)
        total_in = fresh + cache_read + cache_write
        models = list((raw.get("modelUsage") or {}).keys())
        return {
            "usage": {
                "input_tokens": total_in,
                "output_tokens": output,
                "cached_input_tokens": cache_read,
                "total_tokens": total_in + output,
            },
            "session_id": raw.get("session_id"),
            "cost_usd": raw.get("total_cost_usd"),
            "steps": raw.get("num_turns"),
            "config": {"active_model": models[0] if models else None},
            "stop_reason": raw.get("terminal_reason"),
            "outcome": "error" if raw.get("is_error") else "finished",
            "error": raw.get("result") if raw.get("is_error") else None,
            "permission_denials": raw.get("permission_denials") or [],
        }


BACKENDS: dict[str, type[Backend]] = {
    VibeBackend.key: VibeBackend,
    ClaudeBackend.key: ClaudeBackend,
}
DEFAULT_BACKEND = VibeBackend.key


def get_backend(key: str | None) -> Backend:
    """The backend for `key`, falling back to the default for anything unknown."""
    return BACKENDS.get((key or "").strip().lower(), BACKENDS[DEFAULT_BACKEND])()


def describe_backends(config: Config) -> list[dict]:
    """What the board's picker shows: each backend and whether it can run."""
    out = []
    for key, cls in BACKENDS.items():
        backend = cls()
        out.append({
            "key": key,
            "label": backend.label,
            "binary": backend.binary(config),
            "available": backend.available(config),
        })
    return out
