"""Hermetic tests for SessionRunner.

Uses a fake `vibe` binary (shell scripts in tmp_path) — never the real vibe,
never the network.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app import gitops, mailbox
from app.config import Config, load_config
from app.gitops import GitBusyError
from app.cards import CardRunning, CardStore
from app.projects import ProjectStore
from app.runner import (
    NotRunningError,
    SessionRunner,
    TestFailedError,
    WorkdirNotAllowed,
    fix_prompt_template,
    prompt_template,
    resolve_prompt_template,
    review_prompt_template,
)

FAKE_VIBE_OK = """#!/bin/sh
# Fake vibe: writes a file into --workdir, a fake export.json into --output-dir.
# Review mode (--agent ask) prints a verdict instead of implementing.
# FAKE_REVIEW=reject-once rejects the first review round, then approves;
# FAKE_REVIEW=always-reject never approves (loop-cap test).
# Resolver mode (round dir resolve-N): FAKE_RESOLVE=conclude (default) runs
# `git add -A && git commit` to conclude the in-progress merge, fail exits 1,
# hang sleeps until killed.
ARGS="$@"
WORKDIR=""
OUTDIR=""
AGENT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) WORKDIR="$2"; shift 2 ;;
    --output-dir) OUTDIR="$2"; shift 2 ;;
    --agent) AGENT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
round_dir=$(basename "$(dirname "$OUTDIR")")
case "$round_dir" in
  resolve-*)
    case "$FAKE_RESOLVE" in
      fail) echo "fake resolver failing"; exit 1 ;;
      hang)
        trap 'echo "fake resolver killed"; exit 0' TERM
        echo "fake resolver started"
        sleep 30
        exit 0
        ;;
      *)
        git add -A
        git commit -q -m "resolver: conclude merge"
        echo "fake resolver concluded merge"
        exit 0
        ;;
    esac
    ;;
esac
echo "hello from fake vibe"
mkdir -p "$WORKDIR"
echo "hello from fake vibe" > "$WORKDIR/hello.txt"
mkdir -p "$OUTDIR"
echo "$ARGS" > "$OUTDIR/args.txt"
env | grep '^VIBE_HARNESS_MAILBOX' > "$OUTDIR/env.txt" || true
cat > "$OUTDIR/export.json" <<'JSON'
{"cost_usd": 0.01, "usage": {"total_tokens": 42}, "config": {"active_model": "fake-model"}, "stop_reason": "end_turn", "outcome": "success", "session_id": "fake-worker-session"}
JSON
if [ "$AGENT" = "ask" ]; then
  # Review verdict by round: the round dir is <kind>-<n>, export is inside it.
  n=${round_dir#review-}
  case "$FAKE_REVIEW" in
    always-reject)
      echo "VERDICT: REJECTED: fake reviewer always finds a bug" ;;
    reject-once)
      if [ "$n" -lt 2 ]; then
        echo "VERDICT: REJECTED: fake reviewer found a bug in round $n"
      else
        echo "VERDICT: APPROVED"
      fi ;;
    *)
      echo "VERDICT: APPROVED" ;;
  esac
fi
sleep 0.2
exit 0
"""

FAKE_VIBE_FAIL = """#!/bin/sh
# Fake vibe that always fails.
echo "fake vibe failing on purpose"
exit 1
"""

FAKE_VIBE_ROUND = """#!/bin/sh
# Like FAKE_VIBE_OK but writes round-specific content, so every run round
# produces a new commit (needed for diff-scoped review prompts).
ARGS="$@"
WORKDIR=""
OUTDIR=""
AGENT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) WORKDIR="$2"; shift 2 ;;
    --output-dir) OUTDIR="$2"; shift 2 ;;
    --agent) AGENT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
round_dir=$(basename "$(dirname "$OUTDIR")")
mkdir -p "$WORKDIR"
if [ "$AGENT" != "ask" ]; then
  # Worker mode only: the reviewer must not modify the workspace.
  echo "hello from fake vibe ($round_dir)" > "$WORKDIR/hello.txt"
fi
mkdir -p "$OUTDIR"
echo "$ARGS" > "$OUTDIR/args.txt"
env | grep '^VIBE_HARNESS_MAILBOX' > "$OUTDIR/env.txt" || true
cat > "$OUTDIR/export.json" <<'JSON'
{"usage": {"total_tokens": 42}, "config": {"active_model": "fake-model"}, "stop_reason": "end_turn", "session_id": "fake-worker-session"}
JSON
if [ "$AGENT" = "ask" ]; then
  n=${round_dir#review-}
  case "$FAKE_REVIEW" in
    always-reject) echo "VERDICT: REJECTED: fake reviewer always finds a bug" ;;
    reject-once)
      if [ "$n" -lt 2 ]; then
        echo "VERDICT: REJECTED: fake reviewer found a bug in round $n"
      else
        echo "VERDICT: APPROVED"
      fi ;;
    *) echo "VERDICT: APPROVED" ;;
  esac
fi
sleep 0.2
exit 0
"""

FAKE_VIBE_MCP = r'''#!/usr/bin/env python3
"""Fake vibe that talks to the REAL mailbox MCP server (app/mailbox_server.py).

Worker mode (no --agent ask): writes hello.txt into --workdir; when resumed
(--resume <id>) also writes fix.txt and does a mailbox exchange: wait for the
reviewer's question, then reply to it. Writes export.json with session_id
(the resumed id when resuming).

Review mode (--agent ask): drives the mailbox over stdio JSON-RPC —
initialize, tools/list, post_message(to="worker"), submit_verdict(round 1:
rejected, round 2+: approved) — and prints a deliberately MISLEADING VERDICT:
text line, so tests prove the structured submit_verdict wins.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

VALUE_FLAGS = {"--workdir", "--output-dir", "--agent", "--prompt-file",
               "--time-limit", "--max-price", "--resume"}


def parse_args(argv):
    opts, i = {}, 0
    while i < len(argv):
        a = argv[i]
        if a in VALUE_FLAGS and i + 1 < len(argv):
            opts[a] = argv[i + 1]
            i += 2
        else:
            i += 1
    return opts


def rpc(proc, msg_id, method, params=None):
    req = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        req["params"] = params
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("mailbox server exited")
        resp = json.loads(line)
        if resp.get("id") == msg_id:
            return resp


