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


def test_unavailable_off_macos(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(fsdialog.FolderPickerUnavailable):
        fsdialog.choose_folder()


def test_unavailable_without_osascript(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(fsdialog.shutil, "which", lambda name: None)
    with pytest.raises(fsdialog.FolderPickerUnavailable):
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
