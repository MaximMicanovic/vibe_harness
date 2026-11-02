"""The live activity feed: `GET /api/cards/<id>/activity` (card e02cd4).

The vibe journal is appended WHILE a session runs, so it is the only place
that shows what the model is doing right now. `app/activity.py` turns its
records into a small event feed; the endpoint pages it with a line cursor.

Hermetic: a fake `vibe` binary that hangs until killed, and a fake VIBE_HOME
whose `logs/session/active/*.lock.json` maps the spawned pid to a fake session
whose journal we write by hand — the same setup as tests/test_liveusage.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import activity, liveusage
from app.cards import CardStore
from app.config import Config
from app.main import create_app
from app.projects import ProjectStore
from app.runner import SessionRunner

FAKE_VIBE_SLOW = """#!/bin/sh
# Fake vibe that hangs until SIGTERM (so the card stays 'running' in tests).
workdir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2;;
    *) shift;;
  esac
done
mkdir -p "$workdir"
echo "fake vibe slow start"
trap 'echo "fake vibe terminated"; exit 143' TERM
sleep 30
exit 0
"""


# --------------------------------------------------------------- journal lines

def _text_record(text: str, at: int = 1700000000000) -> str:
    """An assistant message: payload.delta[].entry.content[].text."""
    return json.dumps({"type": "projection_delta", "payload": {"delta": [
        {"op": "append_entry", "entry": {
            "type": "message", "role": "assistant", "id": "assistant-1",
            "createdAt": at,
            "content": [{"type": "text", "text": text}]}}]}})


def _reasoning_record(text: str, at: int = 1700000000001) -> str:
    """The model's thinking: payload.delta[].entry.text, id `reasoning-…`."""
    return json.dumps({"type": "projection_delta", "payload": {"delta": [
        {"op": "append_entry", "entry": {
            "type": "reasoning", "id": "reasoning-abc-0", "createdAt": at,
            "text": text}}]}})


def _tool_record(name: str, arguments: dict) -> str:
    """A tool call: payload.request with kind "tool" and call.arguments."""
    return json.dumps({"type": "action_intent", "payload": {
        "kind": "tool", "action_id": "a1",
        "request": {"call": {"name": name, "arguments": arguments}}}})


def _result_record(ok: bool, output: str) -> str:
    """A tool result: payload.result (tool_succeeded / tool_failed)."""
    return json.dumps({"type": "action_result", "payload": {
        "action_id": "a1",
        "result": {"type": "tool_succeeded" if ok else "tool_failed",
                   "result": {"structured_content": {"stdout": output}}}}})


def _status_record(text: str, at: int = 1700000000002) -> str:
    """A status line: payload.delta[].entry.detail.display.statusText."""
    return json.dumps({"type": "projection_delta", "payload": {"delta": [
        {"op": "append_entry", "entry": {
            "type": "effect", "id": "effect-1", "createdAt": at,
            "detail": {"kind": "tool", "toolName": "file_system.bash",
                       "display": {"statusText": text}}}}]}})


# ------------------------------------------------------------------- fixtures

def _fake_home(tmp_path: Path, pid: int, session_id: str = "sess-1") -> Path:
    """A fake VIBE_HOME with an active-session lock owned by `pid`."""
    home = tmp_path / "vibe-home"
    active = home / "logs" / "session" / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / f"{session_id}.lock.json").write_text(
        json.dumps({"process_id": pid, "session_id": session_id}))
    return home


def _journal_dir(home: Path, session_id: str = "sess-1") -> Path:
    return home / "logs" / "session" / "unified" / session_id / "journal"


def _write_journal(journal_dir: Path, lines: list[str],
                   name: str = "0000000000000001.jsonl") -> Path:
    journal_dir.mkdir(parents=True, exist_ok=True)
    f = journal_dir / name
    with open(f, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    return f


def _make_env(tmp_path: Path) -> SimpleNamespace:
    store = CardStore(tmp_path / "cards")
    sessions_dir = tmp_path / "sessions"
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(FAKE_VIBE_SLOW)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30)
    projects = ProjectStore(tmp_path / "projects.json")
    runner = SessionRunner(store, config, sessions_dir=sessions_dir, projects=projects)
    app = create_app(config=config, store=store, runner=runner, projects=projects)
    return SimpleNamespace(client=TestClient(app), store=store, runner=runner,
                           tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path / "vibe-home"))
    e = _make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()
    for card in e.store.list():
        if card["status"] in ("running", "reviewing"):
            try:
                e.runner.stop(card["id"])
            except Exception:
                pass


