"""Tests for the harness MCP server (app/mcp_server.py) and its runner wiring.

The MCP server is exercised two ways:
- in-process: handle_message()/call_tool() against a real dashboard served
  over HTTP by uvicorn in a background thread (fake vibe binary, temp dirs);
- end-to-end: the server is spawned as a subprocess and spoken to over
  stdio with newline-delimited JSON-RPC, like vibe does.

Also covered: the runner injects VIBE_MCP_SERVERS into run sessions, keeps the
harness MCP tools under a tool allowlist, and the prompts tell models to judge
first whether to split the spec into cards. Project management is deliberately
NOT exposed to models.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import uvicorn

from app.archives import ArchiveStore
from app.config import Config
from app.main import create_app
from app.cards import CardStore
from app.projects import ProjectStore
from app.runner import MCP_SERVER_PATH, SessionRunner, fix_prompt_template, prompt_template
from app import mcp_server

FAKE_VIBE = """#!/bin/sh
# Fake vibe: write artifacts into --workdir / --output-dir, then exit 0.
# Review mode (--agent ask) prints a verdict.
workdir=""
outdir=""
agent=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2;;
    --output-dir) outdir="$2"; shift 2;;
    --agent) agent="$2"; shift 2;;
    *) shift;;
  esac
done
echo "fake vibe starting"
echo "fake vibe done"
mkdir -p "$workdir"
echo 'print("hello")' > "$workdir/hello.py"
mkdir -p "$outdir"
cat > "$outdir/export.json" <<'EOF'
{"cost_usd": 0.01, "usage": {"total_tokens": 42}, "config": {"active_model": "fake-model"}, "stop_reason": "end_turn", "outcome": "success"}
EOF
if [ "$agent" = "ask" ]; then
  echo "VERDICT: APPROVED"
fi
exit 0
"""

FAKE_VIBE_SLOW = """#!/bin/sh
echo "fake vibe slow start"
trap 'echo "fake vibe terminated"; exit 143' TERM
sleep 30
exit 0
"""


def make_app(tmp_path: Path, script: str = FAKE_VIBE, **config_kwargs) -> SimpleNamespace:
    store = CardStore(tmp_path / "cards")
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(script)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30, **config_kwargs)
    projects = ProjectStore(tmp_path / "projects.json")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects)
    archives = ArchiveStore(tmp_path / "archives", store, projects)
    app = create_app(config=config, store=store, runner=runner, projects=projects,
                     archives=archives)
    return SimpleNamespace(app=app, store=store, runner=runner, projects=projects,
                           archives=archives, config=config, tmp_path=tmp_path)


def serve(app) -> tuple[int, threading.Thread, uvicorn.Server]:
    """Run the FastAPI app on a real local HTTP port in a daemon thread."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                            log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn test server did not start"
    return port, thread, server


@pytest.fixture()
def harness(tmp_path, monkeypatch):
    """A running dashboard (HTTP) + the MCP module pointed at it."""
    env = make_app(tmp_path)
    port, thread, server = serve(env.app)
    monkeypatch.setenv("VIBE_HARNESS_URL", f"http://127.0.0.1:{port}")
    monkeypatch.delenv("VIBE_HARNESS_NOTE_ID", raising=False)
    yield env
    server.should_exit = True
    thread.join(timeout=5)


def call(harness, tool: str, **args) -> dict:
    """Call an MCP tool in-process; returns the parsed text payload + isError."""
    result = mcp_server.call_tool(tool, args)
    text = result["content"][0]["text"]
    try:
        payload = json.loads(text)
    except ValueError:
        payload = text
    return {"payload": payload, "isError": result.get("isError", False)}


def wait_status(harness, card_id, statuses, timeout=10.0):
    import urllib.request
    url = mcp_server.harness_url()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with urllib.request.urlopen(f"{url}/api/cards/{card_id}", timeout=5) as r:
            card = json.loads(r.read())
        if card["status"] in statuses:
            return card
        time.sleep(0.05)
    raise AssertionError(f"card {card_id} did not reach {statuses}")


