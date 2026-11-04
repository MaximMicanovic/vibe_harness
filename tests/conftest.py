"""Shared test fixtures.

Safety: the suite must never open GUI dialogs on the developer's machine.
`app.fsdialog` opens a native folder picker via a per-OS backend binary
(macOS `osascript`, Linux `zenity`/`kdialog`, Windows `powershell`); the
autouse guard below makes any real picker invocation fail loudly, so a test
that forgets to mock the picker errors out instead of popping a dialog. All
other subprocess usage (e.g. the runner's fake `vibe` binary) passes through
untouched.

Loudness: when `node` or `ruff` is missing, the ~23 front-end checks and the
dead-code gate would SKIP silently. The hooks at the bottom print a LOUD
warning in that case, so a green run can never hide a skipped gate.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

#: Native folder-picker binaries (one per OS backend of app.fsdialog).
NATIVE_PICKER_TOOLS = ("osascript", "zenity", "kdialog", "powershell")


def _is_native_picker(cmd) -> bool:
    """True when `cmd` invokes one of the native folder-picker binaries."""
    if isinstance(cmd, str):
        stripped = cmd.strip()
        first = stripped.split(None, 1)[0] if stripped else ""
    else:
        try:
            first = str(cmd[0]) if cmd else ""
        except (TypeError, IndexError):
            return False
    base = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return any(tool in base for tool in NATIVE_PICKER_TOOLS)


@pytest.fixture(autouse=True)
def no_gui_dialogs(monkeypatch):
    real_run = subprocess.run
    real_popen = subprocess.Popen
    message = ("real native-picker dialogs (osascript/zenity/kdialog/powershell) "
               "are not allowed in tests — "
               "mock app.fsdialog (subprocess.run / choose_folder)")

    def guarded_run(cmd, *args, **kwargs):
        if _is_native_picker(cmd):
            raise RuntimeError(message)
        return real_run(cmd, *args, **kwargs)

    def guarded_popen(cmd, *args, **kwargs):
        if _is_native_picker(cmd):
            raise RuntimeError(message)
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)


# ------------------------------------------------- loud missing-tool warning

def _missing_tool_warnings() -> list[str]:
    """One line per optional tool whose absence makes gates skip silently."""
    missing: list[str] = []
    if shutil.which("node") is None:
        missing.append(
            "node is missing: the app.js front-end checks (tests/*_check.js, "
            "~23 of them) will SKIP silently")
    beside_python = Path(sys.executable).parent / "ruff"
    if shutil.which("ruff") is None and not beside_python.exists():
        missing.append(
            "ruff is missing: the dead-code gate (tests/test_no_dead_code.py) "
            "will SKIP silently")
    return missing


def pytest_sessionstart(session):
    for line in _missing_tool_warnings():
        warnings.warn(line, stacklevel=2)


def pytest_terminal_summary(terminalreporter):
    missing = _missing_tool_warnings()
    if missing:
        terminalreporter.section("LOUD WARNING: optional test tools missing")
        for line in missing:
            terminalreporter.write_line("  " + line, red=True)
