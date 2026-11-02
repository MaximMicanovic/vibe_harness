"""Live token usage: the vibe-journal scanner (app/liveusage.py) and its
exposure through the runner/API while a session is running.

Hermetic: a fake `vibe` binary that hangs until killed, and a fake VIBE_HOME
whose `logs/session/active/*.lock.json` maps the spawned pid to a fake session
whose journal we write by hand.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.runner as runner_mod
from app import liveusage
from app.config import Config
from app.main import create_app
from app.cards import CardStore
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


# ---------------------------------------------------------------- helpers

def _usage_record(total: int, input: int | None = None, output: int = 10,
                  cached: int | None = None) -> str:
    """One journal line shaped like vibe's live session-state projection."""
    if input is None:
        input = total - output
    context_usage = {"inputTokens": input, "outputTokens": output,
                     "totalTokens": total}
    if cached is not None:
        context_usage["cachedInputTokens"] = cached
    rec = {"type": "projection_delta", "sequence": 1,
           "payload": {"delta": [
               {"op": "set_envelope",
                "state": {"session": {"contextUsage": context_usage}}},
               {"op": "append_entry",
                "entry": {"type": "message", "role": "assistant", "text": "hi"}},
           ]}}
    return json.dumps(rec)


def _write_journal(journal_dir: Path, lines: list[str], name: str = "0000000000000001.jsonl") -> Path:
    journal_dir.mkdir(parents=True, exist_ok=True)
    f = journal_dir / name
    with open(f, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")
    return f


def _fake_home(tmp_path: Path, pid: int, session_id: str = "sess-1") -> Path:
    """A fake VIBE_HOME with an active-session lock owned by `pid`."""
    home = tmp_path / "vibe-home"
    active = home / "logs" / "session" / "active"
    active.mkdir(parents=True, exist_ok=True)
    (active / f"{session_id}.lock.json").write_text(
        json.dumps({"process_id": pid, "session_id": session_id}))
    # A child-session lock with the same pid must be ignored.
    (active / "child-xyz.lock.json").write_text(
        json.dumps({"process_id": pid, "session_id": "child-xyz"}))
    return home


def _journal_dir(home: Path, session_id: str = "sess-1") -> Path:
    return home / "logs" / "session" / "unified" / session_id / "journal"


# ------------------------------------------------------- unit: liveusage

def test_find_session_id_by_pid(tmp_path):
    home = _fake_home(tmp_path, pid=4242)
    assert liveusage.find_session_id_by_pid(home, 4242) == "sess-1"
    assert liveusage.find_session_id_by_pid(home, 9999) is None
    # Missing active dir -> None, no crash.
    assert liveusage.find_session_id_by_pid(tmp_path / "nope", 4242) is None


def test_find_session_id_by_pid_process_tree(tmp_path):
    # The lock may name a child of the spawned process (vibe launcher ->
    # vibe-rs -> vibe-app-server): matching walks the process tree.
    home = _fake_home(tmp_path, pid=5555)  # lock pid = app-server child
    launcher_pid = 5000
    children = {5000: [5100], 5100: [5555], 9999: [1234]}  # 5000 -> 5100 -> 5555
    assert liveusage.find_session_id_by_pid(home, launcher_pid, children) == "sess-1"
    assert liveusage.find_session_id_by_pid(home, 9999, children) is None
    # _descendants itself: BFS, cycles safe, limit respected.
    assert liveusage._descendants(5000, children) == {5100, 5555}
    assert liveusage._descendants(5555, children) == set()
    cyclic = {1: [2], 2: [1]}
    assert liveusage._descendants(1, cyclic) == {2}


def test_scan_journal_incremental(tmp_path):
    jdir = _journal_dir(_fake_home(tmp_path, 1))
    f = _write_journal(jdir, [_usage_record(100)])
    offsets: dict[str, int] = {}
    assert liveusage.scan_journal_dir(jdir, offsets) == {
        "total": 100, "input": 90, "output": 10, "cached": None}
    size_after_first = offsets[f.name]
    assert size_after_first == f.stat().st_size  # everything consumed

    # Second scan with no new data reads nothing new.
    assert liveusage.scan_journal_dir(jdir, offsets) is None
    assert offsets[f.name] == size_after_first

    # Appended usage line: only the new bytes are scanned, max wins.
    _write_journal(jdir, ['{"type":"core_input"}', _usage_record(250)])
    assert liveusage.scan_journal_dir(jdir, offsets) == {
        "total": 250, "input": 240, "output": 10, "cached": None}

    # A partially written line (no trailing newline yet) is left for later.
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(_usage_record(999))  # no newline
    off_before = offsets[f.name]
    assert liveusage.scan_journal_dir(jdir, offsets) is None
    assert offsets[f.name] == off_before
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("\n")
    assert liveusage.scan_journal_dir(jdir, offsets) == {
        "total": 999, "input": 989, "output": 10, "cached": None}

    # Garbage lines and missing dir are tolerated.
    _write_journal(jdir, ["not json at all"])
    assert liveusage.scan_journal_dir(jdir, offsets) is None
    assert liveusage.scan_journal_dir(tmp_path / "missing", {}) is None


def test_scan_journal_token_breakdown(tmp_path):
    """The scan returns the token-type breakdown (cached/input/output) of the
    record with the highest totalTokens, not just the total."""
    jdir = _journal_dir(_fake_home(tmp_path, 1))
    offsets: dict[str, int] = {}

    # A record carrying cachedInputTokens: all three types come through.
    _write_journal(jdir, [_usage_record(500, input=390, output=10, cached=100)])
    assert liveusage.scan_journal_dir(jdir, offsets) == {
        "total": 500, "input": 390, "output": 10, "cached": 100}

    # A later, larger record WITHOUT cached tokens wins as a whole — the
    # breakdown always describes the max-total record.
    _write_journal(jdir, [_usage_record(1200, input=1190, output=10)])
    assert liveusage.scan_journal_dir(jdir, offsets) == {
        "total": 1200, "input": 1190, "output": 10, "cached": None}

    # A smaller record with a breakdown must not downgrade the max.
    _write_journal(jdir, [_usage_record(300, input=200, output=10, cached=90)])
    assert liveusage.scan_journal_dir(jdir, offsets) == {
        "total": 300, "input": 200, "output": 10, "cached": 90}

    # Records missing some usage fields report them as None (not 0).
    rec = json.dumps({"type": "projection_delta", "payload": {"delta": [
        {"op": "set_envelope", "state": {"session": {"contextUsage": {
            "totalTokens": 50}}}}]}})
    _write_journal(jdir, [rec])
    assert liveusage.scan_journal_dir(jdir, offsets) == {
        "total": 50, "input": None, "output": None, "cached": None}


def test_vibe_home_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("VIBE_HOME", str(tmp_path / "custom-home"))
    assert liveusage.vibe_home() == tmp_path / "custom-home"


# ------------------------------------------------------ integration: API

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


def _wait_status(store, card_id, status, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.get(card_id)["status"] == status:
            return True
        time.sleep(0.1)
    return False


@pytest.fixture()
def env(tmp_path, monkeypatch):
    # Point vibe's home at a fake dir we control (read at scan time).
    monkeypatch.setenv("VIBE_HOME", str(tmp_path / "vibe-home"))
    # Disable the scan rate limit so appends are visible on the next call.
    monkeypatch.setattr(runner_mod, "LIVE_USAGE_SCAN_INTERVAL", 0.0)
    e = _make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()
    # Make sure no fake vibe is left hanging.
    for card in e.store.list():
        if card["status"] in ("running", "reviewing"):
            try:
                e.runner.stop(card["id"])
            except Exception:
                pass


def _start_card(env) -> str:
    r = env.client.post("/api/cards", json={"title": "live tokens", "body": "x",
                                            "auto_run": True})
    assert r.status_code == 201, r.text
    card = r.json()
    assert card["status"] == "running"
    assert card["session"]["pid"]
    return card["id"]


def test_live_tokens_per_card_and_total(env):
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]

    # No journal yet: no live data, and the recorded chip stays absent.
    card = env.client.get(f"/api/cards/{card_id}").json()
    assert "live_tokens" not in (card["session"] or {})
    stats = env.client.get("/api/stats").json()
    assert stats["live_tokens"] == 0

    # Vibe starts writing its journal: lock (pid -> session) + usage line.
    home = liveusage.vibe_home()
    _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home), [_usage_record(500, input=390, output=10, cached=100)])

    card = env.client.get(f"/api/cards/{card_id}").json()
    assert card["session"]["live_tokens"] == 500  # fresh run: recorded(0) + 500
    # ...and the token-type breakdown rides along for the hover tooltip.
    assert card["session"]["live_token_breakdown"] == {
        "input": 390, "output": 10, "cached": 100}
    cards = env.client.get("/api/cards").json()
    assert cards[0]["session"]["live_tokens"] == 500
    assert cards[0]["session"]["live_token_breakdown"] == {
        "input": 390, "output": 10, "cached": 100}

    stats = env.client.get("/api/stats").json()
    assert stats["total_tokens"] == 500
    assert stats["live_tokens"] == 500
    assert stats["token_breakdown"] == {"input": 390, "output": 10, "cached": 100}

    # The journal grows while the session runs -> the card and total follow.
    _write_journal(_journal_dir(home), [_usage_record(1200, input=1000, output=200, cached=800)])
    card = env.client.get(f"/api/cards/{card_id}").json()
    assert card["session"]["live_tokens"] == 1200
    assert card["session"]["live_token_breakdown"] == {
        "input": 1000, "output": 200, "cached": 800}
    stats = env.client.get("/api/stats").json()
    assert stats["total_tokens"] == 1200
    assert stats["live_tokens"] == 1200
    assert stats["token_breakdown"] == {"input": 1000, "output": 200, "cached": 800}

    # Stopping the card ends the live display (finalize records export.json).
    assert env.client.post(f"/api/cards/{card_id}/stop").status_code == 204
    assert _wait_status(env.store, card_id, "stopped")
    card = env.client.get(f"/api/cards/{card_id}").json()
    assert "live_tokens" not in (card["session"] or {})
    assert "live_token_breakdown" not in (card["session"] or {})
    stats = env.client.get("/api/stats").json()
    assert stats["live_tokens"] == 0


