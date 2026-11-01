"""Tests for app.projects.ProjectStore (projects.json registry)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.projects import (
    ProjectError,
    ProjectExists,
    ProjectNotFound,
    ProjectStore,
)


@pytest.fixture
def projects(tmp_path) -> ProjectStore:
    return ProjectStore(tmp_path / "projects.json")


def test_create_list_get(projects: ProjectStore, tmp_path: Path):
    p = projects.create("my-app", str(tmp_path / "some" / "folder"))
    assert p["name"] == "my-app"
    # The folder is created if missing.
    assert Path(p["path"]).is_dir()

    assert projects.list() == [{"name": "my-app", "path": p["path"]}]
    assert projects.get("my-app") == p
    assert projects.get("nope") is None


def test_persists_across_instances(projects: ProjectStore, tmp_path: Path):
    projects.create("a", str(tmp_path / "a"))
    reloaded = ProjectStore(tmp_path / "projects.json")
    assert [p["name"] for p in reloaded.list()] == ["a"]


def test_list_sorted(projects: ProjectStore, tmp_path: Path):
    projects.create("zeta", str(tmp_path / "z"))
    projects.create("alpha", str(tmp_path / "a"))
    assert [p["name"] for p in projects.list()] == ["alpha", "zeta"]


def test_create_validates(projects: ProjectStore):
    with pytest.raises(ProjectError):
        projects.create("", "/tmp/x")
    with pytest.raises(ProjectError):
        projects.create("  ", "/tmp/x")
    with pytest.raises(ProjectError):
        projects.create("x", "")
    with pytest.raises(ProjectError):
        projects.create("x", "   ")


def test_create_duplicate_raises(projects: ProjectStore, tmp_path: Path):
    projects.create("dup", str(tmp_path / "d"))
    with pytest.raises(ProjectExists):
        projects.create("dup", str(tmp_path / "other"))


def test_delete(projects: ProjectStore, tmp_path: Path):
    projects.create("gone", str(tmp_path / "g"))
    projects.delete("gone")
    assert projects.list() == []
    with pytest.raises(ProjectNotFound):
        projects.delete("gone")


def test_corrupt_file_recovers(tmp_path: Path):
    path = tmp_path / "projects.json"
    path.write_text("not json {{{")
    store = ProjectStore(path)
    assert store.list() == []
    # And it can write again afterwards.
    store.create("ok", str(tmp_path / "ok"))
    assert [p["name"] for p in store.list()] == ["ok"]
