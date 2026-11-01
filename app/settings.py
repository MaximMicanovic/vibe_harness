"""Harness-wide settings the board can change: which backend runs sessions, how
many sessions may run at once, and how an accepted card ships.

Kept apart from `models.json` (per-role model choice) because it is a different
kind of decision: the backend is *which agent CLI* the harness drives, the model
is *which model* that CLI uses, the concurrency cap is *how many* of those
sessions may be live at the same time, and the accept mode is *where the change
lands* (a local merge, or a pull request).
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.backends import BACKENDS, DEFAULT_BACKEND, BackendError

# How accepting a card ships the change. `local` merges the card branch into
# the base branch in the project folder (the original behaviour); `pr` pushes
# the branch and opens a pull request whose body carries the review verdict.
ACCEPT_MODES: tuple[dict, ...] = (
    {
        "key": "local",
        "label": "Merge locally",
        "description": ("Accept merges the card branch into the base branch in "
                        "the project folder, then deletes the branch."),
    },
    {
        "key": "pr",
        "label": "Open a pull request",
        "description": ("Accept pushes the card branch to origin and opens a PR "
                        "whose body carries the review verdict; the branch and "
                        "worktree are kept for follow-up commits."),
    },
)
ACCEPT_MODE_KEYS = tuple(m["key"] for m in ACCEPT_MODES)
DEFAULT_ACCEPT_MODE = "local"


class SettingsError(Exception):
    """Unknown setting value (maps to HTTP 400)."""


def describe_accept_modes() -> list[dict]:
    """The accept modes the board offers, for the sidebar Settings panel."""
    return [dict(m) for m in ACCEPT_MODES]


class SettingsStore:
    """`{"backend": "vibe", "max_concurrent": 3, "accept_mode": "local"}` on
    disk, with the default for anything unset."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def _load(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return raw if isinstance(raw, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def backend(self) -> str:
        key = str(self._load().get("backend") or "").strip().lower()
        return key if key in BACKENDS else DEFAULT_BACKEND

    def set_backend(self, key: str) -> str:
        key = (key or "").strip().lower()
        if key not in BACKENDS:
            raise BackendError(
                f"unknown backend {key!r} (expected one of {', '.join(BACKENDS)})")
        with self._lock:
            data = self._load()
            data["backend"] = key
            self._save(data)
        return key

    def max_concurrent(self, default: int = 3) -> int:
        """Concurrent-session cap; `default` (Config.max_concurrent) when unset.

        0 means unlimited. A corrupt or negative stored value falls back to the
        default rather than capping the board at something nonsensical.
        """
        raw = self._load().get("max_concurrent")
        if raw is None:
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return default
        return value if value >= 0 else default

    def set_max_concurrent(self, value: int) -> int:
        """Persist the cap (0 = unlimited). Raises ValueError when negative."""
        value = int(value)
        if value < 0:
            raise ValueError("max_concurrent must be >= 0 (0 = unlimited)")
        with self._lock:
            data = self._load()
            data["max_concurrent"] = value
            self._save(data)
        return value

    def accept_mode(self, default: str = DEFAULT_ACCEPT_MODE) -> str:
        """The stored accept mode, else `default` (the config/env value)."""
        key = str(self._load().get("accept_mode") or "").strip().lower()
        return key if key in ACCEPT_MODE_KEYS else default

    def set_accept_mode(self, key: str) -> str:
        key = (key or "").strip().lower()
        if key not in ACCEPT_MODE_KEYS:
            raise SettingsError(
                f"unknown accept_mode {key!r} "
                f"(expected one of {', '.join(ACCEPT_MODE_KEYS)})")
        with self._lock:
            data = self._load()
            data["accept_mode"] = key
            self._save(data)
        return key
