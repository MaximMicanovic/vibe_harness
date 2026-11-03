"""Tests for per-role model selection: app.models.ModelSettings + /api/models.

The harness runs three kinds of session — the worker (run-N), the reviewer
(review-N) and the merge-conflict resolver (resolve-N) — and each can run on a
different model. Choosing a model IS choosing the platform: the model list
carries both backends, and picking one sets the backend for that role. Vibe has
no --model flag, but its config layers put VIBE_* env vars above the user's
config.toml, so the runner pins the choice per spawn with VIBE_ACTIVE_MODEL;
Claude takes `--model`. Nothing global is rewritten.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.models import (
    ROLES,
    ModelError,
    ModelSettings,
    available_models,
    default_model,
    describe,
    model_catalog,
)
from app.cards import CardStore
from app.projects import ProjectStore
from app.runner import SessionRunner
from app.settings import SettingsStore

FAKE_VIBE = "#!/bin/sh\nexit 0\n"


@pytest.fixture
def settings(tmp_path) -> ModelSettings:
    return ModelSettings(tmp_path / "models.json")


# ----------------------------------------------------------------------
# the store
# ----------------------------------------------------------------------

def test_roles_cover_the_three_session_kinds():
    sessions = {r["session"] for r in ROLES if r["session"]}
    assert sessions == {"run", "review", "resolve"}
    # The orchestrator has no session yet, so it is offered but not usable.
    orchestrator = next(r for r in ROLES if r["key"] == "orchestrator")
    assert orchestrator["session"] is None


def test_set_and_read_a_role(settings):
    assert settings.all() == {}
    settings.set("review", "mistral-large-4-0-3")
    assert settings.get("review") == "mistral-large-4-0-3"
    # The choice carries the platform that runs it.
    assert settings.all()["review"] == {"model": "mistral-large-4-0-3",
                                        "backend": "vibe"}
    assert settings.model_for_session("review") == "mistral-large-4-0-3"
    assert settings.backend_for_session("review") == "vibe"
    # A role with no choice inherits the worker's; the worker itself falls
    # back to Vibe with the CLI's own default model.
    assert settings.model_for_session("run") is None
    assert settings.backend_for_session("run") == "vibe"


def test_clearing_a_role_removes_it(settings):
    settings.set("worker", "glm-5-3")
    settings.set("worker", "")
    assert settings.all() == {}
    settings.set("worker", "glm-5-3")
    settings.set("worker", None)
    assert settings.all() == {}


def test_update_applies_a_patch(settings):
    settings.update({"worker": "glm-5-3", "review": "mistral-large-4-0-3"})
    assert settings.all() == {
        "worker": {"model": "glm-5-3", "backend": "vibe"},
        "review": {"model": "mistral-large-4-0-3", "backend": "vibe"},
    }
    settings.update({"worker": ""})
    assert settings.all() == {"review": {"model": "mistral-large-4-0-3",
                                         "backend": "vibe"}}


def test_update_accepts_the_new_shape(settings):
    settings.update({"review": {"model": "sonnet", "backend": "claude"}})
    assert settings.all() == {"review": {"model": "sonnet", "backend": "claude"}}


def test_unknown_role_is_rejected(settings):
    with pytest.raises(ModelError):
        settings.set("nope", "glm-5-3")
    with pytest.raises(ModelError):
        settings.update({"nope": "glm-5-3"})
    with pytest.raises(ModelError):
        settings.get("nope")


def test_orchestrator_cannot_be_set(settings):
    # Nothing would ever use it, so accepting a value would be a lie.
    with pytest.raises(ModelError):
        settings.set("orchestrator", "glm-5-3")
    with pytest.raises(ModelError):
        settings.update({"orchestrator": "glm-5-3"})


def test_settings_persist_to_disk(settings, tmp_path):
    settings.set("resolver", "glm-5-3")
    assert (tmp_path / "models.json").exists()
    again = ModelSettings(tmp_path / "models.json")
    assert again.get("resolver") == "glm-5-3"


def test_a_corrupt_file_reads_as_empty(tmp_path):
    path = tmp_path / "models.json"
    path.write_text("{not json", encoding="utf-8")
    assert ModelSettings(path).all() == {}


def test_unknown_roles_in_the_file_are_ignored(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {"worker": "glm-5-3", "bogus": "x"}}),
                    encoding="utf-8")
    assert ModelSettings(path).all() == {"worker": {"model": "glm-5-3",
                                                    "backend": "vibe"}}


# ----------------------------------------------------------------------
# migration: the flat shape still works
# ----------------------------------------------------------------------

def test_flat_models_json_migrates_on_read(tmp_path):
    """A file written before the backend was part of the choice still reads:
    every flat entry was written for Vibe."""
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {"worker": "glm-5-3",
                                           "review": "mistral-large-4-0-3"}}),
                    encoding="utf-8")
    store = ModelSettings(path)
    assert store.all() == {
        "worker": {"model": "glm-5-3", "backend": "vibe"},
        "review": {"model": "mistral-large-4-0-3", "backend": "vibe"},
    }
    assert store.get("worker") == "glm-5-3"


def test_the_migrated_shape_is_rewritten_on_the_next_save(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {"worker": "glm-5-3"}}), encoding="utf-8")
    store = ModelSettings(path)
    store.set("review", "sonnet")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw == {"models": {
        "worker": {"model": "glm-5-3", "backend": "vibe"},
        "review": {"model": "sonnet", "backend": "claude"},
    }}
    # Idempotent: reading the rewritten file gives the same map, and saving it
    # again does not change it.
    again = ModelSettings(path)
    assert again.all() == raw["models"]
    again.set("review", "sonnet")
    assert json.loads(path.read_text(encoding="utf-8")) == raw


def test_a_flat_entry_with_an_empty_model_is_dropped(tmp_path):
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"models": {"worker": "  ", "review": "glm-5-3"}}),
                    encoding="utf-8")
    assert ModelSettings(path).all() == {"review": {"model": "glm-5-3",
                                                    "backend": "vibe"}}


# ----------------------------------------------------------------------
# which models can be picked
# ----------------------------------------------------------------------

def _fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "vibe-home"
    home.mkdir()
    (home / "config.toml").write_text(
        'active_model = "deepseek-v4-1-flash-1017"\n'
        '\n[[models]]\nalias = "mistral-large-4-0-3"\nthinking = "high"\n'
        '\n[[models]]\nalias = "glm-5-3"\nthinking = "high"\n',
        encoding="utf-8")
    (home / "experiment_eval_cache.json").write_text(json.dumps({
        "abc": {"payload": {"features": {"vibe_cli_extra_models": {"defaultValue": {
            "models": [
                {"alias": "mistral-large-4-0-3", "display_name": "Mistral Large 4"},
                {"alias": "zai-glm-5-3-flash", "display_name": "GLM-5.3 Flash"},
            ]}}}}},
    }), encoding="utf-8")
    return home


def test_available_models_merges_config_and_catalog(tmp_path):
    home = _fake_home(tmp_path)
    models = {m["alias"]: m for m in available_models(home)}
    assert set(models) == {"deepseek-v4-1-flash-1017", "mistral-large-4-0-3",
                           "glm-5-3", "zai-glm-5-3-flash"}
    # The catalog's nicer display name wins over the bare alias.
    assert models["mistral-large-4-0-3"]["display_name"] == "Mistral Large 4"
    assert models["glm-5-3"]["display_name"] == "glm-5-3"


def test_default_model_is_the_users_active_model(tmp_path):
    assert default_model(_fake_home(tmp_path)) == "deepseek-v4-1-flash-1017"


def test_missing_home_is_not_an_error(tmp_path):
    empty = tmp_path / "nothing"
    assert available_models(empty) == []
    assert default_model(empty) is None


# ----------------------------------------------------------------------
# one list, both platforms
# ----------------------------------------------------------------------

def test_catalog_lists_both_platforms(tmp_path):
    home = _fake_home(tmp_path)
    catalog = {m["alias"]: m for m in model_catalog(home)}
    # The four Vibe models, each tagged with the backend that runs it.
    assert {a for a, m in catalog.items() if m["backend"] == "vibe"} == {
        "deepseek-v4-1-flash-1017", "mistral-large-4-0-3", "glm-5-3",
        "zai-glm-5-3-flash"}
    assert catalog["mistral-large-4-0-3"]["label"] == "Mistral Large 4"
    # The three Claude aliases, labelled with their platform.
    assert {a for a, m in catalog.items() if m["backend"] == "claude"} == {
        "sonnet", "opus", "haiku"}
    assert catalog["sonnet"]["label"] == "Claude Sonnet"
    assert catalog["opus"]["label"] == "Claude Opus"
    assert catalog["haiku"]["label"] == "Claude Haiku"


def test_catalog_marks_a_missing_cli_unavailable(tmp_path):
    """A backend whose CLI is not installed still lists its models, marked
    unavailable with the reason, so the picker can disable them."""
    home = _fake_home(tmp_path)
    config = Config(vibe_bin=str(tmp_path / "vibe"),
                    claude_bin=str(tmp_path / "claude"))
    catalog = model_catalog(home, config)
    assert catalog
    assert all(m["available"] is False for m in catalog)
    assert all(m["reason"] for m in catalog)

    fake = tmp_path / "vibe"
    fake.write_text(FAKE_VIBE)
    fake.chmod(0o755)
    catalog = model_catalog(home, Config(vibe_bin=str(fake),
                                         claude_bin=str(tmp_path / "claude")))
    assert all(m["available"] for m in catalog if m["backend"] == "vibe")
    assert all(not m["available"] for m in catalog if m["backend"] == "claude")


# ----------------------------------------------------------------------
# choosing a model chooses the platform
# ----------------------------------------------------------------------

def test_set_records_the_backend_of_the_model(tmp_path):
    store = ModelSettings(tmp_path / "models.json")
    store.set("review", "sonnet")
    assert store.all()["review"] == {"model": "sonnet", "backend": "claude"}
    store.set("worker", "glm-5-3")
    assert store.all()["worker"] == {"model": "glm-5-3", "backend": "vibe"}


def test_a_backend_that_does_not_offer_the_model_is_rejected(tmp_path):
    """The old global Backend dropdown allowed `claude` + a Vibe model — a
    combination that cannot run. The backend now has to offer the model."""
    home = _fake_home(tmp_path)
    store = ModelSettings(tmp_path / "models.json", home=home)
    with pytest.raises(ModelError):
        store.set("worker", "mistral-large-4-0-3", "claude")
    with pytest.raises(ModelError):
        store.set("review", "sonnet", "vibe")
    with pytest.raises(ModelError):
        store.set("review", "sonnet", "gpt")
    assert store.all() == {}
    # A full Claude model name typed by hand is fine.
    store.set("review", "claude-sonnet-4-6", "claude")
    assert store.all()["review"] == {"model": "claude-sonnet-4-6",
                                     "backend": "claude"}


def test_resolution_follows_the_worker_then_falls_back_to_vibe(tmp_path):
    store = ModelSettings(tmp_path / "models.json")
    # Nothing chosen anywhere: Vibe, with the CLI's own default model.
    assert store.resolve("worker") == {"model": None, "backend": "vibe",
                                       "inherited": True, "source": None}
    assert store.resolve("review")["backend"] == "vibe"
    # A role with its own choice uses its own backend.
    store.set("worker", "sonnet")
    assert store.resolve("worker") == {"model": "sonnet", "backend": "claude",
                                       "inherited": False, "source": "worker"}
    # "inherit" follows the worker's choice, backend included.
    assert store.resolve("review") == {"model": "sonnet", "backend": "claude",
                                       "inherited": True, "source": "worker"}
    assert store.backend_for_session("review") == "claude"
    # ...until the role picks its own.
    store.set("review", "mistral-large-4-0-3")
    assert store.resolve("review") == {"model": "mistral-large-4-0-3",
                                       "backend": "vibe", "inherited": False,
                                       "source": "review"}
    assert store.backend_for_session("review") == "vibe"
    assert store.backend_for_session("run") == "claude"


def test_describe_reports_effective_and_inherited(tmp_path, settings):
    home = _fake_home(tmp_path)
    settings.set("review", "mistral-large-4-0-3")
    data = describe(settings, home)
    by_key = {r["key"]: r for r in data["roles"]}
    assert by_key["review"]["model"] == "mistral-large-4-0-3"
    assert by_key["review"]["backend"] == "vibe"
    assert by_key["review"]["inherited"] is False
    assert by_key["worker"]["model"] == ""
    assert by_key["worker"]["inherited"] is True
    # Nothing chosen anywhere: Vibe, with the CLI's own default model.
    assert by_key["worker"]["effective"] == ""
    assert by_key["worker"]["effective_backend"] == "vibe"
    assert by_key["orchestrator"]["available"] is False
    assert data["resolved_backend"] == "vibe"
    assert {m["alias"] for m in data["models"]} >= {"sonnet", "glm-5-3"}


def test_describe_follows_the_worker_for_inheriting_roles(tmp_path, settings):
    home = _fake_home(tmp_path)
    settings.set("worker", "sonnet")
    data = describe(settings, home)
    by_key = {r["key"]: r for r in data["roles"]}
    assert by_key["review"]["effective"] == "sonnet"
    assert by_key["review"]["effective_backend"] == "claude"
    assert by_key["review"]["inherited_from"] == "worker"
    assert data["resolved_backend"] == "claude"


# ----------------------------------------------------------------------
# the runner pins the model per session
# ----------------------------------------------------------------------

def test_runner_resolves_each_session_kind(tmp_path):
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin=str(tmp_path / "vibe"), time_limit_seconds=30)
    settings = ModelSettings(tmp_path / "models.json")
    settings.update({"worker": "glm-5-3", "review": "sonnet",
                     "resolver": "zai-glm-5-3-flash"})
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=ProjectStore(tmp_path / "projects.json"),
                           models=settings)
    assert runner._role_model("run") == "glm-5-3"
    assert runner._role_backend("run").key == "vibe"
    assert runner._role_model("review") == "sonnet"
    assert runner._role_backend("review").key == "claude"
    assert runner._role_model("resolve") == "zai-glm-5-3-flash"
    assert runner._role_backend("resolve").key == "vibe"
    # An unknown kind (or no choice) leaves the user's own model alone.
    assert runner._role_model("bogus") is None


def test_runner_without_settings_never_overrides(tmp_path):
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin=str(tmp_path / "vibe"), time_limit_seconds=30)
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions")
    assert runner.models is None
    assert runner._model_env("run") == {}


def test_model_env_pins_only_the_configured_roles(tmp_path):
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin=str(tmp_path / "vibe"), time_limit_seconds=30)
    settings = ModelSettings(tmp_path / "models.json")
    settings.update({"review": "mistral-large-4-0-3"})
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           models=settings)
    assert runner._model_env("review") == {"VIBE_ACTIVE_MODEL": "mistral-large-4-0-3"}
    # Roles left on "inherit" get no override at all, so the user's own
    # active_model applies.
    assert runner._model_env("run") == {}
    assert runner._model_env("resolve") == {}


def test_model_env_is_empty_for_claude(tmp_path):
    """Claude takes --model; VIBE_ACTIVE_MODEL would be a lie for it."""
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin=str(tmp_path / "vibe"), time_limit_seconds=30)
    settings = ModelSettings(tmp_path / "models.json")
    settings.update({"review": "sonnet"})
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           models=settings)
    assert runner._model_env("review") == {}


def test_spawned_session_gets_the_role_model(tmp_path, monkeypatch):
    """The env handed to the vibe process carries VIBE_ACTIVE_MODEL."""
    store = CardStore(tmp_path / "cards")
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(FAKE_VIBE)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30, mailbox=False)
    settings = ModelSettings(tmp_path / "models.json")
    settings.update({"worker": "glm-5-3"})
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=ProjectStore(tmp_path / "projects.json"),
                           models=settings)

    captured = {}

    class FakeProc:
        pid = 4242
        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr("app.runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr(runner, "_finalize", lambda *a, **k: None)
    card = store.create("A card", "spec")
    runner._spawn(card["id"], kind="run", prompt="do it")
    assert captured["env"].get("VIBE_ACTIVE_MODEL") == "glm-5-3"


# ----------------------------------------------------------------------
# the model reaches the CLI, whichever backend runs the session
# ----------------------------------------------------------------------

def _runner(tmp_path, models: ModelSettings,
            backend: str | None = None) -> SessionRunner:
    """A runner whose role choices are `models` (the backend comes from them)."""
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin=str(tmp_path / "vibe"),
                    claude_bin=str(tmp_path / "claude"),
                    time_limit_seconds=30, mailbox=False)
    backend_settings = SettingsStore(tmp_path / "settings.json")
    if backend:
        backend_settings.set_backend(backend)
    return SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                         projects=ProjectStore(tmp_path / "projects.json"),
                         settings=backend_settings, models=models)


def _capture_spawn(monkeypatch, runner: SessionRunner) -> dict:
    """Record the command line of the next spawned session."""
    captured: dict = {}

    class FakeProc:
        pid = 4242
        def poll(self):
            return 0
        def wait(self, timeout=None):
            return 0

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr("app.runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr(runner, "_finalize", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_finalize_resolver", lambda *a, **k: None)
    return captured


def test_build_cmd_passes_the_role_model_for_every_kind(tmp_path):
    """The worker, the reviewer and the resolver each get their own model, and
    each model brings the backend that runs it."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "glm-5-3", "review": "sonnet",
                   "resolver": "haiku"})
    runner = _runner(tmp_path, models)
    # Vibe has no --model flag: the worker's choice rides in the env.
    cmd = runner._build_cmd(tmp_path / "p.md", tmp_path, tmp_path, "run")
    assert "--model" not in cmd
    assert runner._model_env("run") == {"VIBE_ACTIVE_MODEL": "glm-5-3"}
    # Claude takes --model, and the reviewer/resolver are their own roles.
    for kind, expected in (("review", "sonnet"), ("resolve", "haiku")):
        cmd = runner._build_cmd(tmp_path / "p.md", tmp_path, tmp_path, kind)
        assert cmd[cmd.index("--model") + 1] == expected
        assert runner._model_env(kind) == {}