# ---------------------------------------------------------------- protocol

def test_initialize_ping_and_notifications():
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}}})
    assert resp["id"] == 1
    assert resp["result"]["protocolVersion"] == "2025-06-18"
    assert resp["result"]["serverInfo"]["name"] == "vibe-harness"
    assert "tools" in resp["result"]["capabilities"]
    assert mcp_server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "ping"})["result"] == {}
    # notifications get no response
    assert mcp_server.handle_message(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    # unknown method -> -32601
    resp = mcp_server.handle_message({"jsonrpc": "2.0", "id": 3, "method": "bogus"})
    assert resp["error"]["code"] == -32601


def test_tools_list_surface(harness):
    resp = mcp_server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {t["name"]: t for t in resp["result"]["tools"]}
    expected = {"current_card", "list_cards", "get_card", "create_card", "update_card",
                "delete_card", "run_card", "stop_card", "accept_card", "card_log",
                "list_templates", "stats", "list_projects", "ask_user",
                "add_dependency", "remove_dependency", "list_dependencies",
                "create_archive", "list_archives", "get_archive",
                "archive_card", "archive_cards", "archive_stack", "restore_card",
                # Card stacks are server state so a model can build piles and
                # move cards between them (card: stacks with headers + MCP).
                "list_stacks", "create_stack", "update_stack", "delete_stack",
                "stack_cards", "unstack_card",
                # Which models a card can be put on (create_card takes `model`).
                "list_models"}
    assert set(tools) == expected
    # No way for models to manage or swap projects (archiving a whole project
    # deck stays a user-side action — deliberately not an MCP tool).
    for name in tools:
        assert "project" not in name or name == "list_projects", name
    assert "create_project" not in tools and "delete_project" not in tools
    assert "archive_project" not in tools
    # create_card/update_card must not even accept project/workdir arguments.
    for name in ("create_card", "update_card"):
        props = tools[name]["inputSchema"]["properties"]
        assert "project" not in props and "workdir" not in props
    # unknown tool -> JSON-RPC error
    resp = mcp_server.handle_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "create_project", "arguments": {}}})
    assert resp["error"]["code"] == -32602


# ---------------------------------------------------------------- tools over HTTP

