"""One-command snapshot of the board state: `python -m app.backup`.

cards/, archives/ and the root *.json state files (models/projects/settings/
stacks) are gitignored, so they are never committed — this tars them into a
timestamped dir under the backup root (default <harness>/backups/) and prints
the tarball path. sessions/ is skipped by default (huge); pass
--include-sessions to add it. Stdlib tarfile only; a snapshot, not a sync.
"""
from __future__ import annotations

import argparse
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from app.config import BASE_DIR

#: Top-level dirs that hold board state. sessions/ is opt-in (--include-sessions).
STATE_DIRS = ("cards", "archives")
SESSIONS_DIR_NAME = "sessions"


def create_backup(root: Path | None = None, out_dir: Path | None = None,
                  include_sessions: bool = False) -> Path:
    """Tar the board state under `root` into <out_dir>/<timestamp>/backup.tar.gz.

    Members are named relative to `root` (cards/..., archives/..., *.json).
    Missing dirs/files are skipped, so a fresh checkout backs up fine.
    Returns the tarball path.
    """
    root = BASE_DIR if root is None else root
    out_dir = BASE_DIR / "backups" if out_dir is None else out_dir
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = out_dir / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    tarball = backup_dir / "backup.tar.gz"
    dirs = list(STATE_DIRS)
    if include_sessions:
        dirs.append(SESSIONS_DIR_NAME)
    with tarfile.open(tarball, "w:gz") as tar:
        for name in dirs:
            path = root / name
            if path.is_dir():
                tar.add(path, arcname=name)
        for path in sorted(root.glob("*.json")):
            if path.is_file():
                tar.add(path, arcname=path.name)
    return tarball


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.backup",
        description="Tar cards/, archives/ and the root *.json state files into a "
                    "timestamped backup dir and print the tarball path.",
    )
    parser.add_argument("--out", type=Path, default=None, metavar="DIR",
                        help="backup root dir (default: <harness>/backups/)")
    parser.add_argument("--include-sessions", action="store_true",
                        help="also tar sessions/ (huge — skipped by default)")
    args = parser.parse_args(argv)
    tarball = create_backup(out_dir=args.out, include_sessions=args.include_sessions)
    print(tarball)
    return 0


if __name__ == "__main__":
    sys.exit(main())
