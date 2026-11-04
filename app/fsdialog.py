"""Native folder picker for the dashboard (spec §8, §10).

Browsers cannot reveal the absolute path of a picked directory (the File System
Access API deliberately hides it), so the picker is a native dialog run by the
server — the server and the browser are the same machine for this local tool.
One backend per OS:

- macOS: ``osascript choose folder`` (AppleScript), returns the POSIX path.
- Linux: ``zenity --file-selection --directory``, else ``kdialog
  --getexistingdirectory`` — whichever desktop tool is installed.
- Windows: PowerShell hosting a ``System.Windows.Forms.FolderBrowserDialog``.

``available()`` is True only when this OS's backend binary exists. Typing a
path by hand always stays possible and is the fallback when no native picker
exists on the host.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

PROMPT = "Choose a project folder"
TIMEOUT_SECONDS = 120.0


class FolderPickerUnavailable(Exception):
    """No native picker on this platform (or the backend missing/failed)."""


class FolderPickerCancelled(Exception):
    """User dismissed the dialog."""


def _macos_command(prompt: str) -> list[str] | None:
    if shutil.which("osascript") is None:
        return None
    script = (
        'set chosenFolder to choose folder with prompt "'
        + prompt.replace('"', "")
        + '"\nreturn POSIX path of chosenFolder'
    )
    return ["osascript", "-e", script]


def _linux_command(prompt: str) -> list[str] | None:
    if shutil.which("zenity") is not None:
        return ["zenity", "--file-selection", "--directory", f"--title={prompt}"]
    if shutil.which("kdialog") is not None:
        return ["kdialog", "--getexistingdirectory", ".", f"--title={prompt}"]
    return None


def _windows_command(prompt: str) -> list[str] | None:
    if shutil.which("powershell") is None:
        return None
    # Single-quoted PowerShell string: escape embedded single quotes.
    description = prompt.replace("'", "''")
    script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
        f"$d.Description = '{description}';"
        "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) "
        "{ $d.SelectedPath } else { exit 1 }"
    )
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", script]


def picker_command(prompt: str = PROMPT) -> list[str] | None:
    """The native picker command for this platform, or None when there is none."""
    if sys.platform == "darwin":
        return _macos_command(prompt)
    if sys.platform.startswith("linux"):
        return _linux_command(prompt)
    if sys.platform == "win32":
        return _windows_command(prompt)
    return None


def available() -> bool:
    return picker_command() is not None


def _is_cancel(returncode: int, stderr: str) -> bool:
    # osascript reports a dismissal on stderr; zenity/kdialog and the
    # PowerShell wrapper exit 1 with no diagnostic when the user cancels.
    if "User canceled" in stderr or "(-128)" in stderr:
        return True
    return returncode == 1 and not stderr


def choose_folder(prompt: str = PROMPT, timeout: float = TIMEOUT_SECONDS) -> str:
    """Open the native folder dialog and return the chosen absolute path."""
    cmd = picker_command(prompt)
    if cmd is None:
        raise FolderPickerUnavailable(
            "no native folder picker on this platform; type the path instead")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise FolderPickerUnavailable("folder picker timed out") from exc
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if _is_cancel(proc.returncode, err):
            raise FolderPickerCancelled(err or "cancelled")
        raise FolderPickerUnavailable(err or f"{cmd[0]} failed")
    path = proc.stdout.strip()
    if not path:
        raise FolderPickerUnavailable(f"{cmd[0]} returned no path")
    return path
