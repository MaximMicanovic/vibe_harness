"""Keep the machine awake while the harness has work to do.

A card run is a long-lived child process. If the machine sleeps, that process is
suspended, the agent CLI eventually gives up and reports `interrupted` (exit 3)
— which the board shows as a failed card even though nothing was wrong with the
work. Three cards dying within a minute of each other after an hour is the
signature of the OS pausing them, not of three independent failures.

While any session is live, or any card is queued for a slot / a merge / a
resolver, hold an idle-sleep inhibitor:

- macOS: ``caffeinate -i`` (prevent idle sleep; the display may still sleep).
- Linux: ``systemd-inhibit --what=idle:sleep`` when it is available.
- Windows: ``SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)`` via
  ctypes — an in-process API call, no child process; cleared with
  ``ES_CONTINUOUS`` on release.
- Anything else: no-op — there is no portable way to ask for this.

On macOS/Linux the inhibitor is a CHILD PROCESS of the server, so it dies with
it: a crashed or killed dashboard can never leave the machine permanently
awake. On Windows the call is per-thread state of this process, which dies with
it the same way. ``release()`` also runs on shutdown.
"""
from __future__ import annotations

import ctypes
import shutil
import subprocess
import sys
import threading
from typing import Callable

# How often the watchdog re-checks whether there is still work. Short enough
# that a card started right after the machine went idle still holds the
# inhibitor, long enough to be free.
POLL_SECONDS = 5.0


# Windows SetThreadExecutionState flags: ES_CONTINUOUS keeps the setting until
# changed, ES_SYSTEM_REQUIRED asks the OS not to sleep idle.
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_execution_state(flags: int) -> bool:
    """Windows only: SetThreadExecutionState via ctypes. False elsewhere."""
    try:
        return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))
    except (AttributeError, OSError, ValueError):
        return False


def windows_inhibitor() -> bool:
    """True when this platform's inhibitor is the in-process Windows API call
    (no subprocess — see inhibitor_command)."""
    return sys.platform == "win32"


def inhibitor_command() -> list[str] | None:
    """The command that holds an idle-sleep inhibitor as a child process, or
    None — on Windows the inhibitor is the in-process _set_execution_state
    call instead, and on unsupported platforms there is neither."""
    if sys.platform == "darwin":
        # -i: no idle sleep (what actually kills a session).
        # -s: no system sleep while on AC power — the strongest assertion a
        #     userspace process can hold. Closing the lid still sleeps the
        #     machine; only `sudo pmset -a disablesleep 1` overrides that, and
        #     it is a system-wide root setting this harness will not touch.
        return ["caffeinate", "-i", "-s"]
    if sys.platform.startswith("linux") and shutil.which("systemd-inhibit"):
        return ["systemd-inhibit", "--what=idle:sleep",
                "--why=vibe-harness is running cards", "sleep", "infinity"]
    return None


class SleepGuard:
    """Holds an idle-sleep inhibitor while `is_busy()` says there is work."""

    def __init__(self, enabled: bool = True, poll_seconds: float = POLL_SECONDS):
        self.enabled = bool(enabled)
        self.poll_seconds = poll_seconds
        self._proc: subprocess.Popen | None = None
        self._win_held = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------------------------------------------------------------- state

    def held(self) -> bool:
        """True while an inhibitor is actually held (process or Windows API)."""
        with self._lock:
            if windows_inhibitor():
                return self._win_held
            return self._proc is not None and self._proc.poll() is None

    def supported(self) -> bool:
        return inhibitor_command() is not None or windows_inhibitor()

    # ------------------------------------------------------------ lifecycle

    def acquire(self) -> bool:
        """Start the inhibitor. Returns True when one is held."""
        if not self.enabled:
            return False
        with self._lock:
            if windows_inhibitor():
                # In-process on Windows: no child, the call is per-thread state
                # of this process (cleared with ES_CONTINUOUS on release).
                if self._win_held:
                    return True
                self._win_held = _set_execution_state(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                return self._win_held
            cmd = inhibitor_command()
            if cmd is None:
                return False
            if self._proc is not None and self._proc.poll() is None:
                return True
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self._proc = None
                return False
            return True

    def release(self) -> None:
        """Stop the inhibitor (idempotent)."""
        with self._lock:
            proc, self._proc = self._proc, None
            win_held, self._win_held = self._win_held, False
        if win_held:
            _set_execution_state(ES_CONTINUOUS)
            return
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def sync(self, wanted: bool) -> None:
        """Hold the inhibitor when `wanted`, release it otherwise."""
        if wanted:
            self.acquire()
        else:
            self.release()

    def start(self, is_busy: Callable[[], bool]) -> None:
        """Poll `is_busy()` in a daemon thread and keep the inhibitor in sync."""
        if not self.enabled or self._thread is not None:
            return
        if not self.supported():
            return

        def loop() -> None:
            while not self._stop.wait(self.poll_seconds):
                try:
                    self.sync(bool(is_busy()))
                except Exception:
                    pass          # never let the watchdog die on a bad read
            self.release()

        self._thread = threading.Thread(target=loop, daemon=True, name="sleep-guard")
        self._thread.start()

    def stop(self) -> None:
        """Stop the watchdog and release the inhibitor."""
        self._stop.set()
        self.release()
