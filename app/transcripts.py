"""Compressed session transcripts in SQLite.

Every round of every card leaves a vibe session directory behind
(``sessions/<card>/<round>/export/session/``: journal, chunks, tool results).
It is the only record of what a model actually did, so it is worth keeping —
but as loose files it was ~1 GB of the harness's 1.1 GB ``sessions/`` tree, and
nothing in ``app/`` ever read it back.

This module keeps those transcripts in SQLite databases as tar.gz blobs:

- ``save()`` tars the directory, gzips it and stores the blob (``INSERT OR
  REPLACE`` on ``(card_id, round)``);
- ``get()`` / ``extract()`` hand it back for debugging;
- ``list()`` / ``stats()`` report metadata only — the blob never leaves the
  row unless it is asked for;
- ``backfill()`` imports the directories that predate the store.

One database per project: ``project_store()`` resolves to
``<project>/.vibe-harness/transcripts.db``, so a project's transcripts live
with the project they belong to (and leave with it). The harness store
(``<harness root>/.vibe-harness/transcripts.db``) only holds the rounds of
cards with no project. ``VIBE_HARNESS_TRANSCRIPTS_DB`` still wins: one
explicit store for everything.

The database is a cache, not a ledger: WAL + ``synchronous=NORMAL``, one
connection per call (the runner finalizes rounds on daemon threads, so a shared
cursor would race), and a failed write must never fail a round — the caller
leaves the directory in place instead.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app import gitops
from app.activity import DEFAULT_LIMIT, read_activity
from app.config import BASE_DIR

#: The per-project folder the harness keeps its own files in (transcripts
#: today). Kept out of the project's git via `.git/info/exclude`.
HARNESS_DIR = ".vibe-harness"
DB_FILENAME = "transcripts.db"
DB_ENV = "VIBE_HARNESS_TRANSCRIPTS_DB"

#: Name the session directory gets inside the archive, so `extract()` always
#: reproduces `export/session/...` whatever the source directory was called.
SESSION_ARCNAME = "session"

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
  id          INTEGER PRIMARY KEY,
  card_id     TEXT NOT NULL,
  round       TEXT NOT NULL,
  session_id  TEXT,
  created_at  TEXT NOT NULL,
  raw_bytes   INTEGER NOT NULL,
  gz_bytes    INTEGER NOT NULL,
  blob        BLOB NOT NULL,
  UNIQUE(card_id, round)
);
CREATE INDEX IF NOT EXISTS idx_transcripts_card ON transcripts(card_id);
"""

#: What `list()` returns: every column but the blob.
_META_COLUMNS = "id, card_id, round, session_id, created_at, raw_bytes, gz_bytes"

_INSERT_SQL = ("INSERT OR REPLACE INTO transcripts "
               "(card_id, round, session_id, created_at, raw_bytes, gz_bytes, blob) "
               "VALUES (?, ?, ?, ?, ?, ?, ?)")


def _dir_bytes(path: Path) -> int:
    """Uncompressed size of every file under `path`."""
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


