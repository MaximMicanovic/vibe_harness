"""Compressed session transcripts in SQLite.

Every round of every card leaves a vibe session directory behind
(``sessions/<card>/<round>/export/session/``: journal, chunks, tool results).
It is the only record of what a model actually did, so it is worth keeping —
but as loose files it was ~1 GB of the harness's 1.1 GB ``sessions/`` tree, and
nothing in ``app/`` ever read it back.

This module keeps those transcripts in one SQLite database as tar.gz blobs:

- ``save()`` tars the directory, gzips it and stores the blob (``INSERT OR
  REPLACE`` on ``(card_id, round)``);
- ``get()`` / ``extract()`` hand it back for debugging;
- ``list()`` / ``stats()`` report metadata only — the blob never leaves the
  row unless it is asked for;
- ``backfill()`` imports the directories that predate the store.

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

from app.activity import DEFAULT_LIMIT, read_activity
from app.config import BASE_DIR

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
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO transcripts "
                "(card_id, round, session_id, created_at, raw_bytes, gz_bytes, blob) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (card_id, round, _session_id(session_dir),
                 datetime.now(timezone.utc).isoformat(),
                 raw_bytes, len(blob), sqlite3.Binary(blob)))
        return {"raw_bytes": raw_bytes, "gz_bytes": len(blob)}

    def get(self, card_id: str, round: str) -> bytes | None:
        """The round's tar.gz blob, or None when it was never stored."""
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
        with self._connect() as conn:
            return conn.execute(
                "SELECT 1 FROM transcripts WHERE card_id = ? AND round = ?",
                (card_id, round)).fetchone() is not None

    def list(self, card_id: str | None = None) -> list[dict]:
        """Metadata rows (never the blob), newest first."""
        sql = f"SELECT {_META_COLUMNS} FROM transcripts"
        params: tuple = ()
        if card_id is not None:
            sql += " WHERE card_id = ?"
            params = (card_id,)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params)]

    def stats(self) -> dict:
        """How much the store holds and how well it compressed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS rows, COALESCE(SUM(raw_bytes), 0) AS raw_bytes, "
                "COALESCE(SUM(gz_bytes), 0) AS gz_bytes FROM transcripts").fetchone()
        raw_bytes, gz_bytes = row["raw_bytes"], row["gz_bytes"]
        return {"rows": row["rows"], "raw_bytes": raw_bytes, "gz_bytes": gz_bytes,
                "ratio": round(raw_bytes / gz_bytes, 2) if gz_bytes else 0.0}

    def backfill(self, sessions_dir: Path) -> dict:
        """Import every `sessions/<card>/<round>/export/session/` still on disk.

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
            if self.has(card_id, round_name):
                skipped += 1
                shutil.rmtree(session_dir, ignore_errors=True)
                continue
            try:
                sizes = self.save(card_id, round_name, session_dir)
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
