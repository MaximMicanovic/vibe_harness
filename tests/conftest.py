"""Shared test fixtures.

Safety: the suite must never open GUI dialogs on the developer's machine.
`app.fsdialog` opens a native folder picker via `osascript`; the autouse guard
below makes any real `osascript` invocation fail loudly, so a test that forgets
to mock the picker errors out instead of popping a dialog. All other subprocess
usage (e.g. the runner's fake `vibe` binary) passes through untouched.
"""
from __future__ import annotations

import subprocess

import pytest


def _is_osascript(cmd) -> bool:
    if isinstance(cmd, str):
        return cmd.strip().startswith("osascript")
    try:
        return bool(cmd) and "osascript" in str(cmd[0])
    except (TypeError, IndexError):
        return False


@pytest.fixture(autouse=True)
def no_gui_dialogs(monkeypatch):
    real_run = subprocess.run
    real_popen = subprocess.Popen
    message = ("real osascript dialogs are not allowed in tests — "
               "mock app.fsdialog (subprocess.run / choose_folder)")

    def guarded_run(cmd, *args, **kwargs):
        if _is_osascript(cmd):
            raise RuntimeError(message)
        return real_run(cmd, *args, **kwargs)

    def guarded_popen(cmd, *args, **kwargs):
        if _is_osascript(cmd):
            raise RuntimeError(message)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
