"""Harness-wide settings the board can change: which backend runs sessions and
how many sessions may run at once.

Kept apart from `models.json` (per-role model choice) because it is a different
kind of decision: the backend is *which agent CLI* the harness drives, the model
is *which model* that CLI uses, and the concurrency cap is *how many* of those
sessions may be live at the same time.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.backends import BACKENDS, DEFAULT_BACKEND, BackendError


class SettingsStore:
    """`{"backend": "vibe", "max_concurrent": 3}` on disk, with the default for
    anything unset."""

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