def _tar_gz(session_dir: Path) -> bytes:
    """The directory as one gzipped tar, its members under `session/`."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(session_dir, arcname=SESSION_ARCNAME)
    return buf.getvalue()


def _session_id(session_dir: Path) -> str | None:
    """The vibe session id from the directory's meta.json, when it has one."""
    try:
        meta = json.loads((session_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = meta.get("session_id") if isinstance(meta, dict) else None
    return value if isinstance(value, str) and value else None


def format_events(events: list[dict]) -> str:
    """One line per event — the transcript as text a model can read."""
    lines = []
    for event in events:
        at = event.get("at") or "-"
        kind = event.get("kind") or "event"
        if kind == "tool":
            body = f"{event.get('tool') or '?'}: {event.get('summary') or ''}"
        elif kind == "result":
            body = f"{'ok' if event.get('ok') else 'failed'}: {event.get('summary') or ''}"
        else:
            body = event.get("text") or ""
        lines.append(f"{at} {kind} {body}".rstrip())
    return "\n".join(lines)


class TranscriptStore:
    """The compressed transcript database (one row per card round)."""

    def __init__(self, db_path: Path | str | None = None,
                 base_dir: Path | None = None):
        """`db_path` wins, then VIBE_HARNESS_TRANSCRIPTS_DB, then
        `<base_dir>/transcripts.db` (BASE_DIR in production, the temp dir in
        tests)."""
        env = os.environ.get(DB_ENV)
        self.db_path = Path(db_path or env or (Path(base_dir or BASE_DIR) / DB_FILENAME))

    @contextmanager
    def _connect(self):
        """One connection per call, never a shared cursor."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def save(self, card_id: str, round: str, session_dir: Path) -> dict:
        """Store one round's session directory; returns its sizes."""
        session_dir = Path(session_dir)
        raw_bytes = _dir_bytes(session_dir)
        blob = _tar_gz(session_dir)
        self._write_row(card_id, round, _session_id(session_dir),
                        datetime.now(timezone.utc).isoformat(),
                        raw_bytes, len(blob), blob)
        return {"raw_bytes": raw_bytes, "gz_bytes": len(blob)}

    def _write_row(self, card_id: str, round: str, session_id: str | None,
                   created_at: str, raw_bytes: int, gz_bytes: int,
                   blob: bytes) -> None:
        """Upsert one row (the only write path: `save` and `copy_from`)."""
        with self._connect() as conn:
            conn.execute(_INSERT_SQL, (card_id, round, session_id, created_at,
                                       raw_bytes, gz_bytes, sqlite3.Binary(blob)))

    def copy_from(self, source: "TranscriptStore",
                  card_ids: set[str] | None = None) -> int:
        """Copy rows (blob included) from another store; returns rows copied.

        Used by the one-time migration that drains the old harness-root store
        into the per-project ones.
        """
        copied = 0
        for row in source.list():
            if card_ids is not None and row["card_id"] not in card_ids:
                continue
            blob = source.get(row["card_id"], row["round"])
            if blob is None:
                continue
            self._write_row(row["card_id"], row["round"], row["session_id"],
                            row["created_at"], row["raw_bytes"],
                            row["gz_bytes"], blob)
            copied += 1
        return copied

    def delete(self, card_id: str, round: str | None = None) -> int:
        """Drop a card's rows (one round when `round` is given); returns the count."""
        sql = "DELETE FROM transcripts WHERE card_id = ?"
        params: tuple = (card_id,)
        if round is not None:
            sql += " AND round = ?"
            params = (card_id, round)
        with self._connect() as conn:
            return conn.execute(sql, params).rowcount

    def remove(self) -> None:
        """Delete the database file and its WAL sidecars (an emptied store)."""
        for suffix in ("", "-wal", "-shm"):
            Path(f"{self.db_path}{suffix}").unlink(missing_ok=True)

    def get(self, card_id: str, round: str) -> bytes | None:
        """The round's tar.gz blob, or None when it was never stored."""
        if not self.db_path.exists():
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT blob FROM transcripts WHERE card_id = ? AND round = ?",
                (card_id, round)).fetchone()
        return bytes(row["blob"]) if row else None

    def extract(self, card_id: str, round: str, dest: Path) -> Path:
        """Write the round's files back out under `dest`; returns `dest/session`."""
        blob = self.get(card_id, round)
        if blob is None:
            raise KeyError(f"no transcript for {card_id} {round}")
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
            tar.extractall(dest, filter="data")
        return dest / SESSION_ARCNAME

    def has(self, card_id: str, round: str) -> bool:
        """True when this round is already stored."""
        if not self.db_path.exists():
            return False
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM transcripts WHERE card_id = ? AND round = ?",
                (card_id, round)).fetchone() is not None

    def list(self, card_id: str | None = None) -> list[dict]:
        """Metadata rows (never the blob), newest first."""
        if not self.db_path.exists():
            return []
        sql = f"SELECT {_META_COLUMNS} FROM transcripts"
        params: tuple = ()
        if card_id is not None:
            sql += " WHERE card_id = ?"
            params = (card_id,)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def stats(self) -> dict:
        """How much the store holds and how well it compressed.

        A read: a store that was never written reports zeros without creating
        its database file (the stats endpoint walks every project).
        """
        if not self.db_path.exists():
            return {"rows": 0, "raw_bytes": 0, "gz_bytes": 0, "ratio": 0.0}
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS rows, COALESCE(SUM(raw_bytes), 0) AS raw_bytes, "
                "COALESCE(SUM(gz_bytes), 0) AS gz_bytes FROM transcripts").fetchone()
        raw_bytes, gz_bytes = row["raw_bytes"], row["gz_bytes"]
        return {"rows": row["rows"], "raw_bytes": raw_bytes, "gz_bytes": gz_bytes,
                "ratio": round(raw_bytes / gz_bytes, 2) if gz_bytes else 0.0}

    def backfill(self, sessions_dir: Path, store_for=None) -> dict:
        """Import every `sessions/<card>/<round>/export/session/` still on disk.

        `store_for(card_id)` picks the store a round belongs to (the runner
        routes a card to its project's store); the default is this store.

        Idempotent: a round already in the store is skipped (and its directory
        removed — the row already holds it). A failed save leaves the directory
        in place, so nothing is ever lost.
        """
        imported = skipped = raw_bytes = gz_bytes = 0
        for session_dir in sorted(Path(sessions_dir).glob("*/*/export/session")):
            if not session_dir.is_dir():
                continue
            card_id = session_dir.parents[2].name
            round_name = session_dir.parents[1].name
            store = store_for(card_id) if store_for is not None else self
            if store.has(card_id, round_name):
                skipped += 1
                shutil.rmtree(session_dir, ignore_errors=True)
                continue
            try:
                sizes = store.save(card_id, round_name, session_dir)
            except Exception as exc:
                print(f"WARNING: could not store transcript {card_id} "
                      f"{round_name}: {exc!r}")
                continue
            imported += 1
            raw_bytes += sizes["raw_bytes"]
            gz_bytes += sizes["gz_bytes"]
            shutil.rmtree(session_dir, ignore_errors=True)
        return {"imported": imported, "skipped": skipped,
                "raw_bytes": raw_bytes, "gz_bytes": gz_bytes}

    def read_events(self, card_id: str, round: str,
                    limit: int = DEFAULT_LIMIT) -> list[dict]:
        """The round's parsed journal events (the last `limit`), from the blob."""
        with tempfile.TemporaryDirectory() as tmp:
            try:
                root = self.extract(card_id, round, Path(tmp))
            except KeyError:
                return []
            return read_activity(root / "journal", 0, limit)["events"]


def project_store(project_path: Path) -> TranscriptStore:
    """The store of one project folder: `<project>/.vibe-harness/transcripts.db`.

    Creating it also keeps the folder out of the project's git — through the
    local-only `.git/info/exclude`, never the tracked `.gitignore`, so the
    user's repo gains no committed change. A folder that is not a repository
    is fine (nothing to exclude). `VIBE_HARNESS_TRANSCRIPTS_DB` still wins: the
    store then lives elsewhere and the project is left untouched.
    """
    project_path = Path(project_path)
    store = TranscriptStore(base_dir=project_path / HARNESS_DIR)
    if store.db_path == project_path / HARNESS_DIR / DB_FILENAME:
        gitops.exclude_from_git(project_path, f"{HARNESS_DIR}/")
    return store
