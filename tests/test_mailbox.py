"""Unit tests for the per-card MCP mailbox server (app/mailbox_server.py).

Drives the real server over stdio (newline-delimited JSON-RPC 2.0 / MCP) with
two independent server processes sharing one mailbox dir — exactly how the
persistent worker and the fresh reviewer attach to it. Never touches the
network.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import mailbox

SERVER = Path(__file__).resolve().parent.parent / "app" / "mailbox_server.py"


class Rpc:
    """A tiny MCP stdio client: one server process with one role."""

    def __init__(self, mdir: Path, role: str, with_dir: bool = True,
                 round: str | None = None):
        env = dict(os.environ)
        if with_dir:
            env["VIBE_HARNESS_MAILBOX_DIR"] = str(mdir)
            env["VIBE_HARNESS_MAILBOX_ROLE"] = role
            if round:
                env["VIBE_HARNESS_MAILBOX_ROUND"] = round
        else:
            env.pop("VIBE_HARNESS_MAILBOX_DIR", None)
            env.pop("VIBE_HARNESS_MAILBOX_ROLE", None)
            env.pop("VIBE_HARNESS_MAILBOX_ROUND", None)
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            assert line, "mailbox server exited unexpectedly"
            resp = json.loads(line)
            if resp.get("id") == self._id:
                return resp

    def notify(self, method: str) -> None:
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def tool(self, name: str, arguments: dict) -> dict:
        resp = self.call("tools/call", {"name": name, "arguments": arguments})
        assert "result" in resp, resp
        return resp["result"]

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def mdir(tmp_path) -> Path:
    d = tmp_path / "mailbox"
    (d / "messages").mkdir(parents=True)
    return d


def test_initialize_and_tools_list(mdir):
    c = Rpc(mdir, "reviewer")
    try:
        r = c.call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}})
        assert r["result"]["serverInfo"]["name"] == "vibe-harness-mailbox"
        assert "tools" in r["result"]["capabilities"]
        c.notify("notifications/initialized")   # notification: no response, no crash
        r = c.call("tools/list")
        names = sorted(t["name"] for t in r["result"]["tools"])
        assert names == ["post_message", "submit_verdict", "wait_for_message"]
        r = c.call("ping")
        assert r["result"] == {}
    finally:
        c.close()


def test_message_exchange_between_roles(mdir):
    worker = Rpc(mdir, "worker")
    reviewer = Rpc(mdir, "reviewer")
    try:
        # Reviewer asks the persistent worker a question mid-review.
        out = reviewer.tool("post_message", {"to": "worker", "text": "why round 1?"})
        assert out["isError"] is False
        msg = out["structuredContent"]
        assert msg["from"] == "reviewer" and msg["to"] == "worker"
        assert msg["text"] == "why round 1?" and msg["seq"] == 1

        # The worker blocks until its message arrives and consumes it.
        out = worker.tool("wait_for_message", {"timeout_seconds": 5})
        got = out["structuredContent"]["message"]
        assert got["text"] == "why round 1?" and got["seq"] == 1

        # Already consumed -> a second wait times out with none.
        out = worker.tool("wait_for_message", {"timeout_seconds": 0.3})
        assert out["structuredContent"]["none"] is True

        # The worker replies to the reviewer.
        out = worker.tool("post_message", {"to": "reviewer", "text": "fixed in round 2"})
        assert out["structuredContent"]["seq"] == 2
        out = reviewer.tool("wait_for_message", {"timeout_seconds": 5})
        assert out["structuredContent"]["message"]["text"] == "fixed in round 2"

        # The harness side sees the full exchange.
        msgs = mailbox.list_messages(mdir)
        assert [(m["from"], m["to"]) for m in msgs] == [
            ("reviewer", "worker"), ("worker", "reviewer")]
    finally:
        worker.close()
        reviewer.close()


def test_submit_verdict_is_scoped_per_round_and_read_by_harness(mdir):
    # Round review-1's reviewer submits a rejection.
    r1 = Rpc(mdir, "reviewer", round="review-1")
    try:
        out = r1.tool("submit_verdict", {"approved": False,
                                         "reason": "tests fail in round 1"})
        assert out["isError"] is False
        assert out["structuredContent"]["ok"] is True
        verdict = out["structuredContent"]["verdict"]
        assert verdict["from"] == "reviewer" and verdict["round"] == "review-1"
    finally:
        r1.close()
    assert mailbox.read_verdict(mdir, round="review-1") == (False, "tests fail in round 1")
    # A different round sees NO verdict — no inheritance across review rounds.
    assert mailbox.read_verdict(mdir, round="review-2") is None
    # Round-less read returns the latest verdict.
    assert mailbox.read_verdict(mdir) == (False, "tests fail in round 1")

    # Round review-2's reviewer approves; round 1's verdict file stays intact.
    r2 = Rpc(mdir, "reviewer", round="review-2")
    try:
        r2.tool("submit_verdict", {"approved": True, "reason": ""})
    finally:
        r2.close()
    assert mailbox.read_verdict(mdir, round="review-2") == (True, None)
    assert mailbox.read_verdict(mdir, round="review-1") == (False, "tests fail in round 1")
    # Latest mirror updated; empty reason -> None.
    assert mailbox.read_verdict(mdir) == (True, None)

    # Clearing a round's verdict (what the harness does before spawning a
    # review round) makes that round unreadable again.
    assert mailbox.clear_round_verdict(mdir, "review-2") is True
    assert mailbox.read_verdict(mdir, round="review-2") is None
    assert mailbox.clear_round_verdict(mdir, "review-2") is False   # already gone
    assert mailbox.read_verdict(mdir, round="review-1") == (False, "tests fail in round 1")


def test_submit_verdict_rejected_for_non_reviewer_role(mdir):
    # The worker must not be able to submit (or forge) a verdict.
    worker = Rpc(mdir, "worker", round="run-1")
    try:
        resp = worker.call("tools/call", {"name": "submit_verdict",
                                          "arguments": {"approved": True,
                                                        "reason": "self-approve"}})
        assert "error" in resp                       # JSON-RPC error, no write
        assert mailbox.read_verdict(mdir, round="run-1") is None
        assert mailbox.read_verdict(mdir) is None
        # The server stays alive and other tools still work.
        out = worker.tool("post_message", {"to": "reviewer", "text": "hi"})
        assert out["isError"] is False
    finally:
        worker.close()


def test_read_verdict_ignores_verdicts_not_from_reviewer(mdir):
    # Read-side guard: a verdict file whose `from` is not the reviewer role is
    # not a verdict, even with the right shape (no mis-attribution via
    # "submit_verdict").
    vdir = mdir / "verdicts"
    vdir.mkdir(parents=True)
    (vdir / "review-1.json").write_text(
        json.dumps({"approved": True, "reason": "forged", "from": "worker"}))
    (mdir / "verdict.json").write_text(
        json.dumps({"approved": True, "reason": "forged", "from": "worker"}))
    assert mailbox.read_verdict(mdir, round="review-1") is None
    assert mailbox.read_verdict(mdir) is None
    # Malformed files -> None, no crash.
    (vdir / "review-2.json").write_text("{not json")
    assert mailbox.read_verdict(mdir, round="review-2") is None
    (vdir / "review-3.json").write_text(json.dumps({"nope": 1}))
    assert mailbox.read_verdict(mdir, round="review-3") is None


def test_protocol_errors(mdir):
    c = Rpc(mdir, "reviewer")
    try:
        r = c.call("no/such_method")
        assert r["error"]["code"] == -32601
        r = c.call("tools/call", {"name": "no_such_tool", "arguments": {}})
        assert r["error"]["code"] == -32602
        r = c.call("tools/call", {"name": "post_message",
                                  "arguments": {"to": "nobody", "text": "x"}})
        assert r["error"]["code"] == -32602
        r = c.call("tools/call", {"name": "submit_verdict", "arguments": {}})
        assert r["error"]["code"] == -32602
    finally:
        c.close()


def test_missing_mailbox_dir_is_a_tool_level_error(tmp_path):
    c = Rpc(tmp_path, "reviewer", with_dir=False)
    try:
        r = c.call("tools/call", {"name": "post_message",
                                  "arguments": {"to": "worker", "text": "hi"}})
        assert "error" in r          # JSON-RPC error, server stays alive
        r = c.call("tools/list")     # still responsive afterwards
        assert "result" in r
    finally:
        c.close()


# ------------------------------------------------------------- harness side

git_test = pytest.mark.skipif(mailbox.gitops.git_available() is False,
                              reason="git not available")


@git_test
def test_setup_mailbox_writes_config_and_git_exclude(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    sdir = tmp_path / "sessions" / "abc123"
    record = mailbox.setup_mailbox(repo, sdir)

    cfg = (repo / ".vibe" / "config.toml").read_text()
    assert mailbox.CONFIG_MARKER in cfg
    assert 'name = "harness-mailbox"' in cfg
    assert "mailbox_server.py" in cfg
    assert record["dir"] == str(sdir / "mailbox")
    assert (sdir / "mailbox" / "messages").is_dir()
    assert record["git_excluded"] is True
    # `.vibe/` is excluded in the worktree-local info/exclude -> clean tree.
    assert not mailbox.gitops.is_dirty(repo)
    proc = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"],
                          capture_output=True, text=True)
    assert ".vibe" not in proc.stdout


@git_test
def test_setup_mailbox_appends_to_existing_config(tmp_path):
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    cfg_dir = repo / ".vibe"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text('active_model = "my-model"\n')
    mailbox.setup_mailbox(repo, tmp_path / "sessions" / "x")
    cfg = (cfg_dir / "config.toml").read_text()
    assert 'active_model = "my-model"' in cfg       # existing content kept
    assert mailbox.CONFIG_MARKER in cfg              # managed block appended