def test_create_card_stays_in_current_project(harness, monkeypatch):
    harness.projects.create("Proj", str(harness.tmp_path / "proj"))
    import urllib.request
    url = mcp_server.harness_url()
    body = json.dumps({"title": "Parent", "body": "spec", "project": "Proj"}).encode()
    req = urllib.request.Request(f"{url}/api/cards", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        parent = json.loads(r.read())
    monkeypatch.setenv("VIBE_HARNESS_NOTE_ID", parent["id"])

    # current_card grounds the model
    cur = call(harness, "current_card")
    assert cur["isError"] is False
    assert cur["payload"]["card_id"] == parent["id"]
    assert cur["payload"]["card"]["project"] == "Proj"

    # create_card inherits the parent's project
    res = call(harness, "create_card", title="Sub task", body="do the sub thing")
    assert res["isError"] is False, res
    assert res["payload"]["status"] == "draft"
    assert res["payload"]["project"] == "Proj"
    # ...and becomes a CHILD of the card the model is working on, so the board
    # shows the split tree.
    assert res["payload"]["parent_id"] == parent["id"]
    assert [n["id"] for n in harness.store.children_of(parent["id"])] == [res["payload"]["id"]]

    # ...and explicitly refuses project/workdir arguments (no project swapping)
    res = call(harness, "create_card", title="x", body="y", project="Other")
    assert res["isError"] is True
    assert "project" in res["payload"]
    res = call(harness, "update_card", card_id=parent["id"], workdir="/tmp/evil")
    assert res["isError"] is True
    assert "workdir" in res["payload"]


def test_create_card_without_context_is_sandbox_card(harness, monkeypatch):
    monkeypatch.delenv("VIBE_HARNESS_NOTE_ID", raising=False)
    res = call(harness, "create_card", title="Lonely", body="no project")
    assert res["isError"] is False
    assert res["payload"]["project"] is None


def test_card_lifecycle_via_mcp(harness, monkeypatch):
    res = call(harness, "create_card", title="Lifecycle", body="make hello.py")
    card = res["payload"]
    assert card["status"] == "draft"

    # list + filters
    assert any(n["id"] == card["id"] for n in call(harness, "list_cards")["payload"])
    assert call(harness, "list_cards", status="draft")["payload"]
    assert call(harness, "list_cards", status="accepted")["payload"] == []
    got = call(harness, "get_card", card_id=card["id"])
    assert got["payload"]["title"] == "Lifecycle"
    # unknown card -> isError with the 404 detail
    err = call(harness, "get_card", card_id="nope99")
    assert err["isError"] is True and "404" in err["payload"]

    # run it through the MCP, wait for the review loop to finish
    res = call(harness, "run_card", card_id=card["id"])
    assert res["isError"] is False
    assert res["payload"]["status"] == "running"
    done = wait_status(harness, card["id"], ("done", "failed"))
    assert done["status"] == "done"
    assert done["session"]["review"]["approved"] is True

    # log tail via MCP
    log = call(harness, "card_log", card_id=card["id"], tail=50)
    assert "fake vibe" in log["payload"]["lines"]

    # accept via MCP (approved review on record)
    res = call(harness, "accept_card", card_id=card["id"])
    assert res["isError"] is False, res
    assert res["payload"]["status"] == "accepted"

    # update + delete via MCP
    res = call(harness, "update_card", card_id=card["id"], title="Lifecycle 2")
    assert res["isError"] is False and res["payload"]["title"] == "Lifecycle 2"
    res = call(harness, "delete_card", card_id=card["id"])
    assert res["isError"] is False
    assert call(harness, "get_card", card_id=card["id"])["isError"] is True


def test_run_then_stop_via_mcp(harness):
    harness.config.vibe_bin = str(harness.tmp_path / "slow.sh")
    (harness.tmp_path / "slow.sh").write_text(FAKE_VIBE_SLOW)
    (harness.tmp_path / "slow.sh").chmod(0o755)
    res = call(harness, "create_card", title="Slow", body="sleep")
    card = res["payload"]
    call(harness, "run_card", card_id=card["id"])
    stopped = wait_status(harness, card["id"], ("running",))
    assert stopped["status"] == "running"
    res = call(harness, "stop_card", card_id=card["id"])
    assert res["isError"] is False
    final = wait_status(harness, card["id"], ("stopped", "failed"))
    assert final["status"] == "stopped"


def test_dependency_tools_via_mcp(harness):
    """add_dependency / remove_dependency / list_dependencies against the board,
    including the auto-run when a dependency completes."""
    dep = call(harness, "create_card", title="Dep", body="write dep.py")["payload"]
    follow = call(harness, "create_card", title="Follow", body="write follow.py")["payload"]

    # list_dependencies over the whole board
    listing = call(harness, "list_dependencies")["payload"]
    entry = next(n for n in listing if n["id"] == follow["id"])
    assert entry["depends_on"] == []

    # link follow -> dep (dep is still draft, so follow waits)
    res = call(harness, "add_dependency", card_id=follow["id"], depends_on=dep["id"])
    assert res["isError"] is False, res
    assert res["payload"]["depends_on"] == [dep["id"]]
    assert res["payload"]["status"] == "draft"
    assert res["payload"]["session"]["waiting_for"] == [dep["id"]]

    # per-card view: depends_on / blocks / unmet
    view = call(harness, "list_dependencies", card_id=follow["id"])["payload"]
    assert view == {"card_id": follow["id"], "depends_on": [dep["id"]],
                    "blocks": [], "unmet": [dep["id"]]}
    assert call(harness, "list_dependencies", card_id=dep["id"])["payload"]["blocks"] == [follow["id"]]

    # cycle and self-link are tool errors carrying the 409 detail
    assert call(harness, "add_dependency", card_id=dep["id"], depends_on=follow["id"])["isError"] is True
    err = call(harness, "add_dependency", card_id=follow["id"], depends_on=follow["id"])
    assert err["isError"] is True and "itself" in err["payload"]
    assert call(harness, "add_dependency", card_id=follow["id"], depends_on="nope99")["isError"] is True

    # run the dependency; when it completes the dependent starts by itself
    call(harness, "run_card", card_id=dep["id"])
    wait_status(harness, dep["id"], ("done", "failed"))
    final = wait_status(harness, follow["id"], ("done", "failed"))
    assert final["status"] == "done"
    assert final["session"]["waiting_for"] is None

    # unlink (idempotent) — the card is done now, so no re-run happens
    res = call(harness, "remove_dependency", card_id=follow["id"], depends_on=dep["id"])
    assert res["isError"] is False
    assert res["payload"]["depends_on"] == []
    assert call(harness, "remove_dependency", card_id=follow["id"], depends_on=dep["id"])["isError"] is False


def test_misc_read_tools(harness):
    harness.projects.create("P1", str(harness.tmp_path / "p1"))
    call(harness, "create_card", title="T", body="b")
    projs = call(harness, "list_projects")
    assert projs["isError"] is False
    assert projs["payload"][0]["name"] == "P1"
    stats = call(harness, "stats")
    assert stats["payload"]["counts"]["draft"] == 1
    templates = call(harness, "list_templates")
    assert templates["isError"] is False
    assert any(t["name"] == "bug" for t in templates["payload"])


# ---------------------------------------------------------------- ask_user

def _http_json(url, method="GET", payload=None):
    import urllib.request
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def test_ask_user_answered_on_the_card(harness, monkeypatch):
    card = call(harness, "create_card", title="Asker", body="spec")["payload"]
    monkeypatch.setenv("VIBE_HARNESS_NOTE_ID", card["id"])
    url = mcp_server.harness_url()

    # The user watches the card and answers 0.5s after the question lands.
    def answer_later():
        for _ in range(100):
            qs = _http_json(f"{url}/api/cards/{card['id']}/questions")
            if qs:
                _http_json(f"{url}/api/cards/{card['id']}/questions/{qs[0]['id']}/answer",
                           "POST", {"answer": "use sqlite"})
                return
            time.sleep(0.1)
    th = threading.Thread(target=answer_later, daemon=True)
    th.start()

    # The tool call blocks until the answer arrives and returns it.
    res = call(harness, "ask_user",
               question="Which DB should I use?", options=["sqlite", "postgres"],
               timeout_seconds=15)
    assert res["isError"] is False, res
    assert res["payload"]["status"] == "answered"
    assert res["payload"]["answer"] == "use sqlite"
    assert res["payload"]["question"] == "Which DB should I use?"
    th.join(timeout=5)

    # The Q&A is part of the card payload (card history + get_card via MCP).
    got = call(harness, "get_card", card_id=card["id"])
    assert got["payload"]["questions"][0]["status"] == "answered"
    assert got["payload"]["questions"][0]["answer"] == "use sqlite"


def test_ask_user_timeout_and_cancel(harness, monkeypatch):
    card = call(harness, "create_card", title="Asker2", body="spec")["payload"]
    monkeypatch.setenv("VIBE_HARNESS_NOTE_ID", card["id"])
    url = mcp_server.harness_url()

    # Nobody answers -> a timeout result (not a tool error).
    res = call(harness, "ask_user", question="Anyone there?", timeout_seconds=1)
    assert res["isError"] is False
    assert res["payload"]["status"] == "timeout"
    assert "hint" in res["payload"]

    # The user dismisses the next question on the card -> cancelled result.
    def cancel_later():
        for _ in range(100):
            qs = _http_json(f"{url}/api/cards/{card['id']}/questions")
            pending = [q for q in qs if q["status"] == "pending"]
            if pending:
                _http_json(f"{url}/api/cards/{card['id']}/questions/{pending[0]['id']}/cancel",
                           "POST")
                return
            time.sleep(0.1)
    th = threading.Thread(target=cancel_later, daemon=True)
    th.start()
    res = call(harness, "ask_user", question="Dismiss me?", timeout_seconds=15)
    assert res["isError"] is False, res
    assert res["payload"]["status"] == "cancelled"
    assert "hint" in res["payload"]
    th.join(timeout=5)


def test_ask_user_argument_and_context_validation(harness, monkeypatch):
    # No card context -> tool error, no question is created.
    monkeypatch.delenv("VIBE_HARNESS_NOTE_ID", raising=False)
    res = call(harness, "ask_user", question="hi?")
    assert res["isError"] is True and "card session" in res["payload"]

    card = call(harness, "create_card", title="X", body="b")["payload"]
    monkeypatch.setenv("VIBE_HARNESS_NOTE_ID", card["id"])
    assert call(harness, "ask_user", question="   ")["isError"] is True
    # missing required arg -> schema-level HarnessError (handle_message turns
    # it into an isError result; call_tool raises directly)
    with pytest.raises(mcp_server.HarnessError):
        mcp_server.call_tool("ask_user", {})
    bad_options = call(harness, "ask_user", question="q",
                       options=["a", "b", "c", "d", "e", "f", "g", "h", "i"])
    assert bad_options["isError"] is True
    assert call(harness, "ask_user", question="q", timeout_seconds="soon")["isError"] is True
    # nothing was registered by the failed calls
    assert call(harness, "get_card", card_id=card["id"])["payload"]["questions"] == []


def test_dashboard_unreachable_is_a_tool_error(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_HARNESS_URL", "http://127.0.0.1:1")  # nothing there
    monkeypatch.delenv("VIBE_HARNESS_NOTE_ID", raising=False)
    res = mcp_server.call_tool("list_cards", {})
    assert res["isError"] is True
    assert "not reachable" in res["content"][0]["text"]


# ---------------------------------------------------------------- stdio subprocess E2E

def test_stdio_subprocess_jsonrpc(tmp_path, monkeypatch):
    env = make_app(tmp_path)
    port, thread, server = serve(env.app)
    proc_env = dict(os.environ)
    proc_env["VIBE_HARNESS_URL"] = f"http://127.0.0.1:{port}"
    proc_env.pop("VIBE_HARNESS_NOTE_ID", None)
    proc = subprocess.Popen(
        [sys.executable, str(MCP_SERVER_PATH)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=proc_env, cwd=str(tmp_path))
    try:
        def send(msg):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        def recv():
            line = proc.stdout.readline()
            assert line, "MCP server closed stdout unexpectedly"
            return json.loads(line)

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18"}})
        resp = recv()
        assert resp["result"]["serverInfo"]["name"] == "vibe-harness"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        resp = recv()
        names = {t["name"] for t in resp["result"]["tools"]}
        assert "create_card" in names and "run_card" in names
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "create_card",
                         "arguments": {"title": "Via stdio", "body": "hello"}}})
        resp = recv()
        assert resp["result"]["isError"] is False
        card = json.loads(resp["result"]["content"][0]["text"])
        assert card["status"] == "draft"
        send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
              "params": {"name": "get_card", "arguments": {"card_id": card["id"]}}})
        resp = recv()
        assert json.loads(resp["result"]["content"][0]["text"])["id"] == card["id"]
        # missing required arg -> isError, not a crash
        send({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
              "params": {"name": "create_card", "arguments": {"title": "no body"}}})
        resp = recv()
        assert resp["result"]["isError"] is True or "error" in resp
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.should_exit = True
        thread.join(timeout=5)