def test_build_cmd_without_a_role_model_adds_no_flag(tmp_path):
    runner = _runner(tmp_path, ModelSettings(tmp_path / "models.json"))
    cmd = runner._build_cmd(tmp_path / "p.md", tmp_path, tmp_path, "run")
    assert "--model" not in cmd


def test_build_cmd_keeps_vibe_on_the_env_var(tmp_path):
    """Vibe has no --model flag: the choice stays in VIBE_ACTIVE_MODEL."""
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin=str(tmp_path / "vibe"), time_limit_seconds=30)
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "glm-5-3"})
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           models=models)
    cmd = runner._build_cmd(tmp_path / "p.md", tmp_path, tmp_path, "run")
    assert "--model" not in cmd
    assert runner._model_env("run") == {"VIBE_ACTIVE_MODEL": "glm-5-3"}


def test_spawned_review_gets_the_review_model(tmp_path, monkeypatch):
    """The reviewer is its own role: its model must reach the CLI too."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "glm-5-3", "review": "sonnet"})
    runner = _runner(tmp_path, models)
    captured = _capture_spawn(monkeypatch, runner)
    card = runner.store.create("A card", "spec")
    # A review round only follows a run round.
    runner.store.set_status(card["id"], "running")
    runner._spawn(card["id"], kind="review", prompt="review it")
    cmd = captured["cmd"]
    assert cmd[0] == str(tmp_path / "claude")
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_spawned_resolver_gets_the_resolver_model(tmp_path, monkeypatch):
    """The resolver is its own role: it must not run on the worker's model."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "glm-5-3", "resolver": "haiku"})
    runner = _runner(tmp_path, models)
    captured = _capture_spawn(monkeypatch, runner)
    card = runner.store.create("A card", "spec")
    runner.store.patch_session(card["id"], {"git": {"branch": "b",
                                                    "base_branch": "main"}})
    runner._spawn_resolver(runner.store.get(card["id"]))
    cmd = captured["cmd"]
    assert cmd[0] == str(tmp_path / "claude")
    assert cmd[cmd.index("--model") + 1] == "haiku"


