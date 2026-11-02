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
- Anything else: no-op — there is no portable way to ask for this.

The inhibitor is a CHILD PROCESS of the server, so it dies with it: a crashed or
killed dashboard can never leave the machine permanently awake. ``release()``
also runs on shutdown.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from typing import Callable

# How often the watchdog re-checks whether there is still work. Short enough
# that a card started right after the machine went idle still holds the
# inhibitor, long enough to be free.
POLL_SECONDS = 5.0


def inhibitor_command() -> list[str] | None:
    """The command that holds an idle-sleep inhibitor on this platform."""
    if sys.platform == "darwin":
        return ["caffeinate", "-i"]
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
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------------------------------------------------------------- state

    def held(self) -> bool:
        """True while an inhibitor process is actually running."""
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def supported(self) -> bool:
        return inhibitor_command() is not None

    # ------------------------------------------------------------ lifecycle

    def acquire(self) -> bool:
        """Start the inhibitor. Returns True when one is running."""
        if not self.enabled:
            return False
        cmd = inhibitor_command()
        if cmd is None:
            return False
        with self._lock:
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
        if inhibitor_command() is None:
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
