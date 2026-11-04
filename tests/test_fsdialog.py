"""Unit tests for the native folder picker (app/fsdialog.py). osascript is mocked."""
from __future__ import annotations

import subprocess
import sys

import pytest

from app import fsdialog


@pytest.fixture()
def darwin(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: "/usr/bin/osascript")


def test_unavailable_on_unknown_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd13")
    assert fsdialog.available() is False
    with pytest.raises(fsdialog.FolderPickerUnavailable):
        fsdialog.choose_folder()


def test_unavailable_without_osascript(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: None)
    assert fsdialog.available() is False
    with pytest.raises(fsdialog.FolderPickerUnavailable):
        fsdialog.choose_folder()


# ------------------------------------------------------------- linux picker

@pytest.fixture()
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")


def test_linux_prefers_zenity(linux, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which",
                        lambda name: "/usr/bin/" + name if name in ("zenity", "kdialog") else None)
    cmd = fsdialog.picker_command()
    assert cmd[0] == "zenity"
    assert "--file-selection" in cmd and "--directory" in cmd
    assert fsdialog.available() is True


def test_linux_falls_back_to_kdialog(linux, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which",
                        lambda name: "/usr/bin/kdialog" if name == "kdialog" else None)
    cmd = fsdialog.picker_command()
    assert cmd[0] == "kdialog"
    assert "--getexistingdirectory" in cmd
    assert fsdialog.available() is True


def test_linux_without_a_tool_is_unavailable(linux, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: None)
    assert fsdialog.available() is False
    with pytest.raises(fsdialog.FolderPickerUnavailable):
        fsdialog.choose_folder()


def test_linux_zenity_returns_the_path(linux, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: "/usr/bin/zenity")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "zenity"
        return subprocess.CompletedProcess(cmd, 0, stdout="/home/max/code\n", stderr="")
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    assert fsdialog.choose_folder() == "/home/max/code"


def test_linux_zenity_cancel_is_rc1_without_stderr(linux, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: "/usr/bin/zenity")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    with pytest.raises(fsdialog.FolderPickerCancelled):
        fsdialog.choose_folder()


# ------------------------------------------------------------ windows picker

@pytest.fixture()
def win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


def test_windows_uses_powershell_folder_browser(win32, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which",
                        lambda name: "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
                        if name == "powershell" else None)
    cmd = fsdialog.picker_command()
    assert cmd[0] == "powershell"
    assert any("FolderBrowserDialog" in part for part in cmd)
    assert fsdialog.available() is True


def test_windows_without_powershell_is_unavailable(win32, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: None)
    assert fsdialog.available() is False
    with pytest.raises(fsdialog.FolderPickerUnavailable):
        fsdialog.choose_folder()


def test_windows_returns_the_path(win32, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: "powershell.exe")

    def fake_run(cmd, **kwargs):
        assert cmd[0] == "powershell"
        return subprocess.CompletedProcess(cmd, 0, stdout="C:\\Users\\max\\code\r\n", stderr="")
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    assert fsdialog.choose_folder() == "C:\\Users\\max\\code"


def test_windows_cancel_is_rc1_without_stderr(win32, monkeypatch):
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: "powershell.exe")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    with pytest.raises(fsdialog.FolderPickerCancelled):
        fsdialog.choose_folder()


def test_returns_posix_path(darwin, monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[0] == "osascript"
        assert kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout="/Users/maxim/my-app\n", stderr="")
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    assert fsdialog.choose_folder() == "/Users/maxim/my-app"


def test_cancelled_dialog(darwin, monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 1, stdout="", stderr="0:0: execution error: User canceled. (-128)")
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    with pytest.raises(fsdialog.FolderPickerCancelled):
        fsdialog.choose_folder()


def test_osascript_failure(darwin, monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="some osascript error")
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    with pytest.raises(fsdialog.FolderPickerUnavailable):
        fsdialog.choose_folder()


def test_timeout(darwin, monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    with pytest.raises(fsdialog.FolderPickerUnavailable):
        fsdialog.choose_folder()


def test_empty_output(darwin, monkeypatch):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr="")
    monkeypatch.setattr(fsdialog.subprocess, "run", fake_run)
    with pytest.raises(fsdialog.FolderPickerUnavailable):
        fsdialog.choose_folder()


def test_real_picker_never_runs_in_tests(darwin):
    """Canary for the conftest `no_gui_dialogs` guard: an unmocked picker call
    must raise instead of opening a real dialog on the developer's machine."""
    with pytest.raises(RuntimeError, match="not allowed in tests"):
        fsdialog.choose_folder()


@pytest.mark.parametrize("platform,tool", [
    ("linux", "zenity"),
    ("linux", "kdialog"),
    ("win32", "powershell"),
])
def test_real_native_picker_never_runs_in_tests(monkeypatch, platform, tool):
    """The guard bans EVERY native-picker binary, not just osascript: an
    unmocked call on any platform must raise instead of popping a dialog."""
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: "/usr/bin/" + name)
    with pytest.raises(RuntimeError, match="not allowed in tests"):
        fsdialog.choose_folder()
