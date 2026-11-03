"""Per-project transcript stores: `<project>/.vibe-harness/transcripts.db`.

A project's transcripts belong to that project, so the store lives inside the
project folder instead of one shared database at the harness root. The folder
is kept out of the project's git through the local-only `.git/info/exclude`
(never the tracked `.gitignore`), a card with no project falls back to the
harness store, and the old harness-root database is drained into the
per-project ones on first use.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.transcripts import DB_ENV, DB_FILENAME, HARNESS_DIR, TranscriptStore
from test_transcripts import create_card, make_env, make_session_dir

git_test = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


@pytest.fixture()
def env(tmp_path):
    """The same temp-dir app the transcript tests use (fake vibe, temp stores)."""
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def git(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def make_project(env, name: str = "PDF_scanner", *, git_repo: bool = True) -> Path:
    """Register a project folder (a git repo by default); returns its path."""
    path = env.tmp_path / name
    path.mkdir()
    if git_repo:
        git(["init", "-q", "-b", "main", str(path)], env.tmp_path)
        (path / ".gitignore").write_text("*.log\n")
        git(["add", ".gitignore"], path)
        git(["-c", "user.email=t@example.com", "-c", "user.name=t",
             "commit", "-qm", "init"], path)
    env.runner.projects.create(name, str(path))
    # The registry resolves the path; that is the canonical one.
    return Path(env.runner.projects.get(name)["path"])


def create_project_card(client, project: str, title: str = "Transcripts") -> dict:
    r = client.post("/api/cards", json={"title": title, "body": "do a thing",
                                        "project": project})
    assert r.status_code == 201, r.text
    return r.json()


def project_db(project: Path) -> Path:
    return project / HARNESS_DIR / DB_FILENAME


def wait_for_done(client, card_id: str, timeout: float = 20.0) -> dict:
    """Poll the card until the run pipeline concludes (done or failed)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        card = client.get(f"/api/cards/{card_id}").json()
        if card["status"] in {"done", "failed"}:
            return card
        time.sleep(0.05)
    raise AssertionError(f"card {card_id} did not finish in {timeout}s")


# ------------------------------------------------------- path resolution

def test_project_store_lives_inside_the_project(env):
    project = make_project(env)
    card = create_project_card(env.client, "PDF_scanner")

    store = env.runner.transcripts_for(card)

    assert store.db_path == project_db(project)
    assert store is not env.transcripts


def test_card_without_a_project_uses_the_harness_store(env):
    card = create_card(env.client)

    store = env.runner.transcripts_for(card)

    assert store is env.transcripts
    assert store.db_path == env.sessions_dir.parent / HARNESS_DIR / DB_FILENAME


def test_store_is_cached_per_project_path(env):
    make_project(env)
    first = create_project_card(env.client, "PDF_scanner", title="One")
    second = create_project_card(env.client, "PDF_scanner", title="Two")

    assert env.runner.transcripts_for(first) is env.runner.transcripts_for(second)


def test_env_override_wins_over_the_project_store(env, tmp_path, monkeypatch):
    project = make_project(env)
    target = tmp_path / "elsewhere" / "one-store.db"
    monkeypatch.setenv(DB_ENV, str(target))
    card = create_project_card(env.client, "PDF_scanner")

    assert env.runner.transcripts_for(card).db_path == target
    # A store that does not live in the project leaves the repo alone.
    assert HARNESS_DIR not in (project / ".git" / "info" / "exclude").read_text()


# ------------------------------------------------------- keeping it out of git

@git_test
def test_project_store_is_kept_out_of_the_project_git(env):
    project = make_project(env)
    card = create_project_card(env.client, "PDF_scanner")
    store = env.runner.transcripts_for(card)
    store.save(card["id"], "run-1", make_session_dir(env.tmp_path / "handmade"))

    exclude = (project / ".git" / "info" / "exclude").read_text()
    assert f"{HARNESS_DIR}/" in exclude
    # The tracked .gitignore is untouched: the user's repo gains no commit.
    assert (project / ".gitignore").read_text() == "*.log\n"
    assert git(["status", "--porcelain"], project) == ""