def _start_card(env) -> str:
    r = env.client.post("/api/cards", json={"title": "live activity", "body": "x",
                                            "auto_run": True})
    assert r.status_code == 201, r.text
    card = r.json()
    assert card["status"] == "running"
    return card["id"]


def _activity(env, card_id: str, cursor: int | None = None) -> dict:
    url = f"/api/cards/{card_id}/activity"
    if cursor is not None:
        url += f"?cursor={cursor}"
    r = env.client.get(url)
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------- unit: the parser

def test_parse_journal_line_shapes():
    """Every documented journal shape becomes one event of the right kind."""
    events = activity.parse_journal_line(_text_record("hello"))
    assert events == [{"kind": "text", "at": events[0]["at"], "text": "hello"}]
    assert events[0]["at"]  # a timestamp string, not None

    events = activity.parse_journal_line(_reasoning_record("thinking hard"))
    assert [(e["kind"], e["text"]) for e in events] == [("reasoning", "thinking hard")]

    events = activity.parse_journal_line(_tool_record("file_system.bash",
                                                      {"command": "pytest -q"}))
    assert [(e["kind"], e["tool"], e["summary"]) for e in events] == [
        ("tool", "file_system.bash", "pytest -q")]

    events = activity.parse_journal_line(_result_record(True, "all good"))
    assert [(e["kind"], e["ok"], e["summary"]) for e in events] == [
        ("result", True, "all good")]

    events = activity.parse_journal_line(_status_record("Running file_system.bash"))
    assert [(e["kind"], e["text"]) for e in events] == [
        ("status", "Running file_system.bash")]

    # Garbage, empty and unrelated records produce no events and never raise.
    assert activity.parse_journal_line("not json") == []
    assert activity.parse_journal_line("") == []
    assert activity.parse_journal_line('{"type": "core_input", "payload": {}}') == []


def test_parse_journal_line_ignores_user_messages():
    """Only the assistant's output is activity — the prompt is not."""
    rec = json.dumps({"type": "projection_delta", "payload": {"delta": [
        {"op": "append_entry", "entry": {
            "type": "message", "role": "user", "id": "user-1",
            "content": [{"type": "text", "text": "the spec"}]}}]}})
    assert activity.parse_journal_line(rec) == []


# --------------------------------------------------- endpoint: the live feed

def test_activity_events_from_a_live_journal(env):
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home), [
        _text_record("I'll run the tests."),
        _reasoning_record("the suite is slow, run it in background"),
        _tool_record("file_system.bash", {"command": "pytest -q"}),
        _result_record(True, "619 passed"),
        _status_record("Running file_system.bash"),
    ])

    feed = _activity(env, card_id)
    assert feed["cursor"] == 5
    assert [e["kind"] for e in feed["events"]] == [
        "text", "reasoning", "tool", "result", "status"]
    assert feed["events"][0]["text"] == "I'll run the tests."
    assert feed["events"][2]["tool"] == "file_system.bash"
    assert feed["events"][2]["summary"] == "pytest -q"
    assert feed["events"][3]["ok"] is True
    assert feed["events"][4]["text"] == "Running file_system.bash"
    for event in feed["events"]:
        assert event["at"], "every event carries a timestamp"


def test_activity_cursor_returns_only_new_events(env):
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = _fake_home(env.tmp_path, pid)
    jdir = _journal_dir(home)
    _write_journal(jdir, [_text_record("first")])

    feed = _activity(env, card_id)
    assert feed["cursor"] == 1
    assert [e["text"] for e in feed["events"]] == ["first"]

    # Nothing new: the same cursor comes back with no events.
    again = _activity(env, card_id, cursor=feed["cursor"])
    assert again == {"cursor": 1, "events": []}

    # Appended lines are the only thing the next call returns.
    _write_journal(jdir, [_text_record("second"), _text_record("third")])
    more = _activity(env, card_id, cursor=feed["cursor"])
    assert more["cursor"] == 3
    assert [e["text"] for e in more["events"]] == ["second", "third"]