# ---------------------------------------------------------------- runner wiring

def test_session_env_injects_mcp_server(tmp_path, monkeypatch):
    env = make_app(tmp_path)
    monkeypatch.setenv("VIBE_MCP_SERVERS",
                       json.dumps([{"name": "other", "transport": "stdio",
                                    "command": "/bin/echo"},
                                   {"name": "harness", "transport": "stdio",
                                    "command": "/stale"}]))
    session_env = env.runner._session_env("abc123")
    servers = json.loads(session_env["VIBE_MCP_SERVERS"])
    names = [s["name"] for s in servers]
    assert names == ["other", "harness"]  # stale harness entry replaced
    ours = servers[-1]
    assert ours["transport"] == "stdio"
    assert ours["command"] == sys.executable
    assert ours["args"] == [str(MCP_SERVER_PATH)]
    assert ours["env"]["VIBE_HARNESS_URL"] == env.config.api_base_url()
    assert ours["env"]["VIBE_HARNESS_NOTE_ID"] == "abc123"
    assert ours["env"]["VIBE_HARNESS_ASK_USER_TIMEOUT"] == str(env.config.ask_user_timeout_seconds)


def test_build_cmd_keeps_mcp_tools_under_allowlist(tmp_path):
    env = make_app(tmp_path, enabled_tools=["read", "bash"])
    cmd = env.runner._build_cmd(Path("/tmp/p.md"), Path("/tmp"), Path("/tmp"),
                                kind="run")
    flags = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--enabled-tools"]
    assert flags == ["read", "bash", "harness_*"]
    # without an allowlist, nothing is appended
    env2 = make_app(tmp_path / "b")
    cmd2 = env2.runner._build_cmd(Path("/tmp/p.md"), Path("/tmp"), Path("/tmp"),
                                  kind="run")
    assert "--enabled-tools" not in cmd2
    # MCP disabled -> no glob appended
    env3 = make_app(tmp_path / "c", enabled_tools=["read"], mcp_enabled=False)
    cmd3 = env3.runner._build_cmd(Path("/tmp/p.md"), Path("/tmp"), Path("/tmp"),
                                  kind="run")
    flags3 = [cmd3[i + 1] for i, a in enumerate(cmd3) if a == "--enabled-tools"]
    assert flags3 == ["read"]