def start_mailbox():
    server = os.environ.get("VIBE_HARNESS_MAILBOX_SERVER")
    if not server:
        return None
    proc = subprocess.Popen([sys.executable, server], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    rpc(proc, 1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": "fake", "version": "0"}})
    proc.stdin.write(json.dumps({"jsonrpc": "2.0",
                                 "method": "notifications/initialized"}) + "\n")
    proc.stdin.flush()
    return proc


def worker_chat():
    """Resumed worker: read the reviewer's question, reply to it."""
    proc = start_mailbox()
    if proc is None:
        return
    try:
        r = rpc(proc, 2, "tools/call", {"name": "wait_for_message",
                                        "arguments": {"timeout_seconds": 15}})
        assert r["result"]["isError"] is False, r
        r = rpc(proc, 3, "tools/call", {"name": "post_message",
                                        "arguments": {"to": "reviewer",
                                                      "text": "fixed; see fix.txt"}})
        assert r["result"]["isError"] is False, r
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def run_review(outdir, round_dir):
    n = int(round_dir.split("-")[1])
    # FAKE_NO_VERDICT_FROM=<n>: rounds >= n never call submit_verdict (the
    # harness must then NOT inherit an earlier round's structured verdict).
    skip_from = os.environ.get("FAKE_NO_VERDICT_FROM")
    proc = start_mailbox()
    if proc is None:
        print("no mailbox server configured; text verdict only")
        print("VERDICT: APPROVED" if n >= 2 else "VERDICT: REJECTED: no mailbox")
        return
    try:
        r = rpc(proc, 2, "tools/list")
        names = sorted(t["name"] for t in r["result"]["tools"])
        Path(outdir, "tools.txt").write_text("\n".join(names) + "\n")
        # Ask the persistent worker a question mid-review.
        r = rpc(proc, 3, "tools/call", {"name": "post_message",
                                        "arguments": {"to": "worker",
                                                      "text": f"review {n}: why this change?"}})
        assert r["result"]["isError"] is False, r
        approved = n >= 2
        reason = "" if approved else f"tests fail in round {n}"
        if skip_from and n >= int(skip_from):
            # No submit_verdict this round — verdict only via the log line.
            print("VERDICT: APPROVED" if approved
                  else f"VERDICT: REJECTED: log-only round {n}")
        else:
            r = rpc(proc, 4, "tools/call", {"name": "submit_verdict",
                                            "arguments": {"approved": approved,
                                                          "reason": reason}})
            assert r["result"]["isError"] is False, r
            # Misleading fallback text — the structured verdict must win.
            print("VERDICT: APPROVED" if not approved
                  else "VERDICT: REJECTED: misleading log line")
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def main():
    opts = parse_args(sys.argv[1:])
    workdir = Path(opts.get("--workdir", "."))
    outdir = Path(opts.get("--output-dir", "export"))
    agent = opts.get("--agent", "")
    resume = opts.get("--resume")
    round_dir = outdir.parent.name
    workdir.mkdir(parents=True, exist_ok=True)
    if agent != "ask":
        # Worker mode only: the reviewer must not modify the workspace.
        (workdir / "hello.txt").write_text(f"hello from fake vibe ({round_dir})\n")
        if resume:
            (workdir / "fix.txt").write_text(f"fixed in resumed round {round_dir}\n")
            worker_chat()
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "args.txt").write_text(" ".join(sys.argv[1:]) + "\n")
    env_txt = "\n".join(f"{k}={v}" for k, v in os.environ.items()
                        if k.startswith("VIBE_HARNESS_MAILBOX"))
    (outdir / "env.txt").write_text(env_txt + "\n")
    session_id = resume if resume else "worker-session-1"
    (outdir / "export.json").write_text(json.dumps({
        "usage": {"total_tokens": 42},
        "config": {"active_model": "fake-model"},
        "stop_reason": "end_turn",
        "session_id": session_id,
    }))
    if agent == "ask":
        run_review(str(outdir), round_dir)
    print("fake vibe done")


main()
'''

FAKE_VIBE_SLOW = """#!/bin/sh
# Slow fake vibe for stop() tests: traps SIGTERM and exits promptly.
trap 'echo "fake vibe: got SIGTERM, exiting"; exit 0' TERM
echo "slow fake vibe started"
sleep 30
echo "slow fake vibe finished (unexpected)"
exit 0
"""

FAKE_VIBE_HANG_REVIEW = """#!/bin/sh
# Impl mode: succeed immediately. Review mode (--agent ask): hang until killed.
WORKDIR=""
OUTDIR=""
AGENT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) WORKDIR="$2"; shift 2 ;;
    --output-dir) OUTDIR="$2"; shift 2 ;;
    --agent) AGENT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
if [ "$AGENT" = "ask" ]; then
  trap 'echo "fake reviewer: got SIGTERM, exiting"; exit 0' TERM
  echo "fake reviewer started"
  sleep 30
  exit 0
fi
echo "hello from fake vibe"
mkdir -p "$WORKDIR"
echo "hello from fake vibe" > "$WORKDIR/hello.txt"
mkdir -p "$OUTDIR"
cat > "$OUTDIR/export.json" <<'JSON'
{"usage": {"total_tokens": 42}, "config": {"active_model": "fake-model"}, "stop_reason": "end_turn", "outcome": "success"}
JSON
exit 0
"""


def _write_script(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_vibe(tmp_path) -> Path:
    return _write_script(tmp_path / "fake_vibe.sh", FAKE_VIBE_OK)


@pytest.fixture
def fake_vibe_fail(tmp_path) -> Path:
    return _write_script(tmp_path / "fake_vibe_fail.sh", FAKE_VIBE_FAIL)


@pytest.fixture
def fake_vibe_slow(tmp_path) -> Path:
    return _write_script(tmp_path / "fake_vibe_slow.sh", FAKE_VIBE_SLOW)


@pytest.fixture
def fake_vibe_hang_review(tmp_path) -> Path:
    return _write_script(tmp_path / "fake_vibe_hang_review.sh", FAKE_VIBE_HANG_REVIEW)


@pytest.fixture
def store(tmp_path) -> CardStore:
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    return CardStore(cards_dir)


@pytest.fixture
def make_runner(store, tmp_path):
    def _make(vibe_bin: Path, **config_kwargs) -> SessionRunner:
        config = Config(vibe_bin=str(vibe_bin), time_limit_seconds=30, **config_kwargs)
        return SessionRunner(store, config, sessions_dir=tmp_path / "sessions")
    return _make


def _wait_for_status(store: CardStore, card_id: str, status: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    card = store.get(card_id)
    while card["status"] != status:
        if time.time() > deadline:
            raise AssertionError(
                f"card {card_id}: status is {card['status']!r}, "
                f"expected {status!r} within {timeout}s"
            )
        time.sleep(0.05)
        card = store.get(card_id)
    return card


def test_run_success(store, make_runner, fake_vibe):
    runner = make_runner(fake_vibe)
    card = store.create("Hello card", "Write a hello file.")
    card_id = card["id"]

    returned = runner.run(card_id)
    assert returned["status"] == "running"
    assert runner.is_running(card_id)

    final = _wait_for_status(store, card_id, "done")
    assert not runner.is_running(card_id)

    sdir = runner.sessions_dir / card_id
    assert (sdir / "workspace" / "hello.txt").exists()

    session = final.get("session") or {}
    assert "cost_usd" not in session
    assert session.get("tokens") == 42
    assert session.get("model") == "fake-model"
    assert session.get("stop_reason") == "end_turn"
    assert session.get("exit_code") == 0
    assert session.get("pid")
    assert session.get("started_at")
    assert session.get("finished_at")
    # The run went through the reviewer and was approved.
    assert session["review"]["approved"] is True
    assert session["review"]["reason"] is None
    assert session["review_rounds"] == 1

    prompt = (sdir / "run-1" / "prompt.md").read_text()
    assert "You are a Mistral Vibe agent" in prompt
    assert "# Spec" in prompt
    assert "Write a hello file." in prompt
    assert "agent.md" in prompt          # sessions are told to follow agent.md rules

    log = (sdir / "run-1" / "session.log").read_text()
    assert "hello from fake vibe" in log

    # Sandbox runs use the default workspace and are NOT passed --trust.
    args = (sdir / "run-1" / "export" / "args.txt").read_text()
    assert "--workdir" in args
    assert str(sdir / "workspace") in args
    assert "--trust" not in args

    # The reviewer ran read-only (--agent ask) in its own round dir.
    review_args = (sdir / "review-1" / "export" / "args.txt").read_text()
    assert "--agent ask" in review_args
    assert "--trust" not in review_args
    review_prompt = (sdir / "review-1" / "prompt.md").read_text()
    assert "VERDICT: APPROVED" in review_prompt
    assert "Write a hello file." in review_prompt
    review_log = (sdir / "review-1" / "session.log").read_text()
    assert "VERDICT: APPROVED" in review_log


def test_double_run_raises(store, make_runner, fake_vibe_slow):
    runner = make_runner(fake_vibe_slow)
    card = store.create("Slow card", "Take your time.")
    card_id = card["id"]

    runner.run(card_id)
    with pytest.raises(CardRunning):
        runner.run(card_id)

    runner.stop(card_id)
    _wait_for_status(store, card_id, "stopped")


def test_run_failure(store, make_runner, fake_vibe_fail):
    runner = make_runner(fake_vibe_fail)
    card = store.create("Failing card", "Break things.")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "failed")
    session = final.get("session") or {}
    assert session.get("exit_code") == 1


def test_stop(store, make_runner, fake_vibe_slow):
    runner = make_runner(fake_vibe_slow)
    card = store.create("Stoppable card", "Run for a while.")
    card_id = card["id"]

    runner.run(card_id)
    assert runner.is_running(card_id)

    runner.stop(card_id)
    final = _wait_for_status(store, card_id, "stopped")
    assert not runner.is_running(card_id)
    assert (final.get("session") or {}).get("finished_at")


def test_stop_not_running(store, make_runner, fake_vibe):
    runner = make_runner(fake_vibe)
    card = store.create("Idle card", "Never run.")
    with pytest.raises(NotRunningError):
        runner.stop(card["id"])


def test_recover_stale(store, make_runner, fake_vibe):
    runner = make_runner(fake_vibe)
    card = store.create("Stale card", "Left running by a previous process.")
    card_id = card["id"]
    store.set_status(card_id, "running")

    runner.recover_stale()

    recovered = store.get(card_id)
    assert recovered["status"] == "failed"
    session = recovered.get("session") or {}
    assert session.get("finished_at")
    assert session.get("exit_code") is None


def test_recover_stale_keeps_live_pid(store, make_runner, fake_vibe):
    # A card whose session process is still alive must NOT be failed on
    # startup recovery (e.g. a --reload restart while a vibe child runs);
    # it is re-adopted into the registry so it still finalizes on exit.
    import os
    runner = make_runner(fake_vibe)
    card = store.create("Live card", "Session process still alive.")
    card_id = card["id"]
    store.set_status(card_id, "running",
                     session_patch={"pid": os.getpid()})

    runner.recover_stale()

    assert store.get(card_id)["status"] == "running"
    with runner._lock:
        entry = runner._registry.get(card_id)
    assert entry is not None and entry.get("adopted") is True
    assert entry["proc"].pid == os.getpid()
    # cleanup so the finalize thread does not outlive the test
    with runner._lock:
        runner._registry.pop(card_id, None)


def test_recover_stale_adopted_session_finalizes_on_exit(store, make_runner, fake_vibe, tmp_path):
    # A re-adopted session whose process exits is finalized (status leaves
    # 'running') using the round's export.json exit code.
    import os, subprocess as sp
    runner = make_runner(fake_vibe)
    card = store.create("Adopted card", "Dies while adopted.")
    card_id = card["id"]
    rdir = tmp_path / "sessions" / card_id / "run-1"
    (rdir / "export").mkdir(parents=True)
    (rdir / "export" / "export.json").write_text(
        '{"usage": {"total_tokens": 7}, "exit_code": 0, "session_id": "s1"}')
    proc = sp.Popen(["sleep", "0.3"])          # dies on its own
    threading.Thread(target=proc.wait, daemon=True).start()  # reap (avoid zombie)
    store.set_status(card_id, "running", session_patch={
        "pid": proc.pid, "export_dir": str(rdir / "export"),
        "log": str(rdir / "session.log")})

    runner.recover_stale()
    assert store.get(card_id)["status"] == "running"

    deadline = time.time() + 10
    while time.time() < deadline and store.get(card_id)["status"] == "running":
        time.sleep(0.1)
    final = store.get(card_id)
    assert final["status"] != "running"
    assert (final["session"].get("tokens")) == 7


def test_log_tail(store, make_runner, fake_vibe):
    runner = make_runner(fake_vibe)
    card = store.create("Logged card", "Say hello.")
    card_id = card["id"]
    never_run = store.create("Idle card", "No session yet.")

    # No session yet -> empty string, no crash.
    assert runner.log_tail(never_run["id"]) == ""

    runner.run(card_id)
    _wait_for_status(store, card_id, "done")

    tail = runner.log_tail(card_id)
    assert isinstance(tail, str)
    assert "hello from fake vibe" in tail
    # The card's session log points at the latest (reviewer) session.
    assert runner.log_tail(card_id, n=1) == "VERDICT: APPROVED"


def test_run_from_done_starts_fresh(store, make_runner, fake_vibe):
    runner = make_runner(fake_vibe)
    card = store.create("Re-runnable card", "Do it again.")
    card_id = card["id"]

    runner.run(card_id)
    first = _wait_for_status(store, card_id, "done")
    assert first["session"]["exit_code"] == 0

    # Re-run without editing: the card resets to draft and runs again.
    again = runner.run(card_id)
    assert again["status"] == "running"
    second = _wait_for_status(store, card_id, "done")
    assert second["session"]["pid"] != first["session"]["pid"]
    assert second["accepted"] is False


def test_run_with_custom_workdir(store, make_runner, fake_vibe, tmp_path):
    runner = make_runner(fake_vibe)
    target = tmp_path / "my-project"
    card = store.create("P", "work here", workdir=str(target))

    runner.run(card["id"])
    final = _wait_for_status(store, card["id"], "done")

    # Git-backed run: the fake vibe worked in the card's linked worktree
    # (sessions/<id>/worktree), not in the custom folder itself.
    wt = runner.sessions_dir / card["id"] / "worktree"
    assert (wt / "hello.txt").exists()
    assert not (target / "hello.txt").exists()
    assert not (runner.sessions_dir / card["id"] / "workspace").exists()
    assert final["session"]["workdir"] == str(wt.resolve())
    # Custom workdirs are trusted for this invocation only.
    args = (runner.sessions_dir / card["id"] / "run-1" / "export" / "args.txt").read_text()
    assert "--trust" in args
    assert str(wt.resolve()) in args


def test_resolve_workdir():
    from app.runner import resolve_workdir
    assert resolve_workdir("/abs/x") == Path("/abs/x")
    assert resolve_workdir("rel/x", base_dir=Path("/base")) == Path("/base/rel/x")
    assert resolve_workdir("~/x") == Path.home() / "x"


def test_workdir_allowlist(store, fake_vibe, tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = Config(vibe_bin=str(fake_vibe), time_limit_seconds=30,
                    allowed_workdirs=[str(allowed)])
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions")

    card = store.create("P", "b", workdir=str(tmp_path / "elsewhere"))
    with pytest.raises(WorkdirNotAllowed):
        runner.run(card["id"])

    ok = store.create("Q", "b", workdir=str(allowed / "sub"))
    runner.run(ok["id"])
    final = _wait_for_status(store, ok["id"], "done")
    wt = runner.sessions_dir / ok["id"] / "worktree"
    assert (wt / "hello.txt").exists()
    assert final["session"]["workdir"] == str(wt.resolve())


def test_run_uses_project_path(store, fake_vibe, tmp_path):
    from app.projects import ProjectStore

    proj_dir = tmp_path / "projdir"
    projects = ProjectStore(tmp_path / "projects.json")
    projects.create("my-app", str(proj_dir))
    config = Config(vibe_bin=str(fake_vibe), time_limit_seconds=30)
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects)

    card = store.create("P", "b", project="my-app")
    runner.run(card["id"])
    final = _wait_for_status(store, card["id"], "done")

    # Vibe worked in the card's linked worktree, not the project folder itself.
    wt = runner.sessions_dir / card["id"] / "worktree"
    assert (wt / "hello.txt").exists()
    assert not (proj_dir / "hello.txt").exists()
    assert not (tmp_path / "sessions" / card["id"] / "workspace").exists()
    assert final["session"]["workdir"] == str(wt.resolve())


def test_card_workdir_overrides_project_path(store, fake_vibe, tmp_path):
    from app.projects import ProjectStore

    proj_dir = tmp_path / "projdir"
    override = tmp_path / "override"
    projects = ProjectStore(tmp_path / "projects.json")
    projects.create("my-app", str(proj_dir))
    config = Config(vibe_bin=str(fake_vibe), time_limit_seconds=30)
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects)

    card = store.create("P", "b", project="my-app", workdir=str(override))
    runner.run(card["id"])
    _wait_for_status(store, card["id"], "done")

    wt = runner.sessions_dir / card["id"] / "worktree"
    assert (wt / "hello.txt").exists()
    assert not (override / "hello.txt").exists()
    assert not (proj_dir / "hello.txt").exists()


# ----------------------------------------------------------------------
# review loop
# ----------------------------------------------------------------------

def test_review_reject_then_fix_loop(store, make_runner, fake_vibe, monkeypatch):
    """A rejection sends the card back to running with a fixer session that
    receives the reason; the loop repeats until the reviewer approves."""
    monkeypatch.setenv("FAKE_REVIEW", "reject-once")
    runner = make_runner(fake_vibe)
    card = store.create("Loop card", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    session = final["session"]
    assert session["review"]["approved"] is True
    assert session["review_rounds"] == 2

    sdir = runner.sessions_dir / card_id
    # Rounds: run-1 (impl), review-1 (rejected), run-2 (fixer), review-2 (ok).
    assert (sdir / "run-1" / "session.log").exists()
    assert (sdir / "review-1" / "session.log").exists()
    assert (sdir / "run-2" / "session.log").exists()
    assert (sdir / "review-2" / "session.log").exists()
    review1 = (sdir / "review-1" / "session.log").read_text()
    assert "VERDICT: REJECTED: fake reviewer found a bug in round 1" in review1
    # The fixer prompt carries the rejection reason and the original spec.
    fix_prompt = (sdir / "run-2" / "prompt.md").read_text()
    assert "REJECTED by the automated reviewer" in fix_prompt
    assert "fake reviewer found a bug in round 1" in fix_prompt
    assert "Write a hello file." in fix_prompt
    assert "test-driven development" in fix_prompt   # fixer rounds are TDD too


def test_review_loop_cap_requires_manual_accept(store, make_runner, fake_vibe, monkeypatch):
    """After max_review_rounds rejections the loop stops: the card lands in
    'done' without an approved review (review.exhausted) and must be accepted
    manually — auto_accept does NOT fire without an approval."""
    monkeypatch.setenv("FAKE_REVIEW", "always-reject")
    runner = make_runner(fake_vibe, max_review_rounds=2)
    card = store.create("Hopeless card", "Never good enough.", auto_accept=True)
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    session = final["session"]
    assert session["review"]["approved"] is False
    assert session["review"]["exhausted"] is True
    assert session["review"]["reason"] == "fake reviewer always finds a bug"
    assert session["review_rounds"] == 2
    # auto_accept only fires on approval: the card stays done, unaccepted.
    assert final["status"] == "done"
    assert final["accepted"] is False
    # Manual acceptance is allowed once the rounds are exhausted.
    accepted = store.accept(card_id)
    assert accepted["status"] == "accepted"


def test_auto_accept_after_approval(store, make_runner, fake_vibe):
    """auto_accept: the card is accepted as soon as the reviewer approves."""
    runner = make_runner(fake_vibe)
    card = store.create("Hands-free card", "Write a hello file.", auto_accept=True)
    card_id = card["id"]
    assert card["auto_accept"] is True

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "accepted")
    assert final["accepted"] is True
    assert final["session"]["review"]["approved"] is True


def test_stop_during_review(store, make_runner, fake_vibe_hang_review):
    runner = make_runner(fake_vibe_hang_review)
    card = store.create("Reviewing card", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    _wait_for_status(store, card_id, "reviewing")
    assert runner.is_running(card_id)

    runner.stop(card_id)
    final = _wait_for_status(store, card_id, "stopped")
    assert not runner.is_running(card_id)
    assert (final.get("session") or {}).get("finished_at")


def test_run_blocked_while_reviewing(store, make_runner, fake_vibe_hang_review):
    runner = make_runner(fake_vibe_hang_review)
    card = store.create("Busy card", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    _wait_for_status(store, card_id, "reviewing")
    with pytest.raises(CardRunning):
        runner.run(card_id)

    runner.stop(card_id)
    _wait_for_status(store, card_id, "stopped")


def test_recover_stale_reviewing(store, make_runner, fake_vibe):
    runner = make_runner(fake_vibe)
    card = store.create("Stale reviewing", "Stuck in review.")
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "reviewing")

    runner.recover_stale()

    assert store.get(card["id"])["status"] == "failed"


# --------------------------------------------------------------------- git

git_test = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def projects(tmp_path) -> ProjectStore:
    return ProjectStore(tmp_path / "projects.json")


def _make_runner_with_projects(store, tmp_path, vibe_bin, projects, **config_kwargs):
    config = Config(vibe_bin=str(vibe_bin), time_limit_seconds=30, **config_kwargs)
    return SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                         projects=projects)


@git_test
def test_git_run_merges_on_auto_accept(store, tmp_path, fake_vibe, projects):
    """A project-folder run is git-backed: the folder is auto-init'd, the card
    runs on harness/<id> in its own linked worktree (sessions/<id>/worktree),
    and auto-accept merges the branch into the base in the main worktree,
    then removes the worktree and the branch."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    card = store.create("Git card", "Write a hello file.", project="my-app",
                       auto_accept=True)
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "accepted")

    git_info = final["session"]["git"]
    assert git_info["branch"] == f"harness/{card_id}"
    assert git_info["merged"] is True
    wt = runner.sessions_dir / card_id / "worktree"
    assert git_info["worktree"] == str(wt.resolve())
    # The run's session workdir was the linked worktree.
    assert final["session"]["workdir"] == str(wt.resolve())
    # The repo was auto-initialized; the main worktree is on the base branch.
    assert _git(["symbolic-ref", "--short", "HEAD"], proj) == git_info["base_branch"]
    assert (proj / "hello.txt").exists()
    # The implementation commit is on the base branch; card branch + worktree gone.
    assert "hello.txt" in _git(
        ["show", "--name-only", "--format=", git_info["base_branch"]], proj)
    assert not gitops.branch_exists(proj, git_info["branch"])
    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(proj)


@git_test
def test_git_run_auto_stashes_dirty_repo(store, tmp_path, fake_vibe, projects):
    """A dirty tree in the MAIN worktree (e.g. left by an interrupted previous
    run) is stashed automatically on a first run and the run proceeds; the
    stash is kept for manual recovery and flagged in the session git record.
    The card branch is checked out in the linked worktree; the main worktree
    stays on the base branch."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    gitops.ensure_repo(proj)
    (proj / "wip.txt").write_text("uncommitted\n")
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    card = store.create("Dirty", "spec", project="my-app")

    runner.run(card["id"])
    final = _wait_for_status(store, card["id"], "done")
    git_info = final["session"]["git"]
    assert git_info["stashed"] is True
    assert git_info["auto_stashed"] is True
    assert gitops.branch_exists(proj, git_info["branch"])
    wt = runner.sessions_dir / card["id"] / "worktree"
    assert _git(["symbolic-ref", "--short", "HEAD"], wt) == git_info["branch"]
    assert _git(["symbolic-ref", "--short", "HEAD"], proj) == git_info["base_branch"]
    # The leftover change went into the stash, not into the run's branch.
    assert not (proj / "wip.txt").exists()
    assert not gitops.is_dirty(proj)
    assert "vibe-harness" in _git(["stash", "list"], proj)


@git_test
def test_git_two_cards_same_repo_run_concurrently(store, tmp_path, fake_vibe_slow, projects):
    """Worktree-per-card: two cards in the same repo run at the same time —
    each on its own branch in its own linked worktree (no GitBusyError) — and
    the project folder (main worktree) stays on the base branch."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe_slow, projects)
    first = store.create("First", "spec", project="my-app")
    second = store.create("Second", "spec", project="my-app")

    runner.run(first["id"])
    runner.run(second["id"])          # concurrent cards in one repo are allowed
    _wait_for_status(store, first["id"], "running")
    _wait_for_status(store, second["id"], "running")

    n1, n2 = store.get(first["id"]), store.get(second["id"])
    g1, g2 = n1["session"]["git"], n2["session"]["git"]
    wt1 = runner.sessions_dir / first["id"] / "worktree"
    wt2 = runner.sessions_dir / second["id"] / "worktree"
    assert g1["repo"] == g2["repo"] == str(proj.resolve())
    assert g1["branch"] != g2["branch"]
    assert g1["worktree"] == str(wt1.resolve())
    assert g2["worktree"] == str(wt2.resolve())
    assert _git(["symbolic-ref", "--short", "HEAD"], wt1) == g1["branch"]
    assert _git(["symbolic-ref", "--short", "HEAD"], wt2) == g2["branch"]
    # The project folder (main worktree) stays on the base branch, clean.
    assert _git(["symbolic-ref", "--short", "HEAD"], proj) == g1["base_branch"]
    assert not gitops.is_dirty(proj)
    # Each session's workdir is its own linked worktree.
    assert n1["session"]["workdir"] == str(wt1.resolve())
    assert n2["session"]["workdir"] == str(wt2.resolve())

    runner.stop(first["id"])
    runner.stop(second["id"])
    _wait_for_status(store, first["id"], "stopped")
    _wait_for_status(store, second["id"], "stopped")


@git_test
def test_delete_card_removes_linked_worktree(store, tmp_path, fake_vibe, projects):
    """Deleting a card removes its linked worktree (registered removal +
    prune) along with its sessions dir; the unmerged branch is kept."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    card = store.create("Gone", "spec", project="my-app")
    runner.run(card["id"])
    _wait_for_status(store, card["id"], "done")
    git_info = store.get(card["id"])["session"]["git"]
    wt = Path(git_info["worktree"])
    assert wt.is_dir()
    assert wt.resolve() in gitops.registered_worktrees(proj)

    runner.delete_card(card["id"])

    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(proj)
    assert not (runner.sessions_dir / card["id"]).exists()
    assert gitops.branch_exists(proj, git_info["branch"])   # kept for manual recovery


def _conflict_setup(store, tmp_path, projects, vibe_bin):
    """A project run whose hello.txt conflicts with the base branch on merge.

    Returns (runner, card_id, proj, git_info). The card is done and the base
    branch has a competing committed edit, so accepting hits a merge conflict.
    """
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    gitops.ensure_repo(proj)
    (proj / "hello.txt").write_text("base version\n")
    _git(["add", "-A"], proj)
    _git(["commit", "-q", "-m", "base hello.txt"], proj)

    runner = _make_runner_with_projects(store, tmp_path, vibe_bin, projects)
    card = store.create("Conflict card", "Write a hello file.", project="my-app")
    runner.run(card["id"])
    done = _wait_for_status(store, card["id"], "done")
    git_info = done["session"]["git"]
    # While the card is done, its work sits in the linked worktree; the main
    # worktree (project folder) is still on the base branch.
    assert Path(git_info["worktree"]).is_dir()
    assert _git(["symbolic-ref", "--short", "HEAD"], proj) == git_info["base_branch"]
    # The user edits the same file on the base branch after the run.
    _git(["checkout", "-q", git_info["base_branch"]], proj)
    (proj / "hello.txt").write_text("user edit\n")
    _git(["add", "hello.txt"], proj)
    _git(["commit", "-q", "-m", "user edit"], proj)
    return runner, card["id"], proj, git_info


def _wait_for_flag(store, card_id, getter, expected, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getter(store.get(card_id)) == expected:
            return store.get(card_id)
        time.sleep(0.05)
    raise AssertionError(f"flag did not become {expected!r} within {timeout}s")


def _commit_on_branch(proj, branch, path, content, message):
    """Advance `branch` with a commit that rewrites `path`. The base branch is
    checked out in the MAIN worktree (the card's work lives in its own linked
    worktree), so this is a plain commit in the project folder — simulating
    the user committing on the base while a card runs."""
    assert _git(["symbolic-ref", "--short", "HEAD"], proj) == branch
    (proj / path).write_text(content)
    _git(["add", path], proj)
    _git(["commit", "-q", "-m", message], proj)


@git_test
def test_git_auto_accept_conflict_resolved_by_model(store, tmp_path, fake_vibe, projects):
    """auto_accept + merge conflict: a resolver session is spawned, concludes
    the merge, and the accept finishes by itself (card accepted + merged)."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    gitops.ensure_repo(proj)
    (proj / "hello.txt").write_text("base version\n")
    _git(["add", "-A"], proj)
    _git(["commit", "-q", "-m", "base hello.txt"], proj)

    runner = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    card = store.create("Conflict card", "Write a hello file.", project="my-app",
                       auto_accept=True)
    card_id = card["id"]

    runner.run(card_id)
    # While the card runs, the base branch moves forward with a competing edit
    # to the same file -> the accept-time merge will conflict.
    base = store.get(card_id)["session"]["git"]["base_branch"]
    _commit_on_branch(proj, base, "hello.txt", "user edit\n", "user edit")

    final = _wait_for_status(store, card_id, "accepted", timeout=15)

    git_info = final["session"]["git"]
    assert git_info["merged"] is True
    assert git_info.get("merge_conflict") is False
    assert _git(["symbolic-ref", "--short", "HEAD"], proj) == git_info["base_branch"]
    assert not gitops.branch_exists(proj, git_info["branch"])
    assert not gitops.merge_in_progress(proj)
    # The linked worktree was removed once the merge concluded.
    assert not Path(git_info["worktree"]).exists()
    assert Path(git_info["worktree"]).resolve() not in gitops.registered_worktrees(proj)
    # The resolver session ran and is recorded under resolve-1/ — in the MAIN
    # worktree (the project folder), where the conflicted merge was left.
    resolver = final["session"].get("resolver") or {}
    assert resolver.get("exit_code") == 0
    assert resolver.get("workdir") == str(proj.resolve())
    rdir = runner.sessions_dir / card_id / "resolve-1"
    assert (rdir / "session.log").exists()
    rprompt = (rdir / "prompt.md").read_text()
    assert git_info["branch"] in rprompt
    assert git_info["base_branch"] in rprompt
    assert "Write a hello file." in rprompt


@git_test
def test_git_accept_conflict_spawns_resolver_and_finishes(store, tmp_path, fake_vibe, projects):
    """Manual accept on a conflicted merge: the card STAYS 'done' flagged
    resolving (card spins), the resolver concludes the merge, then the card
    becomes accepted + merged."""
    runner, card_id, proj, git_info = _conflict_setup(store, tmp_path, projects, fake_vibe)

    returned = runner.accept_card(card_id)
    assert returned["status"] == "done"            # stays in the Done column
    assert returned["session"]["git"]["resolving"] is True
    assert returned["session"]["git"]["merge_conflict"] is True
    assert runner.is_running(card_id)

    final = _wait_for_status(store, card_id, "accepted", timeout=15)
    g = final["session"]["git"]
    assert g["merged"] is True
    assert g["resolving"] is False
    assert not gitops.branch_exists(proj, g["branch"])
    assert _git(["symbolic-ref", "--short", "HEAD"], proj) == g["base_branch"]
    # The linked worktree was removed after the resolver concluded the merge.
    assert not Path(g["worktree"]).exists()
    assert Path(g["worktree"]).resolve() not in gitops.registered_worktrees(proj)


@git_test
def test_git_accept_while_resolver_running_is_busy(store, tmp_path, fake_vibe, projects, monkeypatch):
    monkeypatch.setenv("FAKE_RESOLVE", "hang")
    runner, card_id, proj, git_info = _conflict_setup(store, tmp_path, projects, fake_vibe)

    runner.accept_card(card_id)                    # spawns the (hanging) resolver
    _wait_for_flag(store, card_id,
                   lambda n: (n["session"]["git"] or {}).get("resolving"), True)
    assert runner.is_running(card_id)

    with pytest.raises(GitBusyError):
        runner.accept_card(card_id)

    runner.stop(card_id)                           # kill the resolver
    final = _wait_for_flag(store, card_id,
                           lambda n: (n["session"]["git"] or {}).get("resolving"), False)
    g = final["session"]["git"]
    assert final["status"] == "done"               # still done, conflict open
    assert g["merge_conflict"] is True
    assert gitops.merge_in_progress(proj)
    # Branch AND linked worktree are kept while the conflict is open.
    assert gitops.branch_exists(proj, g["branch"])
    assert Path(g["worktree"]).is_dir()


@git_test
def test_git_resolver_failure_leaves_card_done(store, tmp_path, fake_vibe, projects, monkeypatch):
    monkeypatch.setenv("FAKE_RESOLVE", "fail")
    runner, card_id, proj, git_info = _conflict_setup(store, tmp_path, projects, fake_vibe)

    runner.accept_card(card_id)
    final = _wait_for_flag(store, card_id,
                           lambda n: (n["session"]["git"] or {}).get("resolving"), False)
    g = final["session"]["git"]
    assert final["status"] == "done"
    assert g["merge_conflict"] is True
    assert g.get("resolver_error")
    assert gitops.merge_in_progress(proj)          # conflict still open, retryable
    assert Path(g["worktree"]).is_dir()            # worktree kept on conflict


FAKE_VIBE_RESOLVE_ON_FLAG = """#!/bin/sh
# Like FAKE_VIBE_OK, but a resolve-* round blocks until $GO_FILE exists, then
# concludes the in-progress merge (git add -A && git commit) — so a test can
# queue a draft behind the open conflict and release it on demand.
WORKDIR=""
OUTDIR=""
AGENT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) WORKDIR="$2"; shift 2 ;;
    --output-dir) OUTDIR="$2"; shift 2 ;;
    --agent) AGENT="$2"; shift 2 ;;
    *) shift ;;
  esac
done
round_dir=$(basename "$(dirname "$OUTDIR")")
case "$round_dir" in
  resolve-*)
    while [ ! -f "$GO_FILE" ]; do sleep 0.05; done
    git add -A
    git commit -q -m "resolver: conclude merge"
    echo "fake resolver concluded merge"
    exit 0
    ;;
esac
mkdir -p "$WORKDIR"
echo "hello from fake vibe" > "$WORKDIR/hello.txt"
mkdir -p "$OUTDIR"
cat > "$OUTDIR/export.json" <<'JSON'
{"usage": {"total_tokens": 42}, "config": {"active_model": "fake-model"}, "stop_reason": "end_turn", "session_id": "fake-worker-session"}
JSON
if [ "$AGENT" = "ask" ]; then
  echo "VERDICT: APPROVED"
fi
sleep 0.2
exit 0
"""


@git_test
def test_run_draft_while_merge_conflict_queues_until_resolved(store, tmp_path, projects, monkeypatch):
    """Running a draft while a merge conflict is being applied (MERGE_HEAD in
    the repo, resolver live) must QUEUE the run — no GitError/409, the card
    stays draft flagged queued — and start it automatically once the conflict
    resolves."""
    go_file = tmp_path / "go"
    monkeypatch.setenv("GO_FILE", str(go_file))
    vibe = _write_script(tmp_path / "fake_vibe_flag.sh", FAKE_VIBE_RESOLVE_ON_FLAG)
    runner, card_id, proj, git_info = _conflict_setup(store, tmp_path, projects, vibe)

    runner.accept_card(card_id)                    # conflict -> resolver spawned, merge left in place
    _wait_for_flag(store, card_id,
                   lambda n: (n["session"]["git"] or {}).get("resolving"), True)
    assert gitops.merge_in_progress(proj)

    # A second draft in the same repo: the run queues instead of raising.
    draft = store.create("Queued card", "Write another file.", project="my-app")
    returned = runner.run(draft["id"])
    assert returned["status"] == "draft"           # not an error, not running yet
    assert returned["session"]["queued"] is True
    assert not runner.is_running(draft["id"])
    # Still queued while the conflict is open.
    assert store.get(draft["id"])["session"]["queued"] is True

    # The resolver concludes the merge -> the queued run starts by itself.
    go_file.write_text("go\n")
    final = _wait_for_status(store, draft["id"], "done", timeout=15)
    assert final["session"]["queued"] is False
    g = final["session"]["git"]
    assert g["branch"] == f"harness/{draft['id']}"
    assert not gitops.merge_in_progress(proj)
    # The conflicted card finished its accept too.
    assert _wait_for_status(store, card_id, "accepted", timeout=15)["session"]["git"]["merged"] is True


# ------------------------------------------------------------- test gates

def test_test_gate_pass_records_verified(store, make_runner, fake_vibe):
    """With a passing test command the run records verified=True and still
    goes through the reviewer."""
    runner = make_runner(fake_vibe, test_cmd="exit 0")
    card = store.create("Tested card", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    session = final["session"]
    assert session["verified"] is True
    assert session["review"]["approved"] is True
    assert session["review_rounds"] == 1
    sdir = runner.sessions_dir / card_id
    assert (sdir / "review-1" / "session.log").exists()


def test_test_gate_rejection_loops_fixer_then_exhausts(store, make_runner, fake_vibe):
    """A failing test command is an automatic rejection: the card goes back to
    running with a fixer that receives the test output; after
    max_review_rounds the loop is exhausted and the card lands in 'done'."""
    runner = make_runner(fake_vibe, test_cmd="echo BOOM-OUTPUT; exit 1",
                         max_review_rounds=2)
    card = store.create("Broken tests", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    session = final["session"]
    assert session["review"]["approved"] is False
    assert session["review"]["exhausted"] is True
    assert session["review"]["source"] == "test"
    assert "BOOM-OUTPUT" in session["review"]["reason"]
    assert session["review_rounds"] == 2
    assert session["verified"] is False
    assert "BOOM-OUTPUT" in (session.get("test_output") or "")

    sdir = runner.sessions_dir / card_id
    # Two implementation rounds, no review session ever ran.
    assert (sdir / "run-1" / "session.log").exists()
    assert (sdir / "run-2" / "session.log").exists()
    assert not (sdir / "review-1").exists()
    # The fixer prompt carries the test output as the rejection reason.
    fix_prompt = (sdir / "run-2" / "prompt.md").read_text()
    assert "BOOM-OUTPUT" in fix_prompt
    assert "Write a hello file." in fix_prompt

    # Exhausted -> manual accept is allowed, but the accept-time green gate
    # still blocks while the tests fail.
    with pytest.raises(TestFailedError):
        runner.accept_card(card_id)
    runner.config.test_cmd = "exit 0"   # tests fixed by hand
    accepted = runner.accept_card(card_id)
    assert accepted["status"] == "accepted"
    assert accepted["session"]["verified"] is True


def test_test_gate_timeout_counts_as_failure(store, make_runner, fake_vibe):
    """A test command that exceeds test_timeout_seconds is a rejection whose
    reason says it timed out."""
    runner = make_runner(fake_vibe, test_cmd="sleep 30", test_timeout_seconds=1,
                         max_review_rounds=1)
    card = store.create("Slow tests", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    session = final["session"]
    assert session["review"]["exhausted"] is True
    assert session["review"]["source"] == "test"
    assert "timed out" in session["review"]["reason"]


def test_accept_gate_runs_tests_before_accepting(store, make_runner, fake_vibe):
    """Ship only green: accept re-runs the test command in the workspace and
    raises TestFailedError while it fails; once green, accept succeeds."""
    runner = make_runner(fake_vibe, test_cmd="test ! -f FAIL")
    card = store.create("Gated card", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")
    assert final["session"]["review"]["approved"] is True

    ws = runner.sessions_dir / card_id / "workspace"
    (ws / "FAIL").write_text("x")
    with pytest.raises(TestFailedError) as excinfo:
        runner.accept_card(card_id)
    assert "tests failed" in str(excinfo.value)
    assert store.get(card_id)["status"] == "done"   # not accepted

    (ws / "FAIL").unlink()
    accepted = runner.accept_card(card_id)
    assert accepted["status"] == "accepted"
    assert accepted["session"]["verified"] is True


def test_accept_cmd_alias_still_works(store, make_runner, fake_vibe):
    """The deprecated accept_cmd config is an alias for test_cmd."""
    runner = make_runner(fake_vibe, accept_cmd="exit 0")
    assert runner.config.test_cmd == "exit 0"
    card = store.create("Alias card", "Write a hello file.")
    runner.run(card["id"])
    final = _wait_for_status(store, card["id"], "done")
    assert final["session"]["verified"] is True


# ----------------------------------------------------------------------
# review loop v2: persistent worker (--resume), diff-scoped fresh review,
# per-card MCP mailbox with structured submit_verdict
# ----------------------------------------------------------------------


@pytest.fixture
def fake_vibe_round(tmp_path) -> Path:
    return _write_script(tmp_path / "fake_vibe_round.sh", FAKE_VIBE_ROUND)


@pytest.fixture
def fake_vibe_mcp(tmp_path) -> Path:
    return _write_script(tmp_path / "fake_vibe_mcp.py", FAKE_VIBE_MCP)


def test_worker_resumed_across_fix_rounds(store, make_runner, fake_vibe, monkeypatch):
    """The worker session persists across fix rounds: run-1 spawns fresh,
    the fixer round run-2 resumes it (`--resume <session_id from run-1's
    export.json>`), and the reviewers stay fresh processes (no --resume)."""
    monkeypatch.setenv("FAKE_REVIEW", "reject-once")
    runner = make_runner(fake_vibe)
    card = store.create("Resume card", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    session = final["session"]
    assert session["worker_session_id"] == "fake-worker-session"
    assert session["review"]["approved"] is True
    assert session["review_rounds"] == 2

    sdir = runner.sessions_dir / card_id
    run1_args = (sdir / "run-1" / "export" / "args.txt").read_text()
    run2_args = (sdir / "run-2" / "export" / "args.txt").read_text()
    assert "--resume" not in run1_args
    assert "--resume fake-worker-session" in run2_args
    # Reviewers are fresh every round — never resumed.
    for r in ("review-1", "review-2"):
        assert "--resume" not in (sdir / r / "export" / "args.txt").read_text()
    # The fixer prompt tells the resumed worker its context is retained.
    fix_prompt = (sdir / "run-2" / "prompt.md").read_text()
    assert "RESUMING your previous session" in fix_prompt
    assert "fake reviewer found a bug in round 1" in fix_prompt


def test_worker_resume_disabled_spawns_fresh(store, make_runner, fake_vibe, monkeypatch):
    """VIBE_HARNESS_WORKER_RESUME=0 kill-switch: the fixer round is a fresh
    session (no --resume) and the prompt has no resume card."""
    monkeypatch.setenv("FAKE_REVIEW", "reject-once")
    runner = make_runner(fake_vibe, worker_resume=False)
    card = store.create("No resume", "Write a hello file.")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    sdir = runner.sessions_dir / card_id
    assert "--resume" not in (sdir / "run-2" / "export" / "args.txt").read_text()
    assert "RESUMING" not in (sdir / "run-2" / "prompt.md").read_text()
    assert final["session"]["review"]["approved"] is True


@git_test
def test_review_prompt_scoped_to_diff_since_last_review(store, tmp_path, projects,
                                                         fake_vibe_round, monkeypatch):
    """The fresh reviewer's prompt carries only the spec, the test-suite output,
    and `git diff <last-reviewed-commit>..HEAD` — round 2's diff starts at the
    commit round 1 reviewed, not at the base branch."""
    monkeypatch.setenv("FAKE_REVIEW", "reject-once")
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe_round, projects,
                                        test_cmd="echo TESTS-PASS-123; exit 0")
    card = store.create("Diff card", "Write a hello file.", project="my-app")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    sdir = runner.sessions_dir / card_id
    git_info = final["session"]["git"]
    base_sha = _git(["rev-parse", git_info["base_branch"]], proj)
    last_reviewed = final["session"]["last_reviewed_commit"]
    assert last_reviewed and last_reviewed != base_sha

    p1 = (sdir / "review-1" / "prompt.md").read_text()
    assert "diff --git" in p1 and "hello.txt" in p1
    assert "TESTS-PASS-123" in p1                      # test-suite output included
    assert "submit_verdict" in p1                      # mailbox instructions
    assert base_sha[:12] in p1                         # round 1 diffs from base
    assert final["session"]["review"]["commit"] == last_reviewed

    p2 = (sdir / "review-2" / "prompt.md").read_text()
    assert "diff --git" in p2 and "hello.txt" in p2
    assert last_reviewed[:12] in p2                    # round 2 diffs from round 1's commit
    assert base_sha[:12] not in p2                     # ...not the whole history again


@git_test
def test_mailbox_attached_to_card_worktree(store, tmp_path, fake_vibe, projects):
    """The per-card MCP mailbox is attached via .vibe/config.toml in the card's
    linked worktree (kept out of git via info/exclude), the mailbox state dir
    lives under sessions/<id>/mailbox, and each spawned vibe process gets the
    mailbox dir + its role in the environment."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    card = store.create("Mailbox card", "Write a hello file.", project="my-app")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    sdir = runner.sessions_dir / card_id
    wt = sdir / "worktree"
    cfg = (wt / ".vibe" / "config.toml").read_text()
    assert "harness-mailbox" in cfg and "mailbox_server.py" in cfg
    # The managed .vibe/ never dirties the card branch.
    assert not gitops.is_dirty(wt)
    mb = final["session"]["mailbox"]
    assert mb["dir"] == str(sdir / "mailbox")
    assert mb["git_excluded"] is True
    assert (sdir / "mailbox" / "messages").is_dir()

    env_run = (sdir / "run-1" / "export" / "env.txt").read_text()
    env_rev = (sdir / "review-1" / "export" / "env.txt").read_text()
    assert "VIBE_HARNESS_MAILBOX_ROLE=worker" in env_run
    assert "VIBE_HARNESS_MAILBOX_ROLE=reviewer" in env_rev
    assert f"VIBE_HARNESS_MAILBOX_DIR={sdir / 'mailbox'}" in env_run
    # Each session is tagged with its round key, scoping submit_verdict's
    # per-round verdict file.
    assert "VIBE_HARNESS_MAILBOX_ROUND=run-1" in env_run
    assert "VIBE_HARNESS_MAILBOX_ROUND=review-1" in env_rev


@git_test
def test_mailbox_disabled_by_config(store, tmp_path, fake_vibe, projects):
    """VIBE_HARNESS_MAILBOX=0: no .vibe/config.toml, no mailbox dir, no mailbox
    env in the spawned sessions."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe, projects,
                                        mailbox=False)
    card = store.create("No mailbox", "Write a hello file.", project="my-app")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done")

    sdir = runner.sessions_dir / card_id
    assert not (sdir / "worktree" / ".vibe").exists()
    assert not (sdir / "mailbox").exists()
    assert (final["session"].get("mailbox") or None) is None
    env_run = (sdir / "run-1" / "export" / "env.txt").read_text()
    assert "VIBE_HARNESS_MAILBOX" not in env_run
    # Verdict still works via the log fallback.
    assert final["session"]["review"]["approved"] is True
    assert final["session"]["review"]["via"] == "log"


@git_test
def test_done_when_persistent_worker_diff_review_mailbox_e2e(store, tmp_path, projects,
                                                             fake_vibe_mcp):
    """The spec's done-when, end to end with the real mailbox MCP server:

    run-1 (fresh worker) -> review-1 (fresh reviewer, REJECTED via
    submit_verdict after post_message to the worker) -> run-2 (the SAME worker
    session resumed via --resume, which reads the reviewer's question and
    replies) -> review-2 (fresh, APPROVED via submit_verdict) -> done.

    Asserts: 1 worker session (resumed), 2 fresh reviewer processes, >= 2 MCP
    messages exchanged, verdict via submit_verdict (structured beats the
    misleading VERDICT: log line), diff-scoped review prompts with the test
    output, and the mailbox attached in the card's worktree.
    """
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe_mcp, projects,
                                        test_cmd="echo TESTS-PASS-123; exit 0")
    card = store.create("E2E loop card", "Write a hello file.", project="my-app")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done", timeout=30)

    session = final["session"]
    # 2 review rounds; the verdict came from the structured MCP call.
    assert session["review_rounds"] == 2
    assert session["review"]["approved"] is True
    assert session["review"]["via"] == "submit_verdict"
    # 1 worker session, resumed for the fix round.
    assert session["worker_session_id"] == "worker-session-1"

    sdir = runner.sessions_dir / card_id
    run1_args = (sdir / "run-1" / "export" / "args.txt").read_text()
    run2_args = (sdir / "run-2" / "export" / "args.txt").read_text()
    assert "--resume" not in run1_args
    assert "--resume worker-session-1" in run2_args
    assert (sdir / "run-2" / "worktree" / "fix.txt").exists() or \
           (sdir / "worktree" / "fix.txt").exists()
    # 2 fresh reviewer processes.
    for r in ("review-1", "review-2"):
        assert "--resume" not in (sdir / r / "export" / "args.txt").read_text()
        assert (sdir / r / "export" / "tools.txt").exists()
        tools = (sdir / r / "export" / "tools.txt").read_text().split()
        assert tools == ["post_message", "submit_verdict", "wait_for_message"]

    # Mailbox: attached in the worktree, messages exchanged both ways.
    wt = sdir / "worktree"
    cfg = (wt / ".vibe" / "config.toml").read_text()
    assert "harness-mailbox" in cfg and "mailbox_server.py" in cfg
    assert not gitops.is_dirty(wt)
    msgs = mailbox.list_messages(sdir / "mailbox")
    assert len(msgs) >= 2
    assert any(m["from"] == "reviewer" and m["to"] == "worker" for m in msgs)
    assert any(m["from"] == "worker" and m["to"] == "reviewer" for m in msgs)
    assert session["review"]["messages"] == len(msgs)
    # The structured verdicts on disk are scoped per review round and match
    # the review records; the latest mirror holds round 2's verdict.
    assert mailbox.read_verdict(sdir / "mailbox", round="review-1") == \
        (False, "tests fail in round 1")
    assert mailbox.read_verdict(sdir / "mailbox", round="review-2") == (True, None)
    assert mailbox.read_verdict(sdir / "mailbox") == (True, None)
    # review-1's structured verdict was REJECTED even though its log printed
    # "VERDICT: APPROVED" (misleading fallback) — proof the structured call won.
    assert "VERDICT: APPROVED" in (sdir / "review-1" / "session.log").read_text()

    # Diff-scoped review prompts + test output; round 2 diffs from round 1.
    p1 = (sdir / "review-1" / "prompt.md").read_text()
    p2 = (sdir / "review-2" / "prompt.md").read_text()
    for p in (p1, p2):
        assert "diff --git" in p and "hello.txt" in p
        assert "TESTS-PASS-123" in p
        assert "submit_verdict" in p and "post_message" in p
    assert session["last_reviewed_commit"][:12] in p2

    # The fixer round was told it is resuming.
    assert "RESUMING your previous session" in (sdir / "run-2" / "prompt.md").read_text()


@git_test
def test_review_round_without_submit_verdict_does_not_inherit(store, tmp_path, projects,
                                                               fake_vibe_mcp, monkeypatch):
    """The reviewer's rejection scenario, end to end: review-1 submits a
    structured REJECTED via submit_verdict; review-2 NEVER calls
    submit_verdict (its only verdict is a VERDICT: APPROVED log line).

    Round 2 must fall back to the log (via="log") and must NOT inherit round
    1's structured verdict as via="submit_verdict" — verdicts are scoped per
    review round (verdicts/review-<n>.json, cleared before the round spawns,
    reviewer role only)."""
    monkeypatch.setenv("FAKE_NO_VERDICT_FROM", "2")
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe_mcp, projects)
    card = store.create("No-inherit card", "Write a hello file.", project="my-app")
    card_id = card["id"]

    runner.run(card_id)
    final = _wait_for_status(store, card_id, "done", timeout=30)

    session = final["session"]
    assert session["review_rounds"] == 2
    assert session["review"]["approved"] is True
    assert session["review"]["round"] == 2
    # Round 2 had no structured verdict -> the log fallback decided, and the
    # verdict was NOT mis-attributed to submit_verdict.
    assert session["review"]["via"] == "log"

    sdir = runner.sessions_dir / card_id
    mdir = sdir / "mailbox"
    # Round 1's structured verdict exists and stays scoped to review-1;
    # review-2 has NO verdict file — there was nothing to inherit.
    assert mailbox.read_verdict(mdir, round="review-1") == (False, "tests fail in round 1")
    assert mailbox.read_verdict(mdir, round="review-2") is None
    # The latest mirror still holds round 1's verdict (round 2 never wrote one).
    assert mailbox.read_verdict(mdir) == (False, "tests fail in round 1")
    # Round 2's log verdict was used.
    assert "VERDICT: APPROVED" in (sdir / "review-2" / "session.log").read_text()
    # The review round was tagged with its round key.
    env_rev2 = (sdir / "review-2" / "export" / "env.txt").read_text()
    assert "VIBE_HARNESS_MAILBOX_ROUND=review-2" in env_rev2


def test_worker_prompts_instruct_tdd_by_default():
    """When the model takes a card and starts working on it (run-1 prompt and
    every fixer round), the prompt tells it to follow test-driven development."""
    for prompt in (prompt_template("spec body"),
                   fix_prompt_template("spec body", "reason")):
        assert "test-driven development" in prompt
        assert "RED" in prompt and "GREEN" in prompt and "REFACTOR" in prompt
        assert "fails" in prompt            # failing test first, confirmed red
        assert "FULL test suite" in prompt  # green before finishing
    # Reviewer/resolver prompts are not worker prompts — unchanged.
    assert "test-driven development" not in review_prompt_template("spec")
    assert "test-driven development" not in resolve_prompt_template(
        "title", "body", "branch", "main")


def test_worker_prompts_tdd_can_be_disabled():
    assert "test-driven development" not in prompt_template("spec", tdd=False)
    assert "test-driven development" not in fix_prompt_template("spec", "r", tdd=False)
    # Disabling TDD does not touch the MCP rule block.
    assert "harness_create_card" in prompt_template("spec", tdd=False)


def test_tdd_config_flag_from_env():
    assert load_config({}).tdd is True
    assert load_config({"VIBE_HARNESS_TDD": "1"}).tdd is True
    assert load_config({"VIBE_HARNESS_TDD": "0"}).tdd is False
    assert load_config({"VIBE_HARNESS_TDD": "false"}).tdd is False


def test_run_writes_tdd_rule_into_prompt(store, make_runner, fake_vibe):
    runner = make_runner(fake_vibe)
    card = store.create("TDD card", "Add a feature.")
    card_id = card["id"]
    runner.run(card_id)
    _wait_for_status(store, card_id, "done")
    prompt = (runner.sessions_dir / card_id / "run-1" / "prompt.md").read_text()
    assert "test-driven development" in prompt
    assert "Add a feature." in prompt


def test_run_with_tdd_disabled_omits_rule(store, make_runner, fake_vibe):
    runner = make_runner(fake_vibe, tdd=False)
    card = store.create("No-TDD card", "Add a feature.")
    card_id = card["id"]
    runner.run(card_id)
    _wait_for_status(store, card_id, "done")
    prompt = (runner.sessions_dir / card_id / "run-1" / "prompt.md").read_text()
    assert "test-driven development" not in prompt
    assert "Add a feature." in prompt


# ----------------------------------------------------------------------
# startup recovery: stuck resolvers, adopted live sessions, orphaned
# worktrees/branches, pending questions (server restart / kill -9)
# ----------------------------------------------------------------------

def _dead_pid() -> int:
    """A pid that is guaranteed dead (an already-reaped child)."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _sleep_child(seconds: float = 30):
    """A live child process standing in for a vibe session. A reaper thread
    waits on it, so os.kill(pid, 0) starts failing as soon as it exits (the
    harness's own children are reaped by init in production)."""
    proc = subprocess.Popen(["sleep", str(seconds)])
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _merge_setup(store, tmp_path, projects, vibe_bin, *, conflict: bool):
    """Project repo + a card branch whose implementation is committed in its
    linked worktree; the accept-time merge into the base branch either
    CONCLUDED in the main worktree (MERGE_HEAD gone, tree clean — the resolver
    died before finalize ran) or is left CONFLICTED (MERGE_HEAD present).
    Returns (runner, card_id, proj, git_info, worktree)."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    gitops.ensure_repo(proj)
    (proj / "hello.txt").write_text("base version\n")
    _git(["add", "-A"], proj)
    _git(["commit", "-q", "-m", "base hello.txt"], proj)

    runner = _make_runner_with_projects(store, tmp_path, vibe_bin, projects)
    card = store.create("Recovery card", "Write a hello file.", project="my-app")
    card_id = card["id"]
    branch = f"harness/{card_id}"
    wt = runner.sessions_dir / card_id / "worktree"
    git_info = gitops.prepare_card_branch(proj, branch, wt)
    # The card's implementation, committed on the card branch in the worktree.
    (wt / "hello.txt").write_text("card version\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-q", "-m", "card work"], wt)
    if conflict:
        # A competing base edit makes the accept-time merge conflict; the
        # conflicted merge is LEFT IN PLACE (MERGE_HEAD set).
        (proj / "hello.txt").write_text("user edit\n")
        _git(["add", "hello.txt"], proj)
        _git(["commit", "-q", "-m", "user edit"], proj)
        proc = subprocess.run(["git", "merge", "--no-edit", branch], cwd=proj,
                              capture_output=True, text=True)
        assert proc.returncode != 0
        assert gitops.merge_in_progress(proj)
    else:
        proc = subprocess.run(["git", "merge", "--ff-only", "--quiet", branch],
                              cwd=proj, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert not gitops.merge_in_progress(proj)
        assert not gitops.is_dirty(proj)
    return runner, card_id, proj, git_info, wt


def _mark_done_resolving(store, card_id, runner, git_info, proj, wt, resolver_pid,
                        export_json=None):
    """Put a card into the stuck `done` + git.resolving state a server kill
    leaves behind (resolver record + an approved review on file)."""
    rdir = runner.sessions_dir / card_id / "resolve-1"
    (rdir / "export").mkdir(parents=True, exist_ok=True)
    if export_json is not None:
        (rdir / "export" / "export.json").write_text(export_json)
    store.set_status(card_id, "running", session_patch={
        "pid": None, "workdir": str(wt.resolve()), "log": str(rdir / "session.log"),
        "export_dir": str(rdir / "export"), "git": git_info,
        "review": {"approved": True, "reason": None, "round": 1, "source": "review",
                   "tokens": 1, "log": None, "export_dir": None},
    })
    store.set_status(card_id, "done", session_patch={
        "git": {**git_info, "merged": False, "resolving": True,
                "merge_conflict": True, "merge_error": "conflicted"},
        "resolver": {"pid": resolver_pid, "started_at": "2026-01-01T00:00:00+00:00",
                     "finished_at": None, "exit_code": None, "round": 1,
                     "workdir": str(proj.resolve()), "log": str(rdir / "session.log"),
                     "export_dir": str(rdir / "export")},
    })


@git_test
def test_recover_stale_finishes_accept_when_resolver_died_after_merge(
        store, tmp_path, fake_vibe, projects):
    """Stuck `done`+resolving whose merge already landed (MERGE_HEAD gone, tree
    clean) and whose resolver pid is dead: startup finishes the accept
    automatically — linked worktree removed, card branch deleted, merged +
    accepted — instead of leaving the card stuck."""
    runner, card_id, proj, git_info, wt = _merge_setup(
        store, tmp_path, projects, fake_vibe, conflict=False)
    _mark_done_resolving(store, card_id, runner, git_info, proj, wt, _dead_pid())

    runner.recover_stale()

    final = store.get(card_id)
    assert final["status"] == "accepted"
    assert final["accepted"] is True
    g = final["session"]["git"]
    assert g["merged"] is True
    assert g["resolving"] is False
    assert g["merge_conflict"] is False
    assert not gitops.branch_exists(proj, g["branch"])
    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(proj)
    resolver = final["session"]["resolver"]
    assert resolver["finished_at"]
    assert resolver["exit_code"] is None


@git_test
def test_recover_stale_leaves_open_conflict_for_resolver(
        store, tmp_path, fake_vibe, projects):
    """MERGE_HEAD still exists (conflict open, resolver pid dead): the card is
    left for a resolver — still done+resolving, worktree and branch kept."""
    runner, card_id, proj, git_info, wt = _merge_setup(
        store, tmp_path, projects, fake_vibe, conflict=True)
    _mark_done_resolving(store, card_id, runner, git_info, proj, wt, _dead_pid())

    runner.recover_stale()

    final = store.get(card_id)
    assert final["status"] == "done"
    g = final["session"]["git"]
    assert g["resolving"] is True
    assert gitops.merge_in_progress(proj)
    assert gitops.branch_exists(proj, g["branch"])
    assert wt.resolve() in gitops.registered_worktrees(proj)


@git_test
def test_recover_stale_adopts_live_resolver_and_finishes_on_exit(
        store, tmp_path, fake_vibe, projects):
    """The resolver process is still alive after the restart: it is re-adopted
    (kind=resolve) and _finalize_resolver finishes the accept when it exits."""
    import os
    import signal
    runner, card_id, proj, git_info, wt = _merge_setup(
        store, tmp_path, projects, fake_vibe, conflict=False)
    child = _sleep_child(30)
    _mark_done_resolving(store, card_id, runner, git_info, proj, wt, child.pid,
                        export_json='{"exit_code": 0, "usage": {"total_tokens": 3}}')

    runner.recover_stale()
    assert store.get(card_id)["status"] == "done"      # adopted, not concluded yet
    with runner._lock:
        entry = runner._registry.get(card_id)
    assert entry is not None and entry.get("adopted") is True
    assert entry["kind"] == "resolve"

    os.kill(child.pid, signal.SIGKILL)                 # the resolver process exits
    final = _wait_for_status(store, card_id, "accepted", timeout=10)
    g = final["session"]["git"]
    assert g["merged"] is True and g["resolving"] is False
    assert not gitops.branch_exists(proj, g["branch"])
    assert not wt.exists()
    assert final["session"]["resolver"]["exit_code"] == 0


def test_recover_stale_stop_works_on_adopted_session_by_pid(
        store, make_runner, fake_vibe, tmp_path):
    """A live card re-adopted after a restart can be stopped by pid: stop()
    must not raise NotRunningError and must SIGTERM the adopted process."""
    runner = make_runner(fake_vibe)
    card = store.create("Adopted stoppable", "spec")
    card_id = card["id"]
    rdir = tmp_path / "sessions" / card_id / "run-1"
    (rdir / "export").mkdir(parents=True)
    (rdir / "export" / "export.json").write_text(
        '{"usage": {"total_tokens": 5}, "exit_code": 0}')
    child = _sleep_child(30)
    store.set_status(card_id, "running", session_patch={
        "pid": child.pid, "export_dir": str(rdir / "export"),
        "log": str(rdir / "session.log"), "workdir": str(rdir)})

    runner.recover_stale()
    assert store.get(card_id)["status"] == "running"
    assert runner.is_running(card_id)

    runner.stop(card_id)                               # must not raise NotRunningError
    final = _wait_for_status(store, card_id, "stopped")
    assert not runner.is_running(card_id)
    assert final["session"]["exit_code"] == 0
    with runner._lock:
        assert card_id not in runner._registry


def test_recover_stale_cancels_pending_questions_for_failed_and_adopted(
        store, make_runner, fake_vibe, tmp_path):
    """Pending questions whose long-poll waiter died with the server are
    cancelled at startup — for cards failed (dead pid) AND for re-adopted
    live cards — so no card shows a question nobody will answer."""
    runner = make_runner(fake_vibe)
    dead = store.create("Dead question", "spec")
    live = store.create("Live question", "spec")
    store.set_status(dead["id"], "running", session_patch={"pid": _dead_pid()})
    child = _sleep_child(30)
    rdir = tmp_path / "sessions" / live["id"] / "run-1"
    (rdir / "export").mkdir(parents=True)
    (rdir / "export" / "export.json").write_text('{"exit_code": 0}')
    store.set_status(live["id"], "running", session_patch={
        "pid": child.pid, "export_dir": str(rdir / "export"),
        "log": str(rdir / "session.log")})
    q_dead = runner.questions.ask(dead["id"], "dead session asks?")
    q_live = runner.questions.ask(live["id"], "live session asks?")

    runner.recover_stale()

    assert store.get(dead["id"])["status"] == "failed"
    assert runner.questions.get(dead["id"], q_dead["id"])["status"] == "cancelled"
    assert store.get(live["id"])["status"] == "running"   # adopted, not failed
    assert runner.questions.get(live["id"], q_live["id"])["status"] == "cancelled"
    runner.stop(live["id"])                               # cleanup
    _wait_for_status(store, live["id"], "stopped")


@git_test
def test_recover_stale_prunes_orphaned_worktree_keeps_unmerged_branch(
        store, tmp_path, fake_vibe, projects):
    """After a restart, a non-live card's registered worktree is pruned
    (worktree remove --force + prune) but its branch is deleted only when
    already merged — unmerged work is never force-deleted. A later accept
    still works (the gate re-creates the worktree from the card branch)."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    card = store.create("Done card", "Write a hello file.", project="my-app")
    card_id = card["id"]
    runner.run(card_id)
    done = _wait_for_status(store, card_id, "done")
    git_info = done["session"]["git"]
    wt = Path(git_info["worktree"])
    assert wt.resolve() in gitops.registered_worktrees(proj)
    assert gitops.branch_exists(proj, git_info["branch"])

    # server restart: a fresh runner over the same store/sessions dir
    runner2 = _make_runner_with_projects(store, tmp_path, fake_vibe, projects,
                                         test_cmd="exit 0")
    runner2.recover_stale()

    assert store.get(card_id)["status"] == "done"
    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(proj)
    assert gitops.branch_exists(proj, git_info["branch"])   # unmerged -> kept

    # Accepting afterwards still works: the green gate re-creates the linked
    # worktree from the card branch, the merge then cleans up.
    accepted = runner2.accept_card(card_id)
    assert accepted["status"] == "accepted"
    assert accepted["session"]["git"]["merged"] is True
    assert not gitops.branch_exists(proj, git_info["branch"])
    assert not wt.exists()


@git_test
def test_recover_stale_prunes_worktree_and_merged_branch_after_killed_accept(
        store, tmp_path, fake_vibe, projects):
    """The server died between the merge landing and remove_card_worktree /
    branch delete: the branch is already merged, so startup prunes BOTH the
    worktree and the branch (safe `git branch -d`); a later accept finishes
    without re-merging."""
    runner, card_id, proj, git_info, wt = _merge_setup(
        store, tmp_path, projects, fake_vibe, conflict=False)
    store.set_status(card_id, "running", session_patch={
        "pid": None, "workdir": str(wt.resolve()), "git": git_info,
        "review": {"approved": True, "reason": None, "round": 1, "source": "review",
                   "tokens": 1, "log": None, "export_dir": None}})
    store.set_status(card_id, "done")

    runner.recover_stale()

    n = store.get(card_id)
    assert n["status"] == "done"
    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(proj)
    assert not gitops.branch_exists(proj, git_info["branch"])  # merged -> deleted

    accepted = runner.accept_card(card_id)     # branch already gone: no re-merge
    assert accepted["status"] == "accepted"
    assert accepted["session"]["git"]["merged"] is True


@git_test
def test_restart_after_kill_mid_run_leaves_no_stuck_cards_or_orphan_worktrees(
        store, tmp_path, fake_vibe, projects):
    """Done-when simulation: a server kill -9 mid-run (vibe child alive, card
    running) followed by a restart re-adopts the session (card stays running,
    not failed/stuck), cancels its dead-waiter question, stop() works by pid,
    and once the session is over a further restart prunes the orphaned
    worktree while keeping the unmerged branch."""
    proj = tmp_path / "my-app"
    proj.mkdir()
    projects.create("my-app", str(proj))
    gitops.ensure_repo(proj)
    (proj / "base.txt").write_text("base\n")
    _git(["add", "-A"], proj)
    _git(["commit", "-q", "-m", "base"], proj)
    runner = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    card = store.create("Mid-run card", "spec", project="my-app")
    card_id = card["id"]
    branch = f"harness/{card_id}"
    wt = runner.sessions_dir / card_id / "worktree"
    git_info = gitops.prepare_card_branch(proj, branch, wt)
    (wt / "hello.txt").write_text("wip\n")
    _git(["add", "-A"], wt)
    _git(["commit", "-q", "-m", "wip on the card branch"], wt)
    rdir = runner.sessions_dir / card_id / "run-1"
    (rdir / "export").mkdir(parents=True)
    (rdir / "export" / "export.json").write_text(
        '{"usage": {"total_tokens": 9}, "exit_code": 0}')
    child = _sleep_child(30)                  # the vibe process, alive across the "kill"
    store.set_status(card_id, "running", session_patch={
        "pid": child.pid, "export_dir": str(rdir / "export"),
        "log": str(rdir / "session.log"), "workdir": str(wt.resolve()),
        "git": git_info})
    q = runner.questions.ask(card_id, "still there?")

    # --- kill -9 the server; restart with a fresh runner over the same state ---
    restarted = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    restarted.recover_stale()

    n = store.get(card_id)
    assert n["status"] == "running"           # re-adopted — not stuck, not failed
    assert restarted.is_running(card_id)
    assert wt.resolve() in gitops.registered_worktrees(proj)  # live -> kept
    assert restarted.questions.get(card_id, q["id"])["status"] == "cancelled"

    restarted.stop(card_id)                   # stop by pid on the adopted session
    _wait_for_status(store, card_id, "stopped")
    assert not restarted.is_running(card_id)

    # --- another restart: the stopped card's worktree is now orphaned ---
    restarted2 = _make_runner_with_projects(store, tmp_path, fake_vibe, projects)
    restarted2.recover_stale()
    assert store.get(card_id)["status"] == "stopped"
    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(proj)
    assert gitops.branch_exists(proj, branch)  # unmerged work never force-deleted


# ----------------------------------------------------------------------
# reviewer follow-up: recovery must never crash server startup, and the
# resolver-adopt branch must cancel pending questions too (spec item 4)
# ----------------------------------------------------------------------

def _broken_resolving_card(store, runner, tmp_path, title="Broken repo"):
    """A `done`+resolving card whose repo folder was deleted while the server
    was down (git calls on it raise FileNotFoundError/GitError)."""
    card = store.create(title, "spec")
    card_id = card["id"]
    gone = tmp_path / f"deleted-repo-{card_id}"
    git_info = {"repo": str(gone), "branch": f"harness/{card_id}",
                "base_branch": "main",
                "worktree": str(tmp_path / f"wt-{card_id}"),
                "merged": False, "resolving": True, "merge_conflict": True,
                "merge_error": "conflicted"}
    store.set_status(card_id, "running", session_patch={
        "pid": None, "workdir": str(gone), "git": git_info,
        "review": {"approved": True, "reason": None, "round": 1, "source": "review",
                   "tokens": 1, "log": None, "export_dir": None}})
    store.set_status(card_id, "done", session_patch={
        "git": git_info,
        "resolver": {"pid": _dead_pid(), "started_at": "2026-01-01T00:00:00+00:00",
                     "finished_at": None, "exit_code": None, "round": 1,
                     "workdir": str(gone), "log": "", "export_dir": ""}})
    return card_id


def test_recover_stale_broken_card_does_not_crash_startup(store, make_runner, fake_vibe, tmp_path):
    """One broken card (e.g. its repo folder was deleted while the server was
    down) must not crash startup recovery: recover_stale() survives it and
    still reconciles the other cards."""
    runner = make_runner(fake_vibe)
    healthy = store.create("Healthy stale", "spec")
    store.set_status(healthy["id"], "running", session_patch={"pid": _dead_pid()})
    # Created (and last updated) after `healthy`, so it is processed FIRST —
    # without per-card isolation its exception would abort the whole loop.
    broken_id = _broken_resolving_card(store, runner, tmp_path)

    runner.recover_stale()                     # must not raise

    assert store.get(healthy["id"])["status"] == "failed"   # still reconciled
    assert store.get(broken_id)["status"] == "done"          # left alone, no crash


@git_test
def test_recover_stale_adopted_resolver_cancels_pending_questions(
        store, tmp_path, fake_vibe, projects):
    """Spec item 4 for the resolver-adopt branch: a still-pending question on a
    card whose live resolver is re-adopted is cancelled at startup (its
    long-poll waiter died with the old server)."""
    runner, card_id, proj, git_info, wt = _merge_setup(
        store, tmp_path, projects, fake_vibe, conflict=True)
    child = _sleep_child(30)
    _mark_done_resolving(store, card_id, runner, git_info, proj, wt, child.pid)
    q = runner.questions.ask(card_id, "question from the dead server?")

    runner.recover_stale()

    with runner._lock:
        entry = runner._registry.get(card_id)
    assert entry is not None and entry.get("adopted") is True
    assert entry["kind"] == "resolve"
    assert runner.questions.get(card_id, q["id"])["status"] == "cancelled"
    runner.stop(card_id)                       # cleanup: kill the adopted resolver
    _wait_for_flag(store, card_id,
                   lambda n: (n["session"]["git"] or {}).get("resolving"), False)
