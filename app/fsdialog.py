"""Native folder picker for the dashboard (spec §8, §10).

Browsers cannot reveal the absolute path of a picked directory (the File System
Access API deliberately hides it), so the picker is a native macOS dialog: the
backend runs `osascript choose folder` and returns the POSIX path. This is a
local single-user tool — the server runs on the same machine as the browser, so
a native dialog is the correct UX. Typing a path by hand always stays possible.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

PROMPT = "Choose a project folder"
TIMEOUT_SECONDS = 120.0


class FolderPickerUnavailable(Exception):
    """No native picker on this platform (or osascript missing/failed)."""


class FolderPickerCancelled(Exception):
    """User dismissed the dialog."""


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("osascript") is not None


def choose_folder(prompt: str = PROMPT, timeout: float = TIMEOUT_SECONDS) -> str:
    """Open the native folder dialog and return the chosen absolute path."""
    if not available():
        raise FolderPickerUnavailable(
            "folder picker needs macOS with osascript; type the path instead")
    script = (
        'set chosenFolder to choose folder with prompt "'
        + prompt.replace('"', "")
        + '"\nreturn POSIX path of chosenFolder'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise FolderPickerUnavailable("folder picker timed out") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "User canceled" in err or "(-128)" in err:
            raise FolderPickerCancelled(err or "cancelled")
        raise FolderPickerUnavailable(err or "osascript failed")
    path = proc.stdout.strip()
    if not path:
        raise FolderPickerUnavailable("osascript returned no path")
    return path