def test_prompts_tell_models_to_judge_and_split_first():
    for prompt in (prompt_template("spec body"),
                   fix_prompt_template("spec body", "reason")):
        assert "harness_create_card" in prompt
        assert "FIRST judge whether this spec is too big" in prompt
        assert "harness_run_card" in prompt
        assert "no way to create, delete, or switch projects" in prompt
    # MCP disabled -> rule block omitted
    assert "harness_create_card" not in prompt_template("spec", mcp_name=None)


def test_api_base_url_maps_wildcard_host():
    assert Config(host="0.0.0.0", port=9999).api_base_url() == "http://127.0.0.1:9999"
    assert Config(host="127.0.0.1", port=8734).api_base_url() == "http://127.0.0.1:8734"


def test_load_config_mcp_env_vars(monkeypatch):
    from app.config import load_config
    monkeypatch.setenv("VIBE_HARNESS_MCP_SERVER_NAME", "board")
    monkeypatch.setenv("VIBE_HARNESS_MCP_ENABLED", "0")
    cfg = load_config({})
    # load_config({}) reads the passed dict, not os.environ
    assert cfg.mcp_server_name == "harness" and cfg.mcp_enabled is True
    cfg = load_config(dict(os.environ))
    assert cfg.mcp_server_name == "board" and cfg.mcp_enabled is False


