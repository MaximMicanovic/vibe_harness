"""Tests for per-role model selection: app.models.ModelSettings + /api/models.

The harness runs three kinds of vibe session — the worker (run-N), the reviewer
(review-N) and the merge-conflict resolver (resolve-N) — and each can run on a
different model. Vibe has no --model flag, but its config layers put VIBE_*
env vars above the user's config.toml, so the runner pins the choice per spawn
with VIBE_ACTIVE_MODEL. Nothing global is rewritten.
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
    assert settings.model_for_session("review") == "mistral-large-4-0-3"
    # A role with no choice inherits the user's own active_model.
    assert settings.model_for_session("run") is None


def test_clearing_a_role_removes_it(settings):
    settings.set("worker", "glm-5-3")
    settings.set("worker", "")
    assert settings.all() == {}
    settings.set("worker", "glm-5-3")
    settings.set("worker", None)
    assert settings.all() == {}


def test_update_applies_a_patch(settings):
    settings.update({"worker": "glm-5-3", "review": "mistral-large-4-0-3"})
    assert settings.all() == {"worker": "glm-5-3", "review": "mistral-large-4-0-3"}
    settings.update({"worker": ""})
    assert settings.all() == {"review": "mistral-large-4-0-3"}


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
    assert ModelSettings(path).all() == {"worker": "glm-5-3"}


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


def test_describe_reports_effective_and_inherited(tmp_path, settings):
    home = _fake_home(tmp_path)
    settings.set("review", "mistral-large-4-0-3")
    data = describe(settings, home)
    by_key = {r["key"]: r for r in data["roles"]}
    assert by_key["review"]["model"] == "mistral-large-4-0-3"
    assert by_key["review"]["inherited"] is False
    assert by_key["worker"]["model"] == ""
    assert by_key["worker"]["inherited"] is True
    assert by_key["worker"]["effective"] == "deepseek-v4-1-flash-1017"
    assert by_key["orchestrator"]["available"] is False
    assert data["default_model"] == "deepseek-v4-1-flash-1017"


# ----------------------------------------------------------------------
# the runner pins the model per session
# ----------------------------------------------------------------------

def test_runner_sets_vibe_active_model_per_session_kind(tmp_path, monkeypatch):
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin=str(tmp_path / "vibe"), time_limit_seconds=30)
    settings = ModelSettings(tmp_path / "models.json")
    settings.update({"worker": "glm-5-3", "review": "mistral-large-4-0-3",
                     "resolver": "zai-glm-5-3-flash"})
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=ProjectStore(tmp_path / "projects.json"),
                           models=settings)
    assert runner.models.model_for_session("run") == "glm-5-3"
    assert runner.models.model_for_session("review") == "mistral-large-4-0-3"
    assert runner.models.model_for_session("resolve") == "zai-glm-5-3-flash"
    # An unknown kind (or no choice) leaves the user's own model alone.
    assert runner.models.model_for_session("bogus") is None


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

def _claude_runner(tmp_path, models: ModelSettings) -> SessionRunner:
    """A runner on the Claude backend, with `models` as the role choices."""
    store = CardStore(tmp_path / "cards")
    config = Config(vibe_bin=str(tmp_path / "vibe"),
                    claude_bin=str(tmp_path / "claude"),
                    time_limit_seconds=30, mailbox=False)
    backend_settings = SettingsStore(tmp_path / "settings.json")
    backend_settings.set_backend("claude")
    return SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                         projects=ProjectStore(tmp_path / "projects.json"),
                         settings=backend_settings, models=models)


def _capture_spawn(monkeypatch, runner: SessionRunner) -> dict:
    """Record the command line of the next spawned session."""
    captured: dict = {}

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
    monkeypatch.setattr(runner, "_finalize_resolver", lambda *a, **k: None)
    return captured


def test_build_cmd_passes_the_role_model_for_every_kind(tmp_path):
    """The worker, the reviewer and the resolver each get their own model."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "glm-5-3", "review": "mistral-large-4-0-3",
                   "resolver": "zai-glm-5-3-flash"})
    runner = _claude_runner(tmp_path, models)
    for kind, expected in (("run", "glm-5-3"),
                           ("review", "mistral-large-4-0-3"),
                           ("resolve", "zai-glm-5-3-flash")):
        cmd = runner._build_cmd(tmp_path / "p.md", tmp_path, tmp_path, kind)
        assert cmd[cmd.index("--model") + 1] == expected


def test_build_cmd_without_a_role_model_adds_no_flag(tmp_path):
    runner = _claude_runner(tmp_path, ModelSettings(tmp_path / "models.json"))
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
    models.update({"worker": "glm-5-3", "review": "mistral-large-4-0-3"})
    runner = _claude_runner(tmp_path, models)
    captured = _capture_spawn(monkeypatch, runner)
    card = runner.store.create("A card", "spec")
    # A review round only follows a run round.
    runner.store.set_status(card["id"], "running")
    runner._spawn(card["id"], kind="review", prompt="review it")
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "mistral-large-4-0-3"


def test_spawned_resolver_gets_the_resolver_model(tmp_path, monkeypatch):
    """The resolver is its own role: it must not run on the worker's model."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"worker": "glm-5-3", "resolver": "zai-glm-5-3-flash"})
    runner = _claude_runner(tmp_path, models)
    captured = _capture_spawn(monkeypatch, runner)
    card = runner.store.create("A card", "spec")
    runner.store.patch_session(card["id"], {"git": {"branch": "b",
                                                    "base_branch": "main"}})
    runner._spawn_resolver(runner.store.get(card["id"]))
    cmd = captured["cmd"]
    assert cmd[cmd.index("--model") + 1] == "zai-glm-5-3-flash"


def test_resolver_records_a_rejected_model(tmp_path):
    """A refused resolver model is recorded on the resolver's own record, so
    the failure detail can name it instead of a bare exit code."""
    models = ModelSettings(tmp_path / "models.json")
    models.update({"resolver": "claude-nope"})
    runner = _claude_runner(tmp_path, models)
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
             "round": 1, "model": "claude-nope"}
    runner._record_resolver_outcome(card["id"], entry)

    session = runner.store.get(card["id"])["session"]
    assert "claude-nope" in session["resolver"]["model_error"]
    assert "claude-nope" in session["git"]["resolver_error"]


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
    settings = ModelSettings(tmp_path / "models.json")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects, models=settings)
    app = create_app(config=config, store=store, runner=runner, projects=projects,
                     models=settings)
    return SimpleNamespace(client=TestClient(app), settings=settings, tmp_path=tmp_path)


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


def test_api_sets_and_clears_a_role(env):
    r = env.client.put("/api/models", json={"models": {"review": "mistral-large-4-0-3"}})
    assert r.status_code == 200, r.text
    review = next(x for x in r.json()["roles"] if x["key"] == "review")
    assert review["model"] == "mistral-large-4-0-3"
    assert env.settings.get("review") == "mistral-large-4-0-3"

    r = env.client.put("/api/models", json={"models": {"review": ""}})
    review = next(x for x in r.json()["roles"] if x["key"] == "review")
    assert review["model"] == "" and review["inherited"] is True


def test_api_rejects_unknown_roles_and_the_orchestrator(env):
    assert env.client.put("/api/models", json={"models": {"nope": "glm-5-3"}}).status_code == 400
    assert env.client.put("/api/models",
                          json={"models": {"orchestrator": "glm-5-3"}}).status_code == 400
    assert env.settings.all() == {}


def test_api_accepts_an_empty_patch(env):
    r = env.client.put("/api/models", json={"models": {}})
    assert r.status_code == 200
    assert env.settings.all() == {}