@git_test
def test_harness_store_is_kept_out_of_the_harness_root_git(env):
    # The harness root is a repository too (in production it is the harness's
    # own project): its store must not dirty it either.
    root = env.sessions_dir.parent
    git(["init", "-q", "-b", "main", str(root)], env.tmp_path)
    card = create_card(env.client)
    env.transcripts.save(card["id"], "run-1", make_session_dir(env.tmp_path / "a"))

    env.runner._transcripts_migrated = False
    env.runner.migrate_transcripts()

    assert f"{HARNESS_DIR}/" in (root / ".git" / "info" / "exclude").read_text()
    assert HARNESS_DIR not in git(["status", "--porcelain"], root)


def test_non_git_project_skips_the_exclude(env):
    project = make_project(env, git_repo=False)
    card = create_project_card(env.client, "PDF_scanner")

    store = env.runner.transcripts_for(card)

    assert store.db_path == project_db(project)
    assert not (project / ".git").exists()


# ------------------------------------------------------- routing the writes

def test_capture_writes_to_the_card_project_store(env):
    make_project(env)
    card = create_project_card(env.client, "PDF_scanner")
    rdir = env.sessions_dir / card["id"] / "run-1"
    make_session_dir(rdir / "export")

    env.runner._capture_transcript(card["id"], "run-1", rdir)

    assert not (rdir / "export" / "session").exists()
    assert [r["round"] for r in env.runner.transcripts_for(card).list(card["id"])] == ["run-1"]
    assert env.transcripts.list(card["id"]) == []


@git_test
def test_a_project_card_transcript_lands_in_the_project(env):
    project = make_project(env)
    card = create_project_card(env.client, "PDF_scanner")

    env.client.post(f"/api/cards/{card['id']}/run")
    final = wait_for_done(env.client, card["id"])

    assert final["status"] == "done", final
    store = env.runner.transcripts_for(card)
    assert {row["round"] for row in store.list(card["id"])} == {"run-1", "review-1"}
    assert env.transcripts.list(card["id"]) == []
    assert project_db(project).exists()
    assert git(["status", "--porcelain"], project) == ""


def test_card_endpoints_read_the_project_store(env):
    make_project(env)
    card = create_project_card(env.client, "PDF_scanner")
    env.runner.transcripts_for(card).save(
        card["id"], "run-1", make_session_dir(env.tmp_path / "handmade"))

    rows = env.client.get(f"/api/cards/{card['id']}/transcripts").json()
    assert [row["round"] for row in rows] == ["run-1"]
    assert env.client.get(
        f"/api/cards/{card['id']}/transcripts/run-1").status_code == 200
    events = env.client.get(
        f"/api/cards/{card['id']}/transcripts/run-1/events").json()
    assert events["events"][0]["text"] == "hello from the transcript"


# ------------------------------------------------------- backfill routing

def test_backfill_routes_each_round_to_its_card_project_store(env):
    make_project(env)
    with_project = create_project_card(env.client, "PDF_scanner", title="With project")
    without = create_card(env.client, title="No project")
    first = make_session_dir(env.sessions_dir / with_project["id"] / "run-1" / "export")
    second = make_session_dir(env.sessions_dir / without["id"] / "run-1" / "export")

    result = env.client.post("/api/transcripts/backfill").json()

    assert result["imported"] == 2
    assert not first.exists() and not second.exists()
    project_store = env.runner.transcripts_for(with_project)
    assert [r["round"] for r in project_store.list(with_project["id"])] == ["run-1"]
    assert env.transcripts.list(with_project["id"]) == []
    assert [r["round"] for r in env.transcripts.list(without["id"])] == ["run-1"]


def test_backfill_falls_back_to_the_harness_store_when_the_project_is_gone(env):
    make_project(env)
    card = create_project_card(env.client, "PDF_scanner")
    env.runner.projects.delete("PDF_scanner")
    session = make_session_dir(env.sessions_dir / card["id"] / "run-1" / "export")

    result = env.client.post("/api/transcripts/backfill").json()

    assert result["imported"] == 1
    assert not session.exists()
    assert [r["round"] for r in env.transcripts.list(card["id"])] == ["run-1"]