# ---------------------------------------------------------------- archives

def test_archive_tools_via_mcp(harness):
    """create_archive / list_archives / get_archive / archive_card /
    archive_cards / restore_card against the live dashboard."""
    # create archives of different types (the archive is a folder structure of
    # typed archives)
    res = call(harness, "create_archive", name="Done pile", type="cards")
    assert res["isError"] is False, res
    a1 = res["payload"]
    assert a1["type"] == "cards" and a1["item_count"] == 0
    res = call(harness, "create_archive", name="Retired deck", type="deck")
    a2 = res["payload"]
    # default type + duplicate name -> tool error carrying the 409 detail
    assert call(harness, "create_archive", name="Plain")["payload"]["type"] == "cards"
    err = call(harness, "create_archive", name="Done pile")
    assert err["isError"] is True and "409" in err["payload"]

    # list (optionally filtered by type) + get
    listed = call(harness, "list_archives")["payload"]
    assert {a["id"] for a in listed} >= {a1["id"], a2["id"]}
    by_type = call(harness, "list_archives", type="deck")["payload"]
    assert {a["id"] for a in by_type} == {a2["id"]}
    got = call(harness, "get_archive", archive_id=a1["id"])
    assert got["isError"] is False and got["payload"]["items"] == []
    assert call(harness, "get_archive", archive_id="nope99")["isError"] is True

    # run a card to accepted via the MCP, then archive the card
    card = call(harness, "create_card", title="Archivable", body="make x.py")["payload"]
    call(harness, "run_card", card_id=card["id"])
    wait_status(harness, card["id"], ("done", "failed"))
    call(harness, "accept_card", card_id=card["id"])

    # a draft cannot be archived (409: only accepted cards)
    draft = call(harness, "create_card", title="Not yet", body="b")["payload"]
    err = call(harness, "archive_card", card_id=draft["id"], archive_id=a1["id"])
    assert err["isError"] is True and "accepted" in err["payload"]

    res = call(harness, "archive_card", card_id=card["id"], archive_id=a1["id"])
    assert res["isError"] is False, res
    assert res["payload"]["archive"]["item_count"] == 1
    # off the board, listed in the archive
    assert call(harness, "get_card", card_id=card["id"])["isError"] is True
    items = call(harness, "get_archive", archive_id=a1["id"])["payload"]["items"]
    assert [n["id"] for n in items] == [card["id"]]
    assert items[0]["archived"] is True

    # move the card to another archive, then restore it to the board
    res = call(harness, "archive_card", card_id=card["id"], archive_id=a2["id"])
    assert res["isError"] is False
    assert call(harness, "get_archive", archive_id=a1["id"])["payload"]["items"] == []
    assert [n["id"] for n in call(harness, "get_archive", archive_id=a2["id"])["payload"]["items"]] == [card["id"]]
    res = call(harness, "restore_card", card_id=card["id"])
    assert res["isError"] is False, res
    assert res["payload"]["archived"] is False
    assert call(harness, "get_card", card_id=card["id"])["payload"]["status"] == "accepted"
    # restore of a board card / unknown card -> isError
    assert call(harness, "restore_card", card_id=card["id"])["isError"] is True
    assert call(harness, "restore_card", card_id="nope99")["isError"] is True