def test_activity_first_call_returns_the_tail(env):
    """No cursor: the last ~200 events, not the whole journal."""
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home),
                   [_text_record(f"line {i}") for i in range(300)])

    feed = _activity(env, card_id)
    assert feed["cursor"] == 300          # the whole journal is consumed
    assert len(feed["events"]) == activity.DEFAULT_LIMIT == 200
    assert feed["events"][-1]["text"] == "line 299"
    assert feed["events"][0]["text"] == "line 100"


def test_activity_caps_text_and_summary(env):
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = _fake_home(env.tmp_path, pid)
    long_text = "x" * 5000
    _write_journal(_journal_dir(home), [
        _text_record(long_text),
        _tool_record("file_system.bash", {"command": "y" * 5000}),
        _result_record(False, "z" * 5000),
    ])

    feed = _activity(env, card_id)
    assert len(feed["events"][0]["text"]) == activity.MAX_TEXT == 2000
    assert len(feed["events"][1]["summary"]) == activity.MAX_TEXT
    assert len(feed["events"][2]["summary"]) == activity.MAX_TEXT
    assert feed["events"][2]["ok"] is False


def test_activity_caps_the_event_count(env):
    """One call never returns more than the limit, even with a huge journal."""
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home),
                   [_text_record(f"line {i}") for i in range(500)])

    feed = _activity(env, card_id)
    assert len(feed["events"]) == activity.DEFAULT_LIMIT
    assert feed["cursor"] == 500


def test_activity_empty_feed_without_a_journal(env):
    """A card with no session at all: an empty feed, never an error."""
    card = env.client.post("/api/cards", json={"title": "draft", "body": "x"}).json()
    assert _activity(env, card["id"]) == {"cursor": 0, "events": []}


def test_activity_unknown_card_404(env):
    r = env.client.get("/api/cards/deadbe/activity")
    assert r.status_code == 404


def test_activity_falls_back_to_the_last_session(env):
    """No live session: the last session's journal still answers."""
    card = env.client.post("/api/cards", json={"title": "done", "body": "x"}).json()
    home = env.tmp_path / "vibe-home"
    _write_journal(_journal_dir(home, "old-session"), [_text_record("from last time")])
    env.store.patch_session(card["id"], {"worker_session_id": "old-session"})

    feed = _activity(env, card["id"])
    assert feed["cursor"] == 1
    assert [e["text"] for e in feed["events"]] == ["from last time"]


def test_activity_never_raises_on_a_broken_journal(env):
    """Garbage lines, a missing dir and an unreadable file are an empty feed."""
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = _fake_home(env.tmp_path, pid)
    jdir = _journal_dir(home)
    _write_journal(jdir, ["not json at all", '{"type": "core_input"}', ""])
    assert _activity(env, card_id) == {"cursor": 3, "events": []}

    # A journal directory that cannot be read: still a 200 with an empty feed.
    jdir.chmod(0o000)
    try:
        assert _activity(env, card_id) == {"cursor": 0, "events": []}
    finally:
        jdir.chmod(0o755)


def test_activity_cursor_past_the_end_starts_over(env):
    """A new session (fresh journal) with an old cursor: show the tail again."""
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home), [_text_record("only line")])

    feed = _activity(env, card_id, cursor=99)
    assert feed["cursor"] == 1
    assert [e["text"] for e in feed["events"]] == ["only line"]


def test_activity_reads_every_journal_file_in_order(tmp_path):
    """The journal is split across files; the cursor counts lines across all."""
    jdir = tmp_path / "journal"
    _write_journal(jdir, [_text_record("one")], name="0000000000000001.jsonl")
    _write_journal(jdir, [_text_record("two")], name="0000000000000002.jsonl")

    feed = activity.read_activity(jdir)
    assert feed["cursor"] == 2
    assert [e["text"] for e in feed["events"]] == ["one", "two"]

    assert activity.read_activity(jdir, cursor=1) == {
        "cursor": 2, "events": [{"kind": "text", "at": feed["events"][1]["at"],
                                 "text": "two"}]}


def test_activity_missing_journal_dir_is_empty(tmp_path):
    assert activity.read_activity(tmp_path / "nope") == {"cursor": 0, "events": []}


def test_vibe_home_is_reused_for_the_journal(env, monkeypatch):
    """The endpoint resolves the journal through liveusage.vibe_home()."""
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home), [_text_record("hi")])
    assert liveusage.vibe_home() == home
    assert _activity(env, card_id)["events"][0]["text"] == "hi"
