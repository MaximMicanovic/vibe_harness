"""Tests for app.backup — the one-command board-state snapshot.

`python -m app.backup` tars cards/, archives/ and the root *.json state files
(models/projects/settings/stacks) into a timestamped dir under the backup root
(default <harness>/backups/) and prints the tarball path. sessions/ is skipped
by default (huge); --include-sessions adds it. Stdlib tarfile only — no new
dependencies, no pyproject script.
"""
from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

from app import backup

ROOT = Path(__file__).resolve().parent.parent

STATE_FILES = ("models.json", "projects.json", "settings.json", "stacks.json")


def _make_state(root: Path) -> None:
    """A minimal board: one card, one archive, one session, the state jsons."""
    (root / "cards").mkdir(parents=True)
    (root / "cards" / "abc123.md").write_text("---\nid: abc123\n---\n", encoding="utf-8")
    (root / "archives" / "deck-x1").mkdir(parents=True)
    (root / "archives" / "deck-x1" / "archive.md").write_text("manifest\n", encoding="utf-8")
    (root / "sessions" / "abc123").mkdir(parents=True)
    (root / "sessions" / "abc123" / "log.jsonl").write_text("{}\n", encoding="utf-8")
    for name in STATE_FILES:
        (root / name).write_text("{}\n", encoding="utf-8")


def _names(tarball: Path) -> list[str]:
    with tarfile.open(tarball, "r:gz") as tar:
        return tar.getnames()


def test_backup_tarball_contains_cards_archives_and_state_files(tmp_path):
    root = tmp_path / "root"
    _make_state(root)
    tarball = backup.create_backup(root=root, out_dir=tmp_path / "backups")
    assert tarball.is_file()
    # the tarball sits in a timestamped dir inside the backup root
    assert tarball.parent.parent == tmp_path / "backups"
    names = _names(tarball)
    assert "cards/abc123.md" in names
    assert "archives/deck-x1/archive.md" in names
    for name in STATE_FILES:
        assert name in names


def test_sessions_skipped_by_default_and_included_with_flag(tmp_path):
    root = tmp_path / "root"
    _make_state(root)
    tarball = backup.create_backup(root=root, out_dir=tmp_path / "b-default")
    assert not any(n == "sessions" or n.startswith("sessions/") for n in _names(tarball))

    tarball = backup.create_backup(root=root, out_dir=tmp_path / "b-sessions",
                                   include_sessions=True)
    assert "sessions/abc123/log.jsonl" in _names(tarball)


def test_missing_state_is_tolerated(tmp_path):
    """A fresh checkout has no cards/ or *.json yet — the snapshot still works."""
    root = tmp_path / "empty"
    root.mkdir()
    tarball = backup.create_backup(root=root, out_dir=tmp_path / "backups")
    assert tarball.is_file()


def test_main_prints_the_tarball_path(tmp_path, capsys, monkeypatch):
    root = tmp_path / "root"
    _make_state(root)
    monkeypatch.setattr(backup, "BASE_DIR", root)
    assert backup.main([]) == 0
    printed = Path(capsys.readouterr().out.strip())
    assert printed.is_file()
    assert printed.parent.parent == root / "backups"
    assert "cards/abc123.md" in _names(printed)


def test_module_entrypoint_runs(tmp_path):
    """`python -m app.backup` is the interface — no pyproject script needed."""
    proc = subprocess.run(
        [sys.executable, "-m", "app.backup", "--out", str(tmp_path / "backups")],
        capture_output=True, text=True, timeout=120, cwd=ROOT,
    )
    assert proc.returncode == 0, proc.stderr
    printed = Path(proc.stdout.strip())
    assert printed.is_file()
    assert printed.parent.parent == tmp_path / "backups"
