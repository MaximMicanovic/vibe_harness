"""Tests for the safety/loudness guards in tests/conftest.py itself.

The autouse `no_gui_dialogs` fixture must ban every native-picker binary (not
just osascript), and the missing-node/ruff warning must be loud — a green run
must never hide a silently skipped front-end check or dead-code gate.
"""
from __future__ import annotations

import subprocess

import pytest

import conftest


@pytest.mark.parametrize("tool", conftest.NATIVE_PICKER_TOOLS)
def test_guard_bans_every_native_picker_binary(tool):
    """subprocess.run/Popen with any picker binary raises instead of popping
    a real dialog on the developer's machine."""
    with pytest.raises(RuntimeError, match="not allowed in tests"):
        subprocess.run([tool, "--version"])
    with pytest.raises(RuntimeError, match="not allowed in tests"):
        subprocess.Popen([f"/usr/bin/{tool}", "--version"])


def test_guard_lets_other_subprocesses_through():
    # git and friends are not picker binaries: the guard must not touch them.
    proc = subprocess.run(["git", "--version"], capture_output=True, text=True)
    assert proc.returncode == 0


def test_missing_tool_warnings_names_node_and_ruff(monkeypatch):
    monkeypatch.setattr(conftest.shutil, "which", lambda name: None)
    monkeypatch.setattr(conftest.Path, "exists", lambda self: False)
    warnings = conftest._missing_tool_warnings()
    assert any("node" in w and "SKIP" in w for w in warnings)
    assert any("ruff" in w and "dead-code" in w for w in warnings)


def test_missing_tool_warnings_empty_when_tools_exist(monkeypatch):
    monkeypatch.setattr(conftest.shutil, "which", lambda name: "/usr/bin/" + name)
    assert conftest._missing_tool_warnings() == []


def test_terminal_summary_is_loud_when_tools_are_missing(monkeypatch):
    monkeypatch.setattr(conftest, "_missing_tool_warnings",
                        lambda: ["node is missing: checks will SKIP"])
    sections, lines = [], []

    class FakeReporter:
        def section(self, title):
            sections.append(title)

        def write_line(self, text, **kwargs):
            lines.append((text, kwargs))

    conftest.pytest_terminal_summary(FakeReporter())
    assert any("LOUD WARNING" in s for s in sections)
    assert any("node is missing" in t for t, _ in lines)
    assert all(kw.get("red") for _, kw in lines), "the warning must be red/loud"


def test_terminal_summary_silent_when_nothing_is_missing(monkeypatch):
    monkeypatch.setattr(conftest, "_missing_tool_warnings", lambda: [])
    sections = []

    class FakeReporter:
        def section(self, title):
            sections.append(title)

        def write_line(self, text, **kwargs):
            raise AssertionError("nothing should be written")

    conftest.pytest_terminal_summary(FakeReporter())
    assert sections == []