def test_resolver_records_a_rejected_model(tmp_path):
    """A refused resolver model is recorded on the resolver's own record, so
    the failure detail can name it instead of a bare exit code."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"resolver": "claude-nope"})
    runner = _runner(tmp_path, models)
    card = runner.store.create("A card", "spec")
    runner.store.set_status(card["id"], "running")
    runner.store.set_status(card["id"], "done", session_patch={
        "resolver": {"pid": 1, "round": 1},
        "git": {"branch": "harness/abc", "merge_conflict": True},
    })
    rdir = runner.sessions_dir / card["id"] / "resolve-1"
    rdir.mkdir(parents=True)
    (rdir / "session.log").write_text(
        'API Error: 404 {"type":"error","error":{"type":"not_found_error",'
        '"message":"model: claude-nope"}}\n', encoding="utf-8")

    entry = {"proc": SimpleNamespace(wait=lambda: 1), "kind": "resolve",
             "round": 1, "model": "claude-nope", "backend": "claude"}
    runner._record_resolver_outcome(card["id"], entry)

    session = runner.store.get(card["id"])["session"]
    assert "claude-nope" in session["resolver"]["model_error"]
    assert "claude-nope" in session["git"]["resolver_error"]


# ----------------------------------------------------------------------
# the backend is pinned per card run
# ----------------------------------------------------------------------

def test_run_records_the_resolved_backend_on_the_session(tmp_path, monkeypatch):
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "sonnet"})
    runner = _runner(tmp_path, models)
    captured = _capture_spawn(monkeypatch, runner)
    card = runner.store.create("A card", "spec")
    runner._spawn(card["id"], kind="run", prompt="do it")
    assert runner.store.get(card["id"])["session"]["backend"] == "claude"
    assert captured["cmd"][0] == str(tmp_path / "claude")


def test_fixer_resume_keeps_the_pinned_backend(tmp_path, monkeypatch):
    """The worker starts on Claude; the user switches it to a Vibe model while
    the run is live. The fixer round resumes the Claude session id, so it must
    stay on Claude — a Vibe session id cannot be resumed by Claude."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "sonnet"})
    runner = _runner(tmp_path, models)
    captured = _capture_spawn(monkeypatch, runner)
    card = runner.store.create("A card", "spec")
    runner._spawn(card["id"], kind="run", prompt="do it")
    assert runner.store.get(card["id"])["session"]["backend"] == "claude"

    models.update({"worker": "glm-5-3"})   # mid-run change
    runner._spawn(card["id"], kind="run", prompt="fix it", resume="s1")
    cmd = captured["cmd"]
    assert cmd[0] == str(tmp_path / "claude")
    assert cmd[cmd.index("--resume") + 1] == "s1"
    # The Vibe model cannot run on Claude: the resumed round keeps Claude's
    # own default instead of passing a model it would refuse.
    assert "--model" not in cmd
    assert "VIBE_ACTIVE_MODEL" not in captured["env"]