# ------------------------------------------------------- stats

def test_stats_reports_per_store_rows_and_totals(env):
    project = make_project(env)
    card = create_project_card(env.client, "PDF_scanner")
    other = create_card(env.client, title="No project")
    env.runner.transcripts_for(card).save(
        card["id"], "run-1", make_session_dir(env.tmp_path / "a"))
    env.transcripts.save(other["id"], "run-1", make_session_dir(env.tmp_path / "b"))

    stats = env.client.get("/api/transcripts/stats").json()

    assert stats["rows"] == 2
    assert stats["raw_bytes"] > stats["gz_bytes"] > 0
    assert stats["ratio"] == round(stats["raw_bytes"] / stats["gz_bytes"], 2)
    by_path = {row["path"]: row for row in stats["stores"]}
    project_row = by_path[str(project_db(project))]
    assert project_row["project"] == "PDF_scanner"
    assert project_row["rows"] == 1
    harness_row = by_path[str(env.transcripts.db_path)]
    assert harness_row["project"] is None
    assert harness_row["rows"] == 1
    assert sum(row["rows"] for row in stats["stores"]) == stats["rows"]


def test_stats_counts_the_harness_project_store_once(env):
    # The harness's own project IS the harness root: one file, one row.
    env.runner.projects.create("Vibe Harness", str(env.sessions_dir.parent))
    card = create_project_card(env.client, "Vibe Harness")
    env.runner.transcripts_for(card).save(
        card["id"], "run-1", make_session_dir(env.tmp_path / "a"))

    stats = env.client.get("/api/transcripts/stats").json()

    assert stats["rows"] == 1
    paths = [row["path"] for row in stats["stores"]]
    assert paths.count(str(env.transcripts.db_path)) == 1


# ------------------------------------------------------- migration

def legacy_store(env) -> TranscriptStore:
    """The old harness-root store every project's transcripts used to land in."""
    return TranscriptStore(env.sessions_dir.parent / DB_FILENAME)


def test_migration_moves_root_rows_into_the_project_stores(env):
    make_project(env)
    card = create_project_card(env.client, "PDF_scanner")
    legacy = legacy_store(env)
    legacy.save(card["id"], "run-1", make_session_dir(env.tmp_path / "old"))

    result = env.runner.migrate_transcripts()

    assert result == {"moved": 1, "kept": 0}
    project_store = env.runner.transcripts_for(card)
    assert [r["round"] for r in project_store.list(card["id"])] == ["run-1"]
    assert project_store.get(card["id"], "run-1") is not None
    assert not legacy.db_path.exists()


def test_migration_keeps_rows_without_a_project_in_the_harness_store(env):
    card = create_card(env.client)
    legacy = legacy_store(env)
    legacy.save(card["id"], "run-1", make_session_dir(env.tmp_path / "old"))

    result = env.runner.migrate_transcripts()

    assert result == {"moved": 1, "kept": 0}
    assert [r["round"] for r in env.transcripts.list(card["id"])] == ["run-1"]
    assert not legacy.db_path.exists()


def test_migration_is_idempotent(env):
    make_project(env)
    card = create_project_card(env.client, "PDF_scanner")
    legacy = legacy_store(env)
    legacy.save(card["id"], "run-1", make_session_dir(env.tmp_path / "old"))
    assert env.runner.migrate_transcripts()["moved"] == 1

    # A second first-use (a restarted server) finds nothing left to move.
    env.runner._transcripts_migrated = False
    assert env.runner.migrate_transcripts() == {"moved": 0, "kept": 0}
    assert len(env.runner.transcripts_for(card).list(card["id"])) == 1


def test_migration_runs_on_first_use(env):
    make_project(env)
    card = create_project_card(env.client, "PDF_scanner")
    legacy = legacy_store(env)
    legacy.save(card["id"], "run-1", make_session_dir(env.tmp_path / "old"))

    # No explicit migrate call: the first store lookup drains the old file.
    assert [r["round"] for r in env.runner.transcripts_for(card).list(card["id"])] == ["run-1"]
    assert not legacy.db_path.exists()