def test_annotate_card_resumed_vs_fresh(env):
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = liveusage.vibe_home()
    _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home), [_usage_record(800, input=600, output=200, cached=500)])

    with env.runner._lock:
        entry = env.runner._registry[card_id]

    recorded_bd = {"input": 100, "output": 50, "cached": 20}

    # Fresh round: live counter is added to the previously recorded tokens.
    entry["resumed"] = False
    out = env.runner.annotate_card({"id": card_id, "status": "running",
                                    "session": {"tokens": 300,
                                                "token_breakdown": recorded_bd}})
    assert out["session"]["live_tokens"] == 1100
    # ...and the breakdowns add up per type (fresh round starts from zero).
    assert out["session"]["live_token_breakdown"] == {
        "input": 700, "output": 250, "cached": 520}

    # Resumed round: the live counter is cumulative, never below the record.
    entry["resumed"] = True
    out = env.runner.annotate_card({"id": card_id, "status": "running",
                                    "session": {"tokens": 300,
                                                "token_breakdown": recorded_bd}})
    assert out["session"]["live_tokens"] == 800
    # ...and the breakdown is the per-type max (the journal is cumulative).
    assert out["session"]["live_token_breakdown"] == {
        "input": 600, "output": 200, "cached": 500}
    out = env.runner.annotate_card({"id": card_id, "status": "running",
                                    "session": {"tokens": 5000,
                                                "token_breakdown": {"input": 9000, "output": 1, "cached": None}}})
    assert out["session"]["live_tokens"] == 5000
    assert out["session"]["live_token_breakdown"] == {
        "input": 9000, "output": 200, "cached": 500}

    # No recorded breakdown: the live breakdown alone is shown (fresh round).
    entry["resumed"] = False
    out = env.runner.annotate_card({"id": card_id, "status": "running",
                                    "session": {"tokens": 300}})
    assert out["session"]["live_tokens"] == 1100
    assert out["session"]["live_token_breakdown"] == {
        "input": 600, "output": 200, "cached": 500}

    # The store's card is not mutated by annotation.
    assert "live_tokens" not in (env.store.get(card_id)["session"] or {})

    assert env.client.post(f"/api/cards/{card_id}/stop").status_code == 204
    assert _wait_status(env.store, card_id, "stopped")


