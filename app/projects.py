"""Project registry: name -> folder path, persisted in projects.json.

A project is the dashboard's primary context: pick a project and the board shows
that project's cards; runs work in the project's folder (a card's own `workdir`
setting overrides it)..
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config import resolve_path


class ProjectError(Exception): ...
class ProjectNotFound(ProjectError): ...    # maps to HTTP 404
class ProjectExists(ProjectError): ...     # maps to HTTP 409


class ProjectStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")

    def list(self) -> list[dict]:
        data = self._load()
        return [{"name": name, "path": entry.get("path", "")}
                for name, entry in sorted(data.items())]

    def get(self, name: str) -> dict | None:
        entry = self._load().get(name)
        if entry is None:
            return None
        return {"name": name, "path": entry.get("path", "")}

    def create(self, name: str, path: str) -> dict:
        name = (name or "").strip()
        if not name:
            raise ProjectError("project name is required")
        raw = (path or "").strip()
        if not raw:
            raise ProjectError("project path is required")
        data = self._load()
        if name in data:
            raise ProjectExists(f"project {name!r} already exists")
        resolved = str(resolve_path(raw))
        Path(resolved).mkdir(parents=True, exist_ok=True)
        data[name] = {"path": resolved}
        self._save(data)
        return {"name": name, "path": resolved}

    def delete(self, name: str) -> None:
        data = self._load()
        if name not in data:
            raise ProjectNotFound(f"no project named {name!r}")
        del data[name]
        self._save(data)
