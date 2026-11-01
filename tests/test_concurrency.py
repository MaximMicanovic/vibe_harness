"""Concurrency cap: Config.max_concurrent, the sidebar setting, the run queue.

Spec (card 03f38a): stop the harness from spawning unlimited concurrent agent
sessions. `Config.max_concurrent` (env `VIBE_HARNESS_MAX_CONCURRENT`, default 3,
0 = unlimited) is the fallback; the sidebar's Settings panel persists an
override in `settings.json` and the runner reads it at run time. A run that
finds no free slot is QUEUED (not an error) and starts automatically, oldest
queue time first, when a session ends.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.cards import CardRunning, CardStore, InvalidTransition
from app.config import Config, load_config
from app.main import create_app
from app.projects import ProjectStore
from app.runner import SessionRunner
from app.settings import SettingsStore

TESTS_DIR = Path(__file__).resolve().parent
QUEUE_CHIP_JS = TESTS_DIR / "queue_chip_check.js"

# Slow fake vibe: stays alive until stopped, so slots stay occupied while the
# test presses Run on more cards. `exec` so SIGTERM kills it immediately — a
# shell waiting on a foreground child defers its trap (flaky 10s stops).
FAKE_VIBE_SLOW = """#!/bin/sh
exec sleep 30
"""


# ------------------------------------------------------------------- config

def test_config_max_concurrent_defaults_to_three():
    assert Config().max_concurrent == 3


def test_load_config_reads_max_concurrent():
    assert load_config({}).max_concurrent == 3
    assert load_config({"VIBE_HARNESS_MAX_CONCURRENT": "5"}).max_concurrent == 5
    # 0 = unlimited, and it must survive the env parse.
    assert load_config({"VIBE_HARNESS_MAX_CONCURRENT": "0"}).max_concurrent == 0


# ----------------------------------------------------------------- settings

def test_settings_max_concurrent_falls_back_to_config(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    assert store.max_concurrent(3) == 3          # unset -> Config fallback
    assert store.max_concurrent(0) == 0


def test_settings_max_concurrent_persists(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    assert store.set_max_concurrent(1) == 1
    # A fresh store (a server restart) reads the same value back.
    assert SettingsStore(path).max_concurrent(3) == 1
    assert store.set_max_concurrent(0) == 0      # 0 = unlimited
    assert SettingsStore(path).max_concurrent(3) == 0
    # The backend setting is not clobbered.
    store.set_backend("claude")
    assert SettingsStore(path).max_concurrent(3) == 0
    assert SettingsStore(path).backend() == "claude"


def test_settings_rejects_a_negative_max_concurrent(tmp_path):
    store = SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.set_max_concurrent(-1)
    assert store.max_concurrent(3) == 3          # nothing was written


def test_settings_ignores_a_corrupt_max_concurrent(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"max_concurrent": "lots"}), encoding="utf-8")
    assert SettingsStore(path).max_concurrent(3) == 3


# ------------------------------------------------------------------- runner

@pytest.fixture
def store(tmp_path) -> CardStore:
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir()
    return CardStore(cards_dir)


@pytest.fixture
def slow_vibe(tmp_path) -> Path:
    path = tmp_path / "fake_vibe_slow.sh"
    path.write_text(FAKE_VIBE_SLOW)
    path.chmod(0o755)
    return path


def _make_runner(store, tmp_path, vibe_bin, settings=None, **config_kwargs):
    config = Config(vibe_bin=str(vibe_bin), time_limit_seconds=30, **config_kwargs)
    return SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                         settings=settings)


def _wait_for(store, card_id, predicate, timeout=10.0):
    deadline = time.time() + timeout
    card = store.get(card_id)
    while not predicate(card):
        if time.time() > deadline:
            raise AssertionError(f"card {card_id} never matched; last={card}")
        time.sleep(0.05)
        card = store.get(card_id)
    return card


def _wait_for_status(store, card_id, status, timeout=10.0):
    return _wait_for(store, card_id, lambda c: c["status"] == status, timeout)


def test_run_queues_when_the_limit_is_reached(store, tmp_path, slow_vibe):
    """Limit 2: the third Run queues the card instead of spawning a session."""
    runner = _make_runner(store, tmp_path, slow_vibe, max_concurrent=2)
    cards = [store.create(f"Card {i}", "spec") for i in range(3)]

    runner.run(cards[0]["id"])
    runner.run(cards[1]["id"])
    _wait_for_status(store, cards[0]["id"], "running")
    _wait_for_status(store, cards[1]["id"], "running")

    returned = runner.run(cards[2]["id"])       # no raise: it queues
    assert returned["status"] == "draft"
    assert returned["session"]["queued"] is True
    assert returned["session"]["queue_reason"] == "waiting for a free slot (2/2 running)"
    assert returned["session"]["queued_at"]
    assert not runner.is_running(cards[2]["id"])
    assert runner.running_count() == 2

    for card in cards[:2]:
        runner.stop(card["id"])
    _wait_for_status(store, cards[0]["id"], "stopped")
    _wait_for_status(store, cards[1]["id"], "stopped")


def test_queued_card_starts_when_a_slot_frees(store, tmp_path, slow_vibe):
    """The queued card starts by itself once a session ends — no second click."""
    runner = _make_runner(store, tmp_path, slow_vibe, max_concurrent=1)
    first = store.create("First", "spec")
    second = store.create("Second", "spec")

    runner.run(first["id"])
    _wait_for_status(store, first["id"], "running")
    runner.run(second["id"])
    assert store.get(second["id"])["session"]["queued"] is True

    runner.stop(first["id"])
    started = _wait_for_status(store, second["id"], "running")
    assert started["session"]["queued"] is False
    assert started["session"]["queue_reason"] is None
    assert started["session"]["queued_at"] is None

    runner.stop(second["id"])
    _wait_for_status(store, second["id"], "stopped")


def test_queued_cards_start_in_fifo_order(store, tmp_path, slow_vibe):
    """No starvation: queued cards start in the order they were queued, not in
    store.list() order (which is most-recently-updated first)."""
    runner = _make_runner(store, tmp_path, slow_vibe, max_concurrent=1)
    running = store.create("Running", "spec")
    runner.run(running["id"])
    _wait_for_status(store, running["id"], "running")

    queued = [store.create(f"Queued {i}", "spec") for i in range(3)]
    for card in queued:
        runner.run(card["id"])
    for card in queued:
        assert store.get(card["id"])["session"]["queued"] is True

    runner.stop(running["id"])
    _wait_for_status(store, queued[0]["id"], "running")
    # The later cards keep waiting while the single slot is taken.
    assert store.get(queued[1]["id"])["session"]["queued"] is True
    assert store.get(queued[2]["id"])["session"]["queued"] is True

    runner.stop(queued[0]["id"])
    _wait_for_status(store, queued[1]["id"], "running")
    assert store.get(queued[2]["id"])["session"]["queued"] is True

    runner.stop(queued[1]["id"])
    _wait_for_status(store, queued[2]["id"], "running")
    runner.stop(queued[2]["id"])
    _wait_for_status(store, queued[2]["id"], "stopped")


def test_limit_zero_means_unlimited(store, tmp_path, slow_vibe):
    runner = _make_runner(store, tmp_path, slow_vibe, max_concurrent=0)
    cards = [store.create(f"Card {i}", "spec") for i in range(4)]
    for card in cards:
        runner.run(card["id"])
    for card in cards:
        _wait_for_status(store, card["id"], "running")
    assert runner.running_count() == 4
    for card in cards:
        runner.stop(card["id"])
        _wait_for_status(store, card["id"], "stopped")


def test_runner_reads_the_setting_at_run_time(store, tmp_path, slow_vibe):
    """The sidebar setting wins over Config, and a change applies without a
    restart (the runner reads it per run, not at boot)."""
    settings = SettingsStore(tmp_path / "settings.json")
    runner = _make_runner(store, tmp_path, slow_vibe, settings=settings,
                          max_concurrent=0)      # config says unlimited
    settings.set_max_concurrent(1)               # the sidebar says one at a time

    first = store.create("First", "spec")
    second = store.create("Second", "spec")
    runner.run(first["id"])
    _wait_for_status(store, first["id"], "running")
    runner.run(second["id"])
    assert store.get(second["id"])["session"]["queued"] is True

    # Raising the cap takes effect immediately: the queued card starts.
    settings.set_max_concurrent(0)
    runner._drain_queued_runs()
    _wait_for_status(store, second["id"], "running")

    for card in (first, second):
        runner.stop(card["id"])
        _wait_for_status(store, card["id"], "stopped")


def test_drain_stops_at_the_limit(store, tmp_path, slow_vibe):
    """The drain starts at most `limit` sessions and leaves the rest queued."""
    runner = _make_runner(store, tmp_path, slow_vibe, max_concurrent=2)
    cards = [store.create(f"Card {i}", "spec") for i in range(4)]
    for card in cards:
        runner.run(card["id"])
    assert runner.running_count() == 2
    assert store.get(cards[2]["id"])["session"]["queued"] is True
    assert store.get(cards[3]["id"])["session"]["queued"] is True

    runner._drain_queued_runs()                  # no free slot: nothing starts
    assert runner.running_count() == 2
    assert store.get(cards[2]["id"])["session"]["queued"] is True

    for card in cards[:2]:
        runner.stop(card["id"])
        _wait_for_status(store, card["id"], "stopped")
    # Two slots freed -> the two queued cards start, in order.
    _wait_for_status(store, cards[2]["id"], "running")
    _wait_for_status(store, cards[3]["id"], "running")
    for card in cards[2:]:
        runner.stop(card["id"])
        _wait_for_status(store, card["id"], "stopped")


# ------------------------------------------------------- drain / slot races

def test_concurrent_drains_start_a_queued_card_once(store, tmp_path, slow_vibe,
                                                    monkeypatch):
    """Two sessions ending together both call _drain_queued_runs(). The queued
    card must start ONCE: a duplicate spawn overwrites the registry entry, the
    second _finalize then dies before releasing the slot, and the cap silently
    drops to N-1 for the rest of the process."""
    runner = _make_runner(store, tmp_path, slow_vibe, max_concurrent=1)
    running = store.create("Running", "spec")
    runner.run(running["id"])
    _wait_for_status(store, running["id"], "running")
    queued = store.create("Queued", "spec")
    runner.run(queued["id"])
    assert store.get(queued["id"])["session"]["queued"] is True

    # Widen the window between the limit check and the queue patch, so both
    # drains are inside the loop at the same time (what two sessions ending
    # together do).
    real_merge = runner._merge_in_progress_for

    def slow_merge(card):
        time.sleep(0.3)
        return real_merge(card)

    monkeypatch.setattr(runner, "_merge_in_progress_for", slow_merge)

    calls = []
    real_run = runner.run

    def counting_run(card_id):
        calls.append(card_id)
        return real_run(card_id)

    monkeypatch.setattr(runner, "run", counting_run)

    # The running session ends: its slot is free, exactly the state _finalize
    # leaves behind before draining.
    with runner._lock:
        ended = runner._registry.pop(running["id"], None)

    threads = [threading.Thread(target=runner._drain_queued_runs) for _ in range(2)]
    try:
        threads[0].start()
        time.sleep(0.1)      # thread 1 is inside the drain, past the limit check
        threads[1].start()
        for t in threads:
            t.join(timeout=20)
        assert calls == [queued["id"]], (
            f"the queued card must start exactly once, got {calls}")
        assert runner.running_count() == 1
    finally:
        ended["proc"].kill()
        ended["proc"].wait()
        entry = runner._registry.get(queued["id"])
        if entry is not None:
            entry["proc"].kill()
            entry["proc"].wait()
        # Let both finalize threads release their slots before the tmp dir goes.
        deadline = time.time() + 10
        while runner.running_count() and time.time() < deadline:
            time.sleep(0.05)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_finalize_releases_the_slot_when_recording_fails(store, tmp_path,
                                                         slow_vibe, monkeypatch):
    """A failure while recording the outcome must not leave a stale registry
    entry — that would shrink the concurrency cap for the rest of the process
    (the reviewer's InvalidTransition case)."""
    runner = _make_runner(store, tmp_path, slow_vibe, max_concurrent=1)
    card = store.create("Card", "spec")
    runner.run(card["id"])
    _wait_for_status(store, card["id"], "running")

    def failing_set_status(card_id, status, **kwargs):
        raise InvalidTransition("the card already moved on")

    monkeypatch.setattr(store, "set_status", failing_set_status)
    runner.stop(card["id"])          # the session ends -> _finalize records it

    deadline = time.time() + 10
    while runner.running_count() and time.time() < deadline:
        time.sleep(0.05)
    assert runner.running_count() == 0, (
        "a failed outcome write must still release the session slot")


def test_spawn_refuses_a_second_live_session(store, tmp_path, slow_vibe):
    """One live session per card: a second spawn would overwrite the registry
    entry, leaving the first process unmanaged and its slot unreleasable."""
    runner = _make_runner(store, tmp_path, slow_vibe)
    card = store.create("Card", "spec")
    proc = subprocess.Popen(["sleep", "30"])
    with runner._lock:
        runner._registry[card["id"]] = {"proc": proc, "stop_requested": False,
                                        "kind": "run", "round": 1}
    try:
        with pytest.raises(CardRunning):
            runner._spawn(card["id"], kind="run", prompt="spec")
    finally:
        proc.kill()
        proc.wait()
        entry = runner._registry.get(card["id"])
        if entry is not None and entry["proc"] is not proc:
            entry["proc"].kill()     # only on the unfixed code path
            entry["proc"].wait()


# ---------------------------------------------------------------------- API
def make_env(tmp_path: Path, **config_kwargs) -> SimpleNamespace:
    cards_dir = tmp_path / "cards"
    sessions_dir = tmp_path / "sessions"
    store = CardStore(cards_dir)
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(FAKE_VIBE_SLOW)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30, **config_kwargs)
    projects = ProjectStore(tmp_path / "projects.json")
    settings = SettingsStore(tmp_path / "settings.json")
    runner = SessionRunner(store, config, sessions_dir=sessions_dir,
                           projects=projects, settings=settings)
    app = create_app(config=config, store=store, runner=runner,
                     projects=projects, settings=settings)
    client = TestClient(app)
    return SimpleNamespace(client=client, store=store, runner=runner,
                           projects=projects, settings=settings,
                           sessions_dir=sessions_dir, tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def _create(client, title="Hello"):
    r = client.post("/api/cards", json={"title": title, "body": "do a thing"})
    assert r.status_code == 201, r.text
    return r.json()


def test_api_settings_exposes_max_concurrent_and_occupancy(env):
    data = env.client.get("/api/settings").json()
    assert data["max_concurrent"] == 3           # Config fallback
    assert data["running"] == 0

    r = env.client.put("/api/settings", json={"max_concurrent": 1})
    assert r.status_code == 200, r.text
    assert r.json()["max_concurrent"] == 1
    assert env.client.get("/api/settings").json()["max_concurrent"] == 1
    # Survives a restart: a fresh store reads the same file.
    assert SettingsStore(env.tmp_path / "settings.json").max_concurrent(3) == 1

    # 0 = unlimited is a valid value.
    assert env.client.put("/api/settings", json={"max_concurrent": 0}).json()["max_concurrent"] == 0
    # Negative values are rejected, and the stored value is untouched.
    assert env.client.put("/api/settings", json={"max_concurrent": -1}).status_code == 422
    assert env.client.get("/api/settings").json()["max_concurrent"] == 0


def test_api_settings_keeps_the_backend(env):
    env.client.put("/api/settings", json={"max_concurrent": 2})
    assert env.client.get("/api/settings").json()["backend"] == "vibe"
    assert env.settings.backend() == "vibe"


def test_api_run_over_the_limit_queues_the_card(env):
    env.client.put("/api/settings", json={"max_concurrent": 1})
    first = _create(env.client, "First")
    second = _create(env.client, "Second")

    assert env.client.post(f"/api/cards/{first['id']}/run").json()["status"] == "running"
    queued = env.client.post(f"/api/cards/{second['id']}/run")
    assert queued.status_code == 200, queued.text
    body = queued.json()
    assert body["status"] == "draft"
    assert body["session"]["queued"] is True
    assert body["session"]["queue_reason"] == "waiting for a free slot (1/1 running)"
    # The panel's occupancy line reads this.
    assert env.client.get("/api/settings").json()["running"] == 1

    env.client.post(f"/api/cards/{first['id']}/stop")
    deadline = time.time() + 10
    while time.time() < deadline:
        if env.client.get(f"/api/cards/{second['id']}").json()["status"] == "running":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the queued card never started after a slot freed")
    env.client.post(f"/api/cards/{second['id']}/stop")


# ---------------------------------------------------------------- front-end

node = shutil.which("node")
pytestmark_js = pytest.mark.skipif(node is None, reason="node is required to run the app.js queue check")


def _served_app_js(env) -> Path:
    r = env.client.get("/app.js")
    assert r.status_code == 200, r.text
    out = env.tmp_path / "app.js"
    out.write_text(r.text)
    return out


@pytestmark_js
def test_queue_chip_and_settings_panel(env):
    """The capacity queue is a normal wait (neutral chip, the server's reason)
    and the Settings panel renders the cap + occupancy and PUTs the change."""
    app_js = _served_app_js(env)
    proc = subprocess.run([node, str(QUEUE_CHIP_JS), str(app_js)],
                          capture_output=True, text=True, timeout=60)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"queue_chip_check.js did not print JSON (exit {proc.returncode})\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}")
    assert result["ok"], "queue chip / settings panel check failed: " + "; ".join(result["failures"])


def test_settings_rail_button_is_served(env):
    html = env.client.get("/").text
    assert 'data-panel="settings"' in html
    js = env.client.get("/app.js").text
    assert "renderSettingsPanel" in js
    assert "max_concurrent" in js
    # The banned literal stays banned.
    assert "deck" not in html.lower()
    assert "deck" not in js.lower()