def test_annotate_card_live_without_breakdown(env):
    # A journal record without type fields still yields live_tokens but no
    # breakdown — the hover tooltip then just has no per-type numbers.
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = liveusage.vibe_home()
    _fake_home(env.tmp_path, pid)
    rec = json.dumps({"type": "projection_delta", "payload": {"delta": [
        {"op": "set_envelope", "state": {"session": {"contextUsage": {
            "totalTokens": 640}}}}]}})
    _write_journal(_journal_dir(home), [rec])

    card = env.client.get(f"/api/cards/{card_id}").json()
    assert card["session"]["live_tokens"] == 640
    assert "live_token_breakdown" not in card["session"]
    stats = env.client.get("/api/stats").json()
    assert stats["total_tokens"] == 640
    assert stats["token_breakdown"] == {"input": None, "output": None, "cached": None}

    assert env.client.post(f"/api/cards/{card_id}/stop").status_code == 204
    assert _wait_status(env.store, card_id, "stopped")


def test_no_live_io_when_nothing_runs(env):
    # A draft card has no registry entry: live_usage is a pure dict lookup.
    r = env.client.post("/api/cards", json={"title": "draft", "body": "x"})
    card_id = r.json()["id"]
    assert env.runner.live_usage(card_id) is None
    card = env.client.get(f"/api/cards/{card_id}").json()
    assert "live_tokens" not in (card["session"] or {})