def test_archive_cards_batch_via_mcp(harness):
    """archive_cards moves a whole deck of accepted cards into one archive."""
    a = call(harness, "create_archive", name="Deck", type="deck")["payload"]
    ids = []
    for i in range(2):
        n = call(harness, "create_card", title=f"Card {i}", body="b")["payload"]
        harness.store.set_status(n["id"], "running")
        harness.store.set_status(n["id"], "done",
                                 session_patch={"review": {"approved": True, "round": 1}})
        harness.store.accept(n["id"])
        ids.append(n["id"])
    res = call(harness, "archive_cards", card_ids=ids, archive_id=a["id"])
    assert res["isError"] is False, res
    assert {n["id"] for n in res["payload"]["cards"]} == set(ids)
    assert call(harness, "get_archive", archive_id=a["id"])["payload"]["item_count"] == 2
    assert call(harness, "list_cards")["payload"] == []

    # duplicate ids in card_ids are deduped, not a 500 (reviewer find)
    n = call(harness, "create_card", title="Dup", body="b")["payload"]
    harness.store.set_status(n["id"], "running")
    harness.store.set_status(n["id"], "done",
                             session_patch={"review": {"approved": True, "round": 1}})
    harness.store.accept(n["id"])
    res = call(harness, "archive_cards", card_ids=[n["id"], n["id"]], archive_id=a["id"])
    assert res["isError"] is False, res
    assert [x["id"] for x in res["payload"]["cards"]] == [n["id"]]
    assert call(harness, "get_archive", archive_id=a["id"])["payload"]["item_count"] == 3


# ---------------------------------------------------------------- stacks

