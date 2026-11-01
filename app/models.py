"""Per-role model selection for the harness's vibe sessions.

The harness runs three kinds of vibe session, and each can run on a different
model:

- **worker** (`run-N`) — implements the card and its fixer rounds;
- **review** (`review-N`) — reviews the diff and the test output;
- **resolver** (`resolve-N`) — resolves a conflicted merge when a card is
  accepted.

Vibe has no `--model` flag, but its config layers put `VIBE_*` environment
variables *above* the user's `config.toml`, so `VIBE_ACTIVE_MODEL=<alias>`
overrides the model for one spawned session. That is what the runner sets: no
global config is touched, and two sessions running at once cannot race each
other's model.

The selection is server state (`models.json`) so the board and the MCP tools see
the same choice, and so a restart keeps it.
"""
from __future__ import annotations

import json
import os
import threading
import tomllib
from pathlib import Path

from app.liveusage import vibe_home

# The roles the board offers. `session` is the runner's session kind, or None
# for a role that has no session yet (the UI shows it disabled with the reason).
ROLES: tuple[dict, ...] = (
    {
        "key": "worker",
        "label": "Worker",
        "session": "run",
        "description": "Implements the card and its fixer rounds (run-N).",
    },
    {
        "key": "review",
        "label": "Review",
        "session": "review",
        "description": "Reviews the diff and the test output (review-N).",
    },
    {
        "key": "resolver",
        "label": "Merge conflict",
        "session": "resolve",
        "description": "Resolves a conflicted merge when a card is accepted (resolve-N).",
    },
    {
        "key": "orchestrator",
        "label": "Orchestrator",
        "session": None,
        "description": ("No orchestrator session exists yet — the worker splits a "
                        "spec into sub-cards itself, through the MCP create_card tool."),
    },
)

ROLE_KEYS = tuple(r["key"] for r in ROLES)
SESSION_ROLES = {r["session"]: r["key"] for r in ROLES if r["session"]}


class ModelError(Exception):
    """Unknown role or model (maps to HTTP 400)."""


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class ModelSettings:
    """The per-role model choice, persisted as `{"models": {role: alias}}`."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        models = raw.get("models") if isinstance(raw, dict) else None
        if not isinstance(models, dict):
            return {}
        return {k: v for k, v in models.items()
                if k in ROLE_KEYS and isinstance(v, str) and v.strip()}

    def _save(self, models: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps({"models": models}, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # reads / writes
    # ------------------------------------------------------------------

    def all(self) -> dict[str, str]:
        with self._lock:
            return self._load()

    def get(self, role: str) -> str | None:
        if role not in ROLE_KEYS:
            raise ModelError(f"unknown role {role!r} (expected one of {', '.join(ROLE_KEYS)})")
        return self.all().get(role)

    def set(self, role: str, model: str | None) -> dict[str, str]:
        """Set (or clear, with None/"") one role's model. Returns the new map."""
        if role not in ROLE_KEYS:
            raise ModelError(f"unknown role {role!r} (expected one of {', '.join(ROLE_KEYS)})")
        if role == "orchestrator":
            raise ModelError("the orchestrator role has no session yet — nothing would use it")
        model = (model or "").strip()
        with self._lock:
            models = self._load()
            if model:
                models[role] = model
            else:
                models.pop(role, None)
            self._save(models)
            return models

    def update(self, patch: dict) -> dict[str, str]:
        """Apply {role: model} pairs; an empty value clears that role."""
        if not isinstance(patch, dict):
            raise ModelError("expected an object of {role: model}")
        unknown = [k for k in patch if k not in ROLE_KEYS]
        if unknown:
            raise ModelError(f"unknown role(s): {', '.join(sorted(unknown))}")
        with self._lock:
            models = self._load()
            for role, model in patch.items():
                if role == "orchestrator":
                    raise ModelError("the orchestrator role has no session yet — nothing would use it")
                value = (model or "").strip() if isinstance(model, str) else ""
                if value:
                    models[role] = value
                else:
                    models.pop(role, None)
            self._save(models)
            return models

    def model_for_session(self, kind: str) -> str | None:
        """The model to pin for a session kind ('run' | 'review' | 'resolve')."""
        role = SESSION_ROLES.get(kind)
        if role is None:
            return None
        return self.all().get(role)


# ----------------------------------------------------------------------
# which models can be picked
# ----------------------------------------------------------------------

def _config_models(home: Path) -> list[dict]:
    """Models declared in the user's config.toml ([[models]] entries)."""
    path = home / "config.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for entry in data.get("models") or []:
        if not isinstance(entry, dict):
            continue
        alias = entry.get("alias") or entry.get("name")
        if not isinstance(alias, str) or not alias:
            continue
        out.append({
            "alias": alias,
            "display_name": entry.get("display_name") or alias,
            "thinking": entry.get("thinking"),
            "source": "config",
        })
    return out


def _catalog_models(home: Path) -> list[dict]:
    """Models the model catalog (GrowthBook cache) adds to the picker."""
    path = home / "experiment_eval_cache.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for payload in (data.values() if isinstance(data, dict) else []):
        features = (payload or {}).get("payload", {}).get("features") or {}
        extra = features.get("vibe_cli_extra_models") or {}
        models = (extra.get("defaultValue") or {}).get("models") or []
        for entry in models:
            if not isinstance(entry, dict):
                continue
            alias = entry.get("alias") or entry.get("name")
            if not isinstance(alias, str) or not alias:
                continue
            out.append({
                "alias": alias,
                "display_name": entry.get("display_name") or alias,
                "thinking": entry.get("thinking"),
                "source": "catalog",
            })
    return out


def available_models(home: Path | None = None) -> list[dict]:
    """Every model alias the user can pick, deduped, config entries first.

    A config entry usually has no display_name (the alias is the name); the
    catalog entry for the same alias does, so the nicer name wins.
    """
    home = Path(home) if home is not None else vibe_home()
    seen: dict[str, dict] = {}
    for entry in _config_models(home) + _catalog_models(home):
        alias = entry["alias"]
        current = seen.get(alias)
        if current is None:
            seen[alias] = entry
            continue
        if current["display_name"] == alias and entry["display_name"] != alias:
            current["display_name"] = entry["display_name"]
        if not current.get("thinking") and entry.get("thinking"):
            current["thinking"] = entry["thinking"]
    # The user's own active_model is always pickable, even when it is not
    # declared as a [[models]] entry (it can come from the catalog alone).
    active = default_model(home)
    if active and active not in seen:
        seen[active] = {"alias": active, "display_name": active,
                        "thinking": None, "source": "active"}
    return sorted(seen.values(), key=lambda m: m["alias"])


def default_model(home: Path | None = None) -> str | None:
    """The user's own `active_model` — what a role with no override runs on."""
    home = Path(home) if home is not None else vibe_home()
    try:
        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("active_model")
    return value if isinstance(value, str) and value else None


def describe(settings: ModelSettings, home: Path | None = None) -> dict:
    """The whole picture the board needs: roles, pickable models, selection."""
    home = Path(home) if home is not None else vibe_home()
    selected = settings.all()
    fallback = default_model(home)
    roles = []
    for role in ROLES:
        chosen = selected.get(role["key"])
        roles.append({
            **role,
            "available": role["session"] is not None,
            "model": chosen or "",
            "effective": chosen or fallback or "",
            "inherited": not chosen,
        })
    return {
        "roles": roles,
        "models": available_models(home),
        "default_model": fallback,
        "updated_at": _now(),
    }
