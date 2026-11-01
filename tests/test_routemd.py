"""Tests for app.routemd — per-project git init + agent.md + route.md."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import gitops, routemd
from app.projects import ProjectStore

git_test = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def rules(monkeypatch) -> None:
    monkeypatch.setattr(routemd, "AGENT_MD_BODY", "# rules\n\n- rule one v1\n")


def test_ensure_project_files_creates_empty_route_md_and_agent_md(tmp_path, rules):
    folder = tmp_path / "proj"
    statuses = routemd.ensure_project_files(folder)
    assert statuses == {"route_md": "created", "agent_md": "created"}
    # route.md starts EMPTY (agent-owned), agent.md carries the marker + rules.
    assert (folder / "route.md").read_text() == ""
    agent = (folder / "agent.md").read_text()
    assert routemd.AGENT_MD_MARKER in agent
    assert "rule one v1" in agent


def test_route_md_is_never_modified_once_it_exists(tmp_path, rules):
    folder = tmp_path / "proj"
    routemd.ensure_project_files(folder)
    target = folder / "route.md"
    target.write_text("# my routing cards\n")      # the agent wrote it
    statuses = routemd.ensure_project_files(folder)
    assert statuses["route_md"] == "exists"
    assert target.read_text() == "# my routing cards\n"


def test_agent_md_managed_sync(tmp_path, rules):
    folder = tmp_path / "proj"
    routemd.ensure_project_files(folder)
    assert routemd.ensure_project_files(folder)["agent_md"] == "unchanged"
    # Harness rule update -> managed copy is refreshed.
    routemd.AGENT_MD_BODY = "# rules\n\n- rule one v2\n"
    assert routemd.ensure_project_files(folder)["agent_md"] == "updated"
    assert "rule one v2" in (folder / "agent.md").read_text()
    # Marker removed -> user-owned, never overwritten.
    (folder / "agent.md").write_text("# my own agent rules\n")
    routemd.AGENT_MD_BODY = "# rules\n\n- v3\n"
    assert routemd.ensure_project_files(folder)["agent_md"] == "custom"
    assert (folder / "agent.md").read_text() == "# my own agent rules\n"


def test_default_agent_md_tells_agents_to_update_route_md():
    body = routemd.AGENT_MD_BODY
    assert "route.md" in body
    assert "update" in body.lower()


@git_test
def test_setup_project_inits_repo_and_commits_files(tmp_path, rules):
    folder = tmp_path / "proj"
    statuses = routemd.setup_project(folder, init_repo=True)
    assert statuses["route_md"] == "created"
    assert statuses["agent_md"] == "created"
    assert statuses["repo"] == str(folder.resolve())
    # One initial commit containing .gitignore + agent.md + route.md.
    names = _git(["show", "--name-only", "--format=", "HEAD"], folder)
    assert ".gitignore" in names
    assert "agent.md" in names
    assert "route.md" in names
    assert _git(["status", "--porcelain"], folder) == ""   # clean tree


@git_test
def test_setup_project_existing_repo_commits_files(tmp_path, rules):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "T"], repo)
    (repo / "README.md").write_text("hi\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "initial"], repo)

    statuses = routemd.setup_project(repo, init_repo=True)
    assert statuses["repo"] == str(repo.resolve())
    assert _git(["status", "--porcelain"], repo) == ""
    assert "agent.md" in _git(["show", "--name-only", "--format=", "HEAD"], repo)
    assert "route.md" in _git(["show", "--name-only", "--format=", "HEAD"], repo)
    # The pre-existing commit is untouched.
    assert "README.md" in _git(["show", "--name-only", "--format=", "HEAD~1"], repo)


@git_test
def test_setup_project_existing_empty_repo(tmp_path, rules):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(["init", "-q", "-b", "main"], repo)
    statuses = routemd.setup_project(repo, init_repo=True)
    assert statuses["repo"] == str(repo.resolve())
    names = _git(["show", "--name-only", "--format=", "HEAD"], repo)
    assert "agent.md" in names and "route.md" in names
    assert _git(["status", "--porcelain"], repo) == ""


def test_commit_project_files_skips_non_repo(tmp_path, rules):
    plain = tmp_path / "plain"
    routemd.ensure_project_files(plain)
    assert routemd.commit_project_files_if_repo(plain) is False   # not a repo


def test_sync_all_projects_sets_up_legacy_projects(tmp_path, rules):
    store = ProjectStore(tmp_path / "projects.json")
    store.create("legacy", str(tmp_path / "legacy"))   # registered before files
    assert not (tmp_path / "legacy" / "route.md").exists()
    results = routemd.sync_all_projects(store)
    assert results["legacy"]["route_md"] == "created"
    assert results["legacy"]["agent_md"] == "created"
    assert (tmp_path / "legacy" / "route.md").read_text() == ""
    # A broken project path does not break the others (registered by writing
    # projects.json directly, since create() itself mkdirs the folder).
    import json
    blocker = tmp_path / "afile"
    blocker.write_text("not a dir")
    data = json.loads(store.path.read_text())
    data["broken"] = {"path": str(blocker / "sub")}
    store.path.write_text(json.dumps(data))
    results = routemd.sync_all_projects(store)
    assert "error" in results["broken"]
    assert results["legacy"]["agent_md"] == "unchanged"