def test_stack_tools_build_rename_and_move_piles(harness):
    """A model can build a pile, name it, move cards in and out of it."""
    a = call(harness, "create_card", title="a", body="x")["payload"]
    b = call(harness, "create_card", title="b", body="x")["payload"]
    c = call(harness, "create_card", title="c", body="x")["payload"]

    created = call(harness, "create_stack", title="Merge queue",
                   card_ids=[a["id"], b["id"]])
    assert created["isError"] is False, created
    stack = created["payload"]
    assert stack["title"] == "Merge queue"
    assert stack["col"] == "draft"          # inferred from the first card
    assert stack["ids"] == [a["id"], b["id"]]   # bottom -> top

    assert [s["id"] for s in call(harness, "list_stacks")["payload"]] == [stack["id"]]

    renamed = call(harness, "update_stack", stack_id=stack["id"], title="Sprint 12")
    assert renamed["payload"]["title"] == "Sprint 12"

    grown = call(harness, "stack_cards", card_ids=[c["id"]], stack_id=stack["id"])
    assert grown["payload"]["ids"] == [a["id"], b["id"], c["id"]]

    out = call(harness, "unstack_card", card_id=c["id"])["payload"]
    assert out["dissolved"] is False
    assert out["stack"]["ids"] == [a["id"], b["id"]]

    gone = call(harness, "delete_stack", stack_id=stack["id"])["payload"]
    assert gone["deleted"] == stack["id"]
    assert call(harness, "list_stacks")["payload"] == []
    # the cards are still on the board
    assert {n["id"] for n in call(harness, "list_cards")["payload"]} == {a["id"], b["id"], c["id"]}


def test_stack_cards_without_stack_id_creates_a_pile(harness):
    a = call(harness, "create_card", title="a", body="x")["payload"]
    b = call(harness, "create_card", title="b", body="x")["payload"]
    res = call(harness, "stack_cards", card_ids=[a["id"], b["id"]], title="New pile")
    assert res["isError"] is False, res
    assert res["payload"]["title"] == "New pile"
    assert res["payload"]["ids"] == [a["id"], b["id"]]


def test_stack_tools_reject_a_wrong_column(harness):
    a = call(harness, "create_card", title="a", body="x")["payload"]
    b = call(harness, "create_card", title="b", body="x")["payload"]
    # Both cards are drafts: they cannot be piled in the done column.
    res = call(harness, "create_stack", title="x", card_ids=[a["id"], b["id"]],
               col="done")
    assert res["isError"] is True
    assert "column" in res["payload"]
    assert call(harness, "list_stacks")["payload"] == []


def test_stack_tools_report_unknown_stacks(harness):
    assert call(harness, "update_stack", stack_id="st-nope", title="x")["isError"] is True
    assert call(harness, "delete_stack", stack_id="st-nope")["isError"] is True
    assert call(harness, "unstack_card", card_id="nope")["isError"] is True


def test_archive_stack_via_mcp(harness):
    """archive_stack retires a whole deck: one archive per deck, named after it,
    with the deck recorded in the manifest so the structure survives."""
    ids = []
    for i in range(2):
        n = call(harness, "create_card", title=f"Deck card {i}", body="b")["payload"]
        harness.store.set_status(n["id"], "running")
        harness.store.set_status(n["id"], "done",
                                 session_patch={"review": {"approved": True, "round": 1}})
        harness.store.accept(n["id"])
        ids.append(n["id"])
    stack = call(harness, "create_stack", title="Deck feature", card_ids=ids)["payload"]

    res = call(harness, "archive_stack", stack_id=stack["id"])
    assert res["isError"] is False, res
    a = res["payload"]["archive"]
    assert a["name"] == "Deck feature" and a["type"] == "deck"
    assert a["item_count"] == 2
    assert [s["id"] for s in a["archived_stacks"]] == [stack["id"]]
    assert call(harness, "list_cards")["payload"] == []
    assert call(harness, "list_stacks")["payload"] == []
    assert call(harness, "archive_stack", stack_id="st-nope")["isError"] is True
