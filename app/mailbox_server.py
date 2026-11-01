#!/usr/bin/env python3
"""Per-card MCP mailbox server (stdio) for vibe-harness.

Attached to every worker/reviewer vibe session via the ``.vibe/config.toml``
that the harness writes into the card's worktree (see ``app/mailbox.py``).
Vibe spawns one instance of this script per session as a stdio MCP server, so
all state lives on the filesystem in a per-card mailbox directory — the two
sides (persistent worker, fresh reviewer) communicate through it:

- ``post_message(to, text)`` — append a message addressed to ``to``
  (``"worker"`` or ``"reviewer"``); ``from`` is the caller's role.
- ``wait_for_message(timeout_seconds?)`` — block until an unread message
  addressed to the caller's role arrives; returns it (or ``{"none": true}``
  on timeout). Consumed messages are tracked per role.
- ``submit_verdict(approved, reason)`` — write the reviewer's structured
  verdict. REVIEWER ROLE ONLY (any other role gets a tool error). The verdict
  is stored per review round as ``verdicts/<round>.json`` (round from
  ``VIBE_HARNESS_MAILBOX_ROUND``) and mirrored to the top-level
  ``verdict.json`` ("latest"). The harness reads only the current round's
  file, so a review round that never calls submit_verdict sees no verdict
  instead of inheriting a previous round's.

Configuration comes from the environment (inherited from the vibe process the
harness spawned):

- ``VIBE_HARNESS_MAILBOX_DIR``  — per-card mailbox state directory (required)
- ``VIBE_HARNESS_MAILBOX_ROLE`` — this server instance's role: worker/reviewer
- ``VIBE_HARNESS_MAILBOX_ROUND`` — this session's round key (e.g. ``review-2``);
  scopes submit_verdict's per-round verdict file

Protocol: MCP over stdio = newline-delimited JSON-RPC 2.0. Only stdlib is
used so the server runs under any python3..1.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "vibe-harness-mailbox"
SERVER_VERSION = "0.1.0"
ROLES = ("worker", "reviewer")

DEFAULT_WAIT_SECONDS = 60.0
POLL_INTERVAL = 0.1

TOOLS = [
    {
        "name": "post_message",
        "description": "Send a message to the other party in this card's review "
                       "loop (to: 'worker' or 'reviewer').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "enum": list(ROLES)},
                "text": {"type": "string"},
            },
            "required": ["to", "text"],
        },
    },
    {
        "name": "wait_for_message",
        "description": "Block until a message addressed to you (your role) that you "
                       "have not consumed yet arrives; returns it, or {\"none\": true} "
                       "on timeout.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "timeout_seconds": {"type": "number", "default": DEFAULT_WAIT_SECONDS},
            },
        },
    },
    {
        "name": "submit_verdict",
        "description": "Submit the final review verdict (reviewer role only). This "
                       "is the structured verdict channel the harness reads "
                       "(approved, reason); stored per review round.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "approved": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["approved"],
        },
    },
]


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mailbox_dir() -> Path:
    raw = os.environ.get("VIBE_HARNESS_MAILBOX_DIR", "").strip()
    if not raw:
        raise RpcError(-32000, "VIBE_HARNESS_MAILBOX_DIR is not set — the harness "
                               "must spawn this server with the per-card mailbox dir")
    return Path(raw)


def _role() -> str:
    return os.environ.get("VIBE_HARNESS_MAILBOX_ROLE", "unknown")


def _round() -> str:
    """This session's round key (e.g. ``review-2``), sanitized for filenames."""
    raw = os.environ.get("VIBE_HARNESS_MAILBOX_ROUND", "").strip() or "unspecified"
    return "".join(c if (c.isalnum() or c in "-_.") else "_" for c in raw)


def _messages_dir() -> Path:
    d = _mailbox_dir() / "messages"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _post_message(to: str, text: str) -> dict:
    if to not in ROLES:
        raise RpcError(-32602, f"invalid 'to' {to!r}: must be one of {ROLES}")
    mdir = _messages_dir()
    seq = len(list(mdir.glob("*.json"))) + 1
    while True:
        path = mdir / f"{seq:06d}.json"
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            seq += 1
            continue
        msg = {"seq": seq, "from": _role(), "to": to, "text": text, "ts": _now()}
        os.write(fd, json.dumps(msg).encode("utf-8"))
        os.close(fd)
        return msg


def _seen_path() -> Path:
    return _mailbox_dir() / f"seen-{_role()}.json"


def _last_seen() -> int:
    try:
        return int(json.loads(_seen_path().read_text(encoding="utf-8")).get("seq", 0))
    except (OSError, ValueError):
        return 0


def _mark_seen(seq: int) -> None:
    _atomic_write(_seen_path(), {"seq": seq})


def _read_messages() -> list[dict]:
    out = []
    for path in sorted(_messages_dir().glob("*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _wait_for_message(timeout_seconds: float) -> dict:
    seen = _last_seen()
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while True:
        for msg in _read_messages():
            if msg.get("to") == _role() and int(msg.get("seq", 0)) > seen:
                _mark_seen(int(msg["seq"]))
                return {"message": msg}
        if time.monotonic() >= deadline:
            return {"none": True, "role": _role()}
        time.sleep(POLL_INTERVAL)


def _verdicts_dir() -> Path:
    d = _mailbox_dir() / "verdicts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _submit_verdict(approved: bool, reason: str = "") -> dict:
    # Only the reviewer may submit a verdict — a worker calling this must not
    # be able to forge one, and the harness double-checks `from` on read.
    if _role() != "reviewer":
        raise RpcError(-32602, "only the reviewer role may call submit_verdict "
                               f"(this server's role is {_role()!r})")
    verdict = {"approved": bool(approved), "reason": reason or None,
               "from": _role(), "round": _round(), "ts": _now()}
    # Per-round file: the harness reads exactly this round's verdict, so a
    # review round that never calls submit_verdict gets None — never a stale
    # verdict inherited from an earlier round.
    _atomic_write(_verdicts_dir() / f"{_round()}.json", verdict)
    # Latest-verdict mirror for supervision / round-less reads.
    _atomic_write(_mailbox_dir() / "verdict.json", verdict)
    return {"ok": True, "verdict": verdict}


def handle(method: str, params: dict) -> dict:
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name == "post_message":
            out = _post_message(args.get("to"), args.get("text", ""))
        elif name == "wait_for_message":
            timeout = args.get("timeout_seconds", DEFAULT_WAIT_SECONDS)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
                raise RpcError(-32602, "timeout_seconds must be a number")
            out = _wait_for_message(float(timeout))
        elif name == "submit_verdict":
            if "approved" not in args or not isinstance(args["approved"], bool):
                raise RpcError(-32602, "submit_verdict requires boolean 'approved'")
            out = _submit_verdict(args["approved"], args.get("reason") or "")
        else:
            raise RpcError(-32602, f"unknown tool {name!r}")
        return {"content": [{"type": "text", "text": json.dumps(out)}],
                "structuredContent": out, "isError": False}
    raise RpcError(-32601, f"method not found: {method!r}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue  # not JSON: ignore (stdio transport is line-delimited JSON)
        if not isinstance(msg, dict) or "id" not in msg:
            continue  # notification (e.g. notifications/initialized): no response
        try:
            result = handle(msg.get("method", ""), msg.get("params") or {})
            resp = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        except RpcError as exc:
            resp = {"jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:  # never crash the server on one bad call
            resp = {"jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32603, "message": f"internal error: {exc}"}}
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
