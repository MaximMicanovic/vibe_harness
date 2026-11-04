"""Per-OS branches: process stop, detach flags, pid liveness, restart relaunch.

vibe-harness runs on macOS, Linux and Windows. Every test here mocks
sys.platform (win32/linux/darwin) and the subprocess/ctypes boundary, so no
real signal, taskkill, lsof/netstat call or dialog ever runs in the suite.
"""
from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from app import main
from app.config import Config
from app.runner import supervise
from app.runner.supervise import SuperviseMixin


class FakeProc:
    """A session process handle: terminate/kill map to TerminateProcess."""

    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.returncode = None
        self.calls: list[str] = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")
        self.returncode = 1

    def kill(self):
        self.calls.append("kill")
        self.returncode = -9


def _recorded_run(runs):
    def _run(cmd, **kwargs):
        runs.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    return _run


# ------------------------------------------------------------- detach flags

def test_detach_kwargs_posix_uses_start_new_session(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert supervise._detach_kwargs() == {"start_new_session": True}
    monkeypatch.setattr(sys, "platform", "linux")
    assert supervise._detach_kwargs() == {"start_new_session": True}


def test_detach_kwargs_windows_uses_creationflags(monkeypatch):
    """start_new_session is POSIX-only: on Windows pass CREATE_NEW_PROCESS_GROUP."""
    monkeypatch.setattr(sys, "platform", "win32")
    kwargs = supervise._detach_kwargs()
    assert "start_new_session" not in kwargs
    assert kwargs["creationflags"] & 0x00000200      # CREATE_NEW_PROCESS_GROUP


# ------------------------------------------------------------ stopping a tree

def test_terminate_session_posix_signals_then_kills(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    seen: list[tuple[int, int]] = []
    monkeypatch.setattr(SuperviseMixin, "_signal_session",
                        staticmethod(lambda pid, sig: seen.append((pid, sig))))
    proc = FakeProc()
    SuperviseMixin._terminate_session(proc, force=False)
    SuperviseMixin._terminate_session(proc, force=True)
    assert seen == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]


def test_terminate_session_windows_taskkills_the_tree(monkeypatch):
    """No killpg/SIGKILL on Windows: terminate()/kill() (TerminateProcess) on
    the child plus `taskkill /PID <pid> /T /F` for the detached tree."""
    monkeypatch.setattr(sys, "platform", "win32")
    runs: list = []
    monkeypatch.setattr(supervise.subprocess, "run", _recorded_run(runs))
    proc = FakeProc()
    SuperviseMixin._terminate_session(proc, force=False)
    assert proc.calls == ["terminate"]
    assert runs == [["taskkill", "/PID", "4321", "/T", "/F"]]
    SuperviseMixin._terminate_session(proc, force=True)
    assert proc.calls == ["terminate", "kill"]
    assert runs[-1] == ["taskkill", "/PID", "4321", "/T", "/F"]


def test_terminate_session_windows_adopted_pid_uses_taskkill_only(monkeypatch, tmp_path):
    """A re-adopted session is a pid shim with no Popen handle: taskkill /T /F
    is the only way to stop its tree."""
    monkeypatch.setattr(sys, "platform", "win32")
    runs: list = []
    monkeypatch.setattr(supervise.subprocess, "run", _recorded_run(runs))
    adopted = supervise._AdoptedProc(999, tmp_path / "export.json")
    SuperviseMixin._terminate_session(adopted, force=True)
    assert runs == [["taskkill", "/PID", "999", "/T", "/F"]]


def test_stop_on_windows_taskkills_the_live_session(monkeypatch, tmp_path):
    from app.cards import CardStore
    from app.projects import ProjectStore
    from app.runner import SessionRunner

    monkeypatch.setattr(sys, "platform", "win32")
    runs: list = []
    monkeypatch.setattr(supervise.subprocess, "run", _recorded_run(runs))
    store = CardStore(tmp_path / "cards")
    runner = SessionRunner(store, Config(vibe_bin="vibe"),
                           sessions_dir=tmp_path / "sessions",
                           projects=ProjectStore(tmp_path / "projects.json"))
    card = store.create("Win stop", "spec")
    proc = FakeProc()
    with runner._lock:
        runner._registry[card["id"]] = {"proc": proc, "stop_requested": False,
                                        "kind": "run", "round": 1}
    runner.stop(card["id"])
    assert ["taskkill", "/PID", str(proc.pid), "/T", "/F"] in runs
    assert runner._registry[card["id"]]["stop_requested"] is True


# -------------------------------------------------------------- pid liveness

def test_pid_alive_posix_uses_os_kill_zero(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(supervise.os, "kill", lambda pid, sig: calls.append((pid, sig)))
    assert SuperviseMixin._pid_alive(123) is True
    assert calls == [(123, 0)]


def test_pid_alive_windows_never_calls_os_kill(monkeypatch):
    """os.kill(pid, 0) on Windows calls TerminateProcess — it would KILL the
    session instead of probing it. The Windows branch must not touch os.kill."""
    monkeypatch.setattr(sys, "platform", "win32")

    def _boom(pid, sig):
        raise AssertionError("os.kill must not run on win32")

    monkeypatch.setattr(supervise.os, "kill", _boom)
    monkeypatch.setattr(supervise, "_windows_pid_alive", lambda pid: True)
    assert SuperviseMixin._pid_alive(123) is True


def test_windows_pid_alive_helper_never_raises():
    # Off Windows there is no ctypes.windll: the helper reports "not alive"
    # instead of raising. On Windows it would query the real process.
    assert isinstance(supervise._windows_pid_alive(1), bool)


# ------------------------------------------------------------ restart relaunch

def test_relaunch_argv_posix_keeps_the_sh_script(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    argv = main._relaunch_argv(Config(vibe_bin="vibe", port=8765), Path("/repo"), {1, 2})
    assert argv[0] == "sh" and argv[1] == "-c"
    assert "uvicorn" in argv[2] and "lsof" in argv[2] and "8765" in argv[2]


def test_relaunch_argv_windows_spawns_python_without_sh(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    argv = main._relaunch_argv(Config(vibe_bin="vibe", port=8765), Path("/repo"), {4321})
    assert argv[0] == sys.executable and argv[1] == "-c"
    assert "sh" not in argv[:2]
    script = argv[2]
    assert "taskkill" in script and "4321" in script
    assert "uvicorn" in script and "8765" in script


def test_port_listener_pids_windows_parses_netstat(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    netstat_out = (
        "  TCP    0.0.0.0:8765           0.0.0.0:0              LISTENING       4321\n"
        "  TCP    127.0.0.1:9999         0.0.0.0:0              LISTENING       777\n"
        "  TCP    [::]:8765              [::]:0                 LISTENING       4321\n"
    )
    monkeypatch.setattr(main.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            cmd, 0, stdout=netstat_out, stderr=""))
    assert main._port_listener_pids(8765) == {4321}


def test_port_listener_pids_posix_uses_lsof(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    seen: list = []
    monkeypatch.setattr(main.subprocess, "run",
                        lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(
                            cmd, 0, stdout="111\n222\n", stderr=""))
    assert main._port_listener_pids(8765) == {111, 222}
    assert seen == [["lsof", "-ti", "tcp:8765"]]
