"""Per-role model selection for the harness's sessions.

The harness runs three kinds of session, and each can run on a different model:

- **worker** (`run-N`) — implements the card and its fixer rounds;
- **review** (`review-N`) — reviews the diff and the test output;
- **resolver** (`resolve-N`) — resolves a conflicted merge when a card is
  accepted.

Choosing a model IS choosing the platform: the model list carries both backends
(Vibe's aliases from the user's config, plus Claude's `sonnet` / `opus` /
`haiku`), and picking one sets the backend for that role. There is no separate
global backend control to contradict it.

Vibe has no `--model` flag, but its config layers put `VIBE_*` environment
variables *above* the user's `config.toml`, so `VIBE_ACTIVE_MODEL=<alias>`
overrides the model for one spawned session. That is what the runner sets: no
global config is touched, and two sessions running at once cannot race each
other's model. Claude takes `--model` on the command line instead.

The selection is server state (`models.json`) so the board and the MCP tools see
the same choice, and so a restart keeps it.
"""
from __future__ import annotations

import json
import os
import threading
import tomllib
from pathlib import Path

from app.backends import BACKENDS, DEFAULT_BACKEND, describe_backends
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

#: The Claude Code aliases the picker offers. Claude also accepts a full model
#: name typed by hand (`claude-sonnet-4-6`), which is why the picker has an
#: "other…" entry.
CLAUDE_MODELS: tuple[dict, ...] = (
    {"alias": "sonnet", "label": "Claude Sonnet"},
    {"alias": "opus", "label": "Claude Opus"},
    {"alias": "haiku", "label": "Claude Haiku"},
)
CLAUDE_ALIASES = tuple(m["alias"] for m in CLAUDE_MODELS)


class ModelError(Exception):
    """Unknown role or model, or a backend that cannot run it (maps to HTTP 400)."""


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def backend_for_model(model: str) -> str:
    """The backend that runs `model` when the caller does not say.

    The Claude aliases — and a full `claude-…` id typed into the picker's
    "other…" entry — belong to Claude; everything else is a Vibe alias.
    """
    alias = (model or "").strip().lower()
    if alias in CLAUDE_ALIASES or alias.startswith("claude"):
        return "claude"
    return DEFAULT_BACKEND


def _choice(value) -> dict | None:
    """One stored entry in the current shape, migrating the flat one.

    A flat `{role: alias}` file predates the backend being part of the choice;
    every entry in it was written for Vibe, so it reads as
    `{"model": alias, "backend": "vibe"}`.
    """
    if isinstance(value, str):
        model = value.strip()
        return {"model": model, "backend": DEFAULT_BACKEND} if model else None
    if isinstance(value, dict):
        model = str(value.get("model") or "").strip()
        if not model:
            return None
        backend = str(value.get("backend") or "").strip().lower()
        if backend not in BACKENDS:
            backend = backend_for_model(model)
        return {"model": model, "backend": backend}
    return None