def test_fixer_resume_keeps_vibe_when_the_worker_moves_to_claude(tmp_path,
                                                                 monkeypatch):
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "glm-5-3"})
    runner = _runner(tmp_path, models)
    captured = _capture_spawn(monkeypatch, runner)
    card = runner.store.create("A card", "spec")
    runner._spawn(card["id"], kind="run", prompt="do it")
    assert runner.store.get(card["id"])["session"]["backend"] == "vibe"

    models.update({"worker": "sonnet"})   # mid-run change
    runner._spawn(card["id"], kind="run", prompt="fix it", resume="s1")
    cmd = captured["cmd"]
    assert cmd[0] == str(tmp_path / "vibe")
    assert cmd[cmd.index("--resume") + 1] == "s1"
    assert "VIBE_ACTIVE_MODEL" not in captured["env"]


def test_a_review_round_does_not_overwrite_the_pinned_backend(tmp_path,
                                                              monkeypatch):
    """Only the worker's run pins the platform: a review round on the other
    backend must not repin the card's worker session."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "glm-5-3", "review": "sonnet"})
    runner = _runner(tmp_path, models)
    _capture_spawn(monkeypatch, runner)
    card = runner.store.create("A card", "spec")
    runner._spawn(card["id"], kind="run", prompt="do it")
    runner._spawn(card["id"], kind="review", prompt="review it")
    assert runner.store.get(card["id"])["session"]["backend"] == "vibe"


# ----------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------

def make_env(tmp_path: Path) -> SimpleNamespace:
    store = CardStore(tmp_path / "cards")
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(FAKE_VIBE)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30)
    projects = ProjectStore(tmp_path / "projects.json")
    settings = ModelSettings(tmp_path / "models.json", home=_fake_home(tmp_path))
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects, models=settings)
    app = create_app(config=config, store=store, runner=runner, projects=projects,
                     models=settings)
    return SimpleNamespace(client=TestClient(app), settings=settings,
                           tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def test_api_lists_roles_and_models(env):
    data = env.client.get("/api/models").json()
    assert [r["key"] for r in data["roles"]] == ["worker", "review", "resolver",
                                                 "orchestrator"]
    assert isinstance(data["models"], list)
    assert all(r["inherited"] for r in data["roles"])


def test_api_lists_models_with_their_backend(env):
    data = env.client.get("/api/models").json()
    by_alias = {m["alias"]: m for m in data["models"]}
    assert by_alias["mistral-large-4-0-3"]["backend"] == "vibe"
    assert by_alias["mistral-large-4-0-3"]["label"] == "Mistral Large 4"
    assert by_alias["sonnet"]["backend"] == "claude"
    assert by_alias["sonnet"]["label"] == "Claude Sonnet"
    assert data["resolved_backend"] == "vibe"


def test_api_put_takes_role_model_and_backend(env):
    r = env.client.put("/api/models", json={"role": "review", "model": "sonnet",
                                            "backend": "claude"})
    assert r.status_code == 200, r.text
    review = next(x for x in r.json()["roles"] if x["key"] == "review")
    assert review["model"] == "sonnet"
    assert review["backend"] == "claude"
    assert review["effective_backend"] == "claude"
    assert env.settings.all()["review"] == {"model": "sonnet",
                                            "backend": "claude"}


def test_api_rejects_a_backend_that_does_not_offer_the_model(env):
    r = env.client.put("/api/models", json={"role": "worker",
                                            "model": "mistral-large-4-0-3",
                                            "backend": "claude"})
    assert r.status_code == 400
    assert env.settings.all() == {}
    r = env.client.put("/api/models", json={"role": "review", "model": "sonnet",
                                            "backend": "vibe"})
    assert r.status_code == 400
    assert env.settings.all() == {}


def test_api_clears_a_role(env):
    env.client.put("/api/models", json={"role": "review", "model": "sonnet",
                                        "backend": "claude"})
    r = env.client.put("/api/models", json={"role": "review", "model": ""})
    review = next(x for x in r.json()["roles"] if x["key"] == "review")
    assert review["model"] == "" and review["inherited"] is True
    assert env.settings.all() == {}


def test_api_rejects_unknown_roles_and_the_orchestrator(env):
    assert env.client.put("/api/models",
                          json={"role": "nope", "model": "glm-5-3"}).status_code == 400
    assert env.client.put("/api/models",
                          json={"role": "orchestrator",
                                "model": "glm-5-3"}).status_code == 400
    assert env.settings.all() == {}


def test_api_requires_a_role(env):
    assert env.client.put("/api/models", json={}).status_code == 422
    assert env.client.put("/api/models", json={"model": "glm-5-3"}).status_code == 422
