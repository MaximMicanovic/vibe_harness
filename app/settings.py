"""Harness-wide settings the board can change: which backend runs sessions.

Kept apart from `models.json` (per-role model choice) because it is a different
kind of decision: the backend is *which agent CLI* the harness drives, the model
is *which model* that CLI uses.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from app.backends import BACKENDS, DEFAULT_BACKEND, BackendError


class SettingsStore:
    """`{"backend": "vibe"}` on disk, with the default for anything unset."""

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