class ModelSettings:
    """The per-role model choice, persisted as
    `{"models": {role: {"model": alias, "backend": "vibe"|"claude"}}}`.

    A flat `{role: alias}` file still reads (see `_choice`) and is rewritten in
    the new shape on the next save.
    """

    def __init__(self, path: Path, home: Path | None = None):
        self.path = Path(path)
        # The Vibe home the model catalog is read from; None = the real one.
        self.home = Path(home) if home is not None else None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _home(self) -> Path:
        return self.home if self.home is not None else vibe_home()

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        models = raw.get("models") if isinstance(raw, dict) else None
        if not isinstance(models, dict):
            return {}
        out = {}
        for role, value in models.items():
            if role not in ROLE_KEYS:
                continue
            choice = _choice(value)
            if choice:
                out[role] = choice
        return out

    def _save(self, models: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps({"models": models}, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # reads / writes
    # ------------------------------------------------------------------

    def all(self) -> dict[str, dict]:
        with self._lock:
            return self._load()

    def get(self, role: str) -> str | None:
        """The model chosen for `role`, or None when it inherits."""
        choice = self.choice(role)
        return choice["model"] if choice else None

    def choice(self, role: str) -> dict | None:
        """The `{model, backend}` chosen for `role`, or None when it inherits."""
        if role not in ROLE_KEYS:
            raise ModelError(f"unknown role {role!r} (expected one of {', '.join(ROLE_KEYS)})")
        return self.all().get(role)

    def set(self, role: str, model: str | None,
            backend: str | None = None) -> dict[str, dict]:
        """Set (or clear, with None/"") one role's model. Returns the new map.

        `backend` is the platform that runs the model; omitted, it is inferred
        from the model. A backend that does not offer the model is refused —
        that is the combination the old global Backend dropdown allowed.
        """
        if role not in ROLE_KEYS:
            raise ModelError(f"unknown role {role!r} (expected one of {', '.join(ROLE_KEYS)})")
        if role == "orchestrator":
            raise ModelError("the orchestrator role has no session yet — nothing would use it")
        model = (model or "").strip()
        with self._lock:
            models = self._load()
            if model:
                models[role] = self._entry(model, backend)
            else:
                models.pop(role, None)
            self._save(models)
            return models

    def update(self, patch: dict) -> dict[str, dict]:
        """Apply {role: model} or {role: {model, backend}} pairs; "" clears."""
        if not isinstance(patch, dict):
            raise ModelError("expected an object of {role: model}")
        unknown = [k for k in patch if k not in ROLE_KEYS]
        if unknown:
            raise ModelError(f"unknown role(s): {', '.join(sorted(unknown))}")
        with self._lock:
            models = self._load()
            for role, value in patch.items():
                if role == "orchestrator":
                    raise ModelError("the orchestrator role has no session yet — nothing would use it")
                if isinstance(value, dict):
                    model = str(value.get("model") or "").strip()
                    backend = value.get("backend")
                else:
                    model = (value or "").strip() if isinstance(value, str) else ""
                    backend = None
                if model:
                    models[role] = self._entry(model, backend)
                else:
                    models.pop(role, None)
            self._save(models)
            return models

    def _entry(self, model: str, backend: str | None) -> dict:
        """The stored entry for one choice, refusing a backend that cannot run it."""
        if backend is None or not str(backend).strip():
            return {"model": model, "backend": backend_for_model(model)}
        key = str(backend).strip().lower()
        if key not in BACKENDS:
            raise ModelError(
                f"unknown backend {key!r} (expected one of {', '.join(BACKENDS)})")
        if not self.offers(key, model):
            raise ModelError(f"the {key} backend does not offer the model {model!r}")
        return {"model": model, "backend": key}

    def offers(self, backend: str, model: str) -> bool:
        """Whether `backend` can run `model`.

        Vibe offers exactly the aliases its config declares. Claude offers its
        three aliases plus any full `claude-…` name typed by hand, but never a
        model that belongs to the other platform — that is the mismatch the old
        global Backend dropdown allowed. The check cannot lean on the local Vibe
        config alone: a Vibe model is not Claude's even on a machine whose
        config does not declare it.
        """
        model = (model or "").strip()
        if not model:
            return False
        if backend == "claude":
            if model in CLAUDE_ALIASES:
                return True
            vibe_aliases = {m["alias"] for m in available_models(self._home())}
            return model not in vibe_aliases and backend_for_model(model) == "claude"
        if backend == "vibe":
            return model in {m["alias"] for m in available_models(self._home())}
        return False

    # ------------------------------------------------------------------
    # resolution
    # ------------------------------------------------------------------

    def resolve(self, role: str, selected: dict | None = None) -> dict:
        """The model + backend a role actually runs on.

        A role with its own choice uses it; "inherit" follows the worker's
        choice, backend included; when the worker inherits too, the fallback is
        Vibe with the CLI's own default model (no override at all). `selected`
        is an already-loaded map, so describing every role reads the file once.
        """
        if role not in ROLE_KEYS:
            raise ModelError(f"unknown role {role!r} (expected one of {', '.join(ROLE_KEYS)})")
        selected = self.all() if selected is None else selected
        own = selected.get(role)
        if own:
            return {**own, "inherited": False, "source": role}
        if role != "worker":
            worker = selected.get("worker")
            if worker:
                return {**worker, "inherited": True, "source": "worker"}
        return {"model": None, "backend": DEFAULT_BACKEND,
                "inherited": True, "source": None}

    def resolve_session(self, kind: str) -> dict:
        """The model + backend a session kind ('run' | 'review' | 'resolve') runs on."""
        role = SESSION_ROLES.get(kind)
        if role is None:
            return {"model": None, "backend": DEFAULT_BACKEND,
                    "inherited": True, "source": None}
        return self.resolve(role)

    def model_for_session(self, kind: str) -> str | None:
        """The model to pin for a session kind ('run' | 'review' | 'resolve')."""
        return self.resolve_session(kind)["model"]

    def backend_for_session(self, kind: str) -> str:
        """The backend key that runs a session kind."""
        return self.resolve_session(kind)["backend"]


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
    """The user's own `active_model` — what Vibe runs on with no override."""
    home = Path(home) if home is not None else vibe_home()
    try:
        data = tomllib.loads((home / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("active_model")
    return value if isinstance(value, str) and value else None


def model_catalog(home: Path | None = None, config=None) -> list[dict]:
    """Every model the harness can run, tagged with the backend that runs it.

    Vibe's list comes from the user's config; Claude's is its three aliases
    (the picker adds an "other…" entry for a full model name). A backend whose
    CLI is not installed still lists its models, marked unavailable with the
    reason, so the picker can disable them instead of hiding them.
    """
    home = Path(home) if home is not None else vibe_home()
    out = [{"alias": m["alias"], "label": m["display_name"],
            "backend": "vibe", "thinking": m.get("thinking")}
           for m in available_models(home)]
    out += [{"alias": m["alias"], "label": m["label"],
             "backend": "claude", "thinking": None} for m in CLAUDE_MODELS]
    availability = {b["key"]: b for b in describe_backends(config)} if config else {}
    for entry in out:
        info = availability.get(entry["backend"])
        entry["available"] = info["available"] if info else True
        entry["reason"] = (None if entry["available"]
                           else f"{info['binary']} is not on PATH")
    return out


def describe(settings: ModelSettings, home: Path | None = None,
             config=None) -> dict:
    """The whole picture the board needs: roles, pickable models, selection."""
    home = Path(home) if home is not None else (settings.home or vibe_home())
    selected = settings.all()
    roles = []
    for role in ROLES:
        own = selected.get(role["key"]) or {}
        resolved = settings.resolve(role["key"], selected)
        roles.append({
            **role,
            "available": role["session"] is not None,
            "model": own.get("model", ""),
            "backend": own.get("backend", ""),
            "effective": resolved["model"] or "",
            "effective_backend": resolved["backend"],
            "inherited": resolved["inherited"],
            "inherited_from": resolved["source"],
        })
    worker = settings.resolve("worker", selected)
    return {
        "roles": roles,
        "models": model_catalog(home, config),
        # The board's own platform: the worker's resolved backend. The token
        # counter follows it (only Vibe streams live counts).
        "resolved_backend": worker["backend"],
        "default_model": worker["model"] or "",
        "updated_at": _now(),
    }