def test_live_counter_never_dips_across_a_round_boundary(env):
    """The displayed total only ever climbs — including across a
    review -> fixer boundary, where the live counter disappears for a moment.

    Sequence: run round live (500) -> run ends, review starts (no live value
    yet) -> review live (620) -> review ends, fixer resumes with a cumulative
    counter below the last shown value -> fixer climbs past it.
    """
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = liveusage.vibe_home()
    _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home), [_usage_record(500, input=400, output=100)])

    with env.runner._lock:
        entry = env.runner._registry[card_id]

    # 1. The run round is live: recorded 0 + 500.
    card = env.client.get(f"/api/cards/{card_id}").json()
    assert card["session"]["live_tokens"] == 500

    # 2. The run round ended (export 42 recorded, last live value kept as the
    #    floor) and the review round has not produced a live value yet.
    entry["live_tokens"] = None
    entry["resumed"] = False
    out = env.runner.annotate_card({
        "id": card_id, "status": "reviewing",
        "session": {"tokens": 42, "live_high_water": 500}})
    assert out["session"]["card_tokens"] == 500      # floor, not 42
    assert "live_tokens" not in out["session"]       # no live round yet

    # 3. The review round's journal climbs: 500 + 120.
    entry["live_tokens"] = 120
    out = env.runner.annotate_card({
        "id": card_id, "status": "reviewing",
        "session": {"tokens": 42, "live_high_water": 500}})
    assert out["session"]["live_tokens"] == 620

    # 4. The review ended (42 + 120 recorded) and the fixer RESUMED: its
    #    cumulative counter (150) is below the last shown value -> no dip.
    entry["resumed"] = True
    entry["live_tokens"] = 150
    out = env.runner.annotate_card({
        "id": card_id, "status": "running",
        "session": {"tokens": 42, "reviews": [{"round": 1, "tokens": 120}],
                    "live_high_water": 620}})
    assert out["session"]["live_tokens"] == 620

    # 5. ...and it climbs again as the fixer works.
    entry["live_tokens"] = 900
    out = env.runner.annotate_card({
        "id": card_id, "status": "running",
        "session": {"tokens": 42, "reviews": [{"round": 1, "tokens": 120}],
                    "live_high_water": 620}})
    assert out["session"]["live_tokens"] == 900

    assert env.client.post(f"/api/cards/{card_id}/stop").status_code == 204
    assert _wait_status(env.store, card_id, "stopped")


def test_the_last_live_value_is_kept_when_the_round_ends(env):
    """A round ending persists the last live total as the card's floor, so the
    chip does not fall back to the (smaller) recorded export."""
    card_id = _start_card(env)
    pid = env.store.get(card_id)["session"]["pid"]
    home = liveusage.vibe_home()
    _fake_home(env.tmp_path, pid)
    _write_journal(_journal_dir(home), [_usage_record(500, input=400, output=100)])
    card = env.client.get(f"/api/cards/{card_id}").json()
    assert card["session"]["live_tokens"] == 500

    assert env.client.post(f"/api/cards/{card_id}/stop").status_code == 204
    assert _wait_status(env.store, card_id, "stopped")

    final = env.client.get(f"/api/cards/{card_id}").json()
    # The fake vibe writes no export: the recorded total is 0, but the card
    # keeps showing what it had reached instead of dropping to nothing.
    assert final["session"]["live_high_water"] == 500
    assert final["session"]["card_tokens"] == 500
    assert "live_tokens" not in final["session"]
    stats = env.client.get("/api/stats").json()
    assert stats["total_tokens"] == 500
    assert stats["live_tokens"] == 0
