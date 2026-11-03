"""Harness side of the per-card MCP mailbox (see app/mailbox_server.py).

Lifecycle (the harness supervises this; vibe itself spawns the stdio server
per session from the project-local ``.vibe/config.toml``):

- ``setup_mailbox(ws, sdir)`` — called once per card run: creates the mailbox
  state dir ``sessions/<card_id>/mailbox/`` and writes/merges the
  ``[[mcp_servers]]`` entry into ``<ws>/.vibe/config.toml`` (the card's linked
  git worktree, or the sandbox workspace). For git worktrees, ``.vibe/`` is
  added to the worktree-local ``info/exclude`` so the config never dirties
  the card branch or blocks a clean merge.
- ``read_verdict(mdir, round)`` / ``clear_round_verdict(mdir, round)`` /
  ``list_messages(mdir)`` — before spawning a review round the runner clears
  that round's verdict file; after it reads ONLY that round's structured
  verdict (``verdicts/<round>.json``, reviewer role only) plus the message
  log for token accounting/supervision.
- ``post_message(mdir, from_role, to, text)`` — the harness side of the same
  log (same format and sequence as the server), used by the dashboard's
  steering endpoint to send the user's guidance to the worker.

The spawned vibe processes get ``VIBE_HARNESS_MAILBOX_DIR`` /
``VIBE_HARNESS_MAILBOX_ROLE`` / ``VIBE_HARNESS_MAILBOX_ROUND`` /
``VIBE_HARNESS_MAILBOX_SERVER`` in their environment; the stdio server
inherits them..1.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import gitops

SERVER_SCRIPT = Path(__file__).resolve().parent / "mailbox_server.py"
SERVER_NAME = "harness-mailbox"
CONFIG_MARKER = "# vibe-harness managed: per-card MCP mailbox"

#: Valid `to` targets, same as the server's: the two spawned roles plus the
#: person watching the run (the dashboard's steering channel).
MESSAGE_TARGETS = ("worker", "reviewer", "user")


def mailbox_dir(sessions_dir: Path, card_id: str) -> Path:
    return Path(sessions_dir) / card_id / "mailbox"


def config_toml(server_command: str, server_args: list[str]) -> str:
    args = "\n".join(f'  "{a}",' for a in server_args)
    return (
        f"{CONFIG_MARKER}\n"
        "[[mcp_servers]]\n"
        f'name = "{SERVER_NAME}"\n'
        'transport = "stdio"\n'
        f'command = "{server_command}"\n'
        f"args = [\n{args}\n]\n"
    )


def _exclude_vibe_from_git(ws: Path) -> bool:
    """Add `.vibe/` to the worktree-local info/exclude so the managed
    `.vibe/config.toml` never shows up as an untracked/dirty file in the
    card's worktree. Returns True when the exclude was written."""
    return gitops.exclude_from_git(ws, ".vibe/")


def setup_mailbox(ws: Path, sdir: Path) -> dict:
    """Create the per-card mailbox state + attach the MCP server to the card's
    worktree via `.vibe/config.toml`. Returns the session `mailbox` record.

    An existing project `.vibe/config.toml` is NOT overwritten: the managed
    `[[mcp_servers]]` block is appended (valid TOML at EOF). `ws` is the card's
    linked worktree for git-backed runs, else the sandbox workspace.
    """
    ws = Path(ws)
    mdir = Path(sdir) / "mailbox"
    (mdir / "messages").mkdir(parents=True, exist_ok=True)
    (mdir / "verdicts").mkdir(parents=True, exist_ok=True)

    cfg_dir = ws / ".vibe"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / "config.toml"
    block = config_toml(shlex.quote(sys.executable), [str(SERVER_SCRIPT)])
    if cfg_path.exists():
        existing = cfg_path.read_text(encoding="utf-8")
        if CONFIG_MARKER not in existing:
            if existing and not existing.endswith("\n"):
                existing += "\n"
            cfg_path.write_text(existing + "\n" + block, encoding="utf-8")
    else:
        cfg_path.write_text(block, encoding="utf-8")

    excluded = _exclude_vibe_from_git(ws)
    return {"dir": str(mdir), "config": str(cfg_path), "server": str(SERVER_SCRIPT),
            "name": SERVER_NAME, "git_excluded": excluded}


def _safe_round(round_key: str) -> str:
    """Sanitize a round key for use as a filename (same rule as the server)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", round_key)


def verdicts_dir(mdir: Path) -> Path:
    return Path(mdir) / "verdicts"


def _load_verdict(path: Path) -> tuple[bool, str | None] | None:
    """Load a verdict file — but only if it was submitted by the reviewer role.

    A verdict whose `from` is not "reviewer" is not a verdict (mis-attribution
    guard on the read side; the server refuses non-reviewer submissions too).
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or "approved" not in data:
        return None
    if data.get("from") != "reviewer":
        return None
    reason = data.get("reason")
    return bool(data["approved"]), (reason if reason else None)


def read_verdict(mdir: Path, round: str | None = None) -> tuple[bool, str | None] | None:
    """The structured verdict written by the reviewer's `submit_verdict`.

    With ``round`` (e.g. ``"review-2"``) only that review round's verdict file
    (``verdicts/<round>.json``) is read, so a round that never called
    submit_verdict gets None instead of inheriting an earlier round's verdict.
    Without ``round`` the latest verdict (``verdict.json``) is read. In both
    cases the verdict must come from the reviewer role, else None.
    """
    mdir = Path(mdir)
    if round is not None:
        return _load_verdict(verdicts_dir(mdir) / f"{_safe_round(round)}.json")
    return _load_verdict(mdir / "verdict.json")


def clear_round_verdict(mdir: Path, round: str) -> bool:
    """Delete a review round's verdict file. The runner calls this before
    spawning each review round, so a stale verdict from an earlier attempt at
    the same round can never be inherited. Returns True when a file was
    removed."""
    path = verdicts_dir(Path(mdir)) / f"{_safe_round(round)}.json"
    try:
        path.unlink()
        return True
    except OSError:
        return False


def list_messages(mdir: Path) -> list[dict]:
    """All mailbox messages, oldest first (supervision/token accounting)."""
    mdir = Path(mdir) / "messages"
    out = []
    if not mdir.is_dir():
        return out
    for path in sorted(mdir.glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def post_message(mdir: Path, from_role: str, to: str, text: str) -> dict:
    """Append a message to the card's mailbox from the harness side.

    Same on-disk format and sequence allocation as the MCP server
    (``app/mailbox_server.py``), so a message posted here is exactly what the
    worker's next ``wait_for_message`` returns. The dashboard's steering
    endpoint uses it to send the user's guidance (``from_role="user"``,
    ``to="worker"``).
    """
    if to not in MESSAGE_TARGETS:
        raise ValueError(f"invalid 'to' {to!r}: must be one of {MESSAGE_TARGETS}")
    text = (text or "").strip()
    if not text:
        raise ValueError("text must be a non-empty string")
    mdir = Path(mdir) / "messages"
    mdir.mkdir(parents=True, exist_ok=True)
    seq = len(list(mdir.glob("*.json"))) + 1
    while True:
        path = mdir / f"{seq:06d}.json"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            seq += 1
            continue
        msg = {"seq": seq, "from": from_role, "to": to, "text": text,
               "ts": datetime.now(timezone.utc).isoformat()}
        os.write(fd, json.dumps(msg).encode("utf-8"))
        os.close(fd)
        return msg
