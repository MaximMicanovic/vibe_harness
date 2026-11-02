"""The sleep guard: keep the machine awake while cards run or wait.

A sleeping machine suspends the session process, the agent CLI gives up and
reports `interrupted` (exit 3) — a card that looks failed but was only paused.
These tests cover the guard itself and the predicate that drives it; the real
inhibitor process is stubbed so nothing here depends on the platform.
"""
from __future__ import annotations

import subprocess

import pytest

from app import sleepguard
from app.cards import CardStore
from app.config import Config
from app.projects import ProjectStore
from app.runner import SessionRunner
from app.sleepguard import SleepGuard


class FakeProc:
    """Stands in for the inhibitor process."""

    def __init__(self, cmd, **kwargs):
        self.cmd = cmd
        self.kwargs = kwargs
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


@pytest.fixture()
def fake_popen(monkeypatch):
    """Record every inhibitor process the guard spawns."""
    spawned: list[FakeProc] = []

    def _popen(cmd, **kwargs):
        proc = FakeProc(cmd, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(sleepguard.subprocess, "Popen", _popen)
    return spawned


# ------------------------------------------------------------------ platform

def test_inhibitor_command_on_macos(monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "darwin")
    assert sleepguard.inhibitor_command() == ["caffeinate", "-i", "-s"]


def test_inhibitor_command_on_linux(monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "linux")
    monkeypatch.setattr(sleepguard.shutil, "which", lambda name: "/usr/bin/" + name)
    cmd = sleepguard.inhibitor_command()
    assert cmd[:2] == ["systemd-inhibit", "--what=idle:sleep"]


def test_inhibitor_command_unsupported_platform(monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "win32")
    assert sleepguard.inhibitor_command() is None


# --------------------------------------------------------------------- guard

def test_acquire_starts_the_inhibitor(fake_popen, monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "darwin")
    guard = SleepGuard()
    assert guard.held() is False
    assert guard.acquire() is True
    assert guard.held() is True
    assert fake_popen[0].cmd == ["caffeinate", "-i", "-s"]


def test_acquire_is_idempotent(fake_popen, monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "darwin")
    guard = SleepGuard()
    guard.acquire()
    guard.acquire()
    assert len(fake_popen) == 1          # one inhibitor, not two


def test_release_terminates_the_inhibitor(fake_popen, monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "darwin")
    guard = SleepGuard()
    guard.acquire()
    guard.release()
    assert fake_popen[0].terminated is True
    assert guard.held() is False


def test_release_is_idempotent(fake_popen, monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "darwin")
    guard = SleepGuard()
    guard.release()                      # nothing held: must not raise
    guard.acquire()
    guard.release()
    guard.release()
    assert guard.held() is False


def test_sync_toggles(fake_popen, monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "darwin")
    guard = SleepGuard()
    guard.sync(True)
    assert guard.held() is True
    guard.sync(False)
    assert guard.held() is False
    guard.sync(True)
    assert guard.held() is True
    assert len(fake_popen) == 2          # a fresh inhibitor after the release


def test_disabled_guard_never_spawns(fake_popen, monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "darwin")
    guard = SleepGuard(enabled=False)
    assert guard.acquire() is False
    assert guard.held() is False
    assert fake_popen == []


def test_unsupported_platform_never_spawns(fake_popen, monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "win32")
    guard = SleepGuard()
    assert guard.supported() is False
    assert guard.acquire() is False
    assert fake_popen == []


def test_spawn_failure_is_not_fatal(monkeypatch):
    monkeypatch.setattr(sleepguard.sys, "platform", "darwin")

    def _boom(cmd, **kwargs):
        raise OSError("caffeinate is not installed")

    monkeypatch.setattr(sleepguard.subprocess, "Popen", _boom)
    guard = SleepGuard()
    assert guard.acquire() is False
    assert guard.held() is False


# ------------------------------------------------------- what counts as work

@pytest.fixture()
def runner(tmp_path):
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin="vibe")
    return SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                         projects=ProjectStore(tmp_path / "projects.json"))


def test_idle_runner_has_no_pending_work(runner):
    runner.store.create("Idle", "spec")
    assert runner.has_pending_work() is False


def test_queued_card_is_pending_work(runner):
    card = runner.store.create("Queued", "spec")
    runner.store.patch_session(card["id"], {"queued": True})
    assert runner.has_pending_work() is True


def test_accept_queued_card_is_pending_work(runner):
    card = runner.store.create("Merging", "spec")
    runner.store.patch_session(card["id"], {"accept_queued": True})
    assert runner.has_pending_work() is True


def test_resolving_card_is_pending_work(runner):
    card = runner.store.create("Resolving", "spec")
    runner.store.patch_session(card["id"], {"git": {"resolving": True}})
    assert runner.has_pending_work() is True


def test_dependency_wait_is_not_pending_work(runner):
    """A card waiting on an unmerged dependency is not work: nothing happens
    until the user merges it, so it must not keep the machine awake."""
    card = runner.store.create("Waiting", "spec")
    runner.store.patch_session(card["id"], {"waiting_for": ["abc123"]})
    assert runner.has_pending_work() is False


def test_live_session_is_pending_work(runner):
    card = runner.store.create("Live", "spec")
    runner._registry[card["id"]] = {"proc": FakeProc(["vibe"]), "stop_requested": False}
    assert runner.has_pending_work() is True


def test_guard_is_disabled_by_config(tmp_path):
    store = CardStore(tmp_path / "cards")
    runner = SessionRunner(store, Config(prevent_sleep=False),
                           sessions_dir=tmp_path / "sessions",
                           projects=ProjectStore(tmp_path / "projects.json"))
    assert runner.sleep_guard.enabled is False
