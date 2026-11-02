"""Compressed session transcripts: the SQLite store, its API and the finalize hook.

The store keeps every round's vibe session directory (journal + chunks) as one
tar.gz blob in `transcripts.db`, so the transcripts stay available for
debugging without costing ~1 GB of loose files. Nothing here may ever fail a
round: a broken store leaves the directory on disk and the run carries on.
"""
from __future__ import annotations

import io
import json
import sqlite3
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.archives import ArchiveStore
from app.cards import CardStore
from app.config import Config
from app.main import create_app
from app.projects import ProjectStore
from app.runner import SessionRunner
from app.settings import SettingsStore
from app.transcripts import TranscriptStore, format_events

#: A journal line `app.activity.parse_journal_line` turns into a text event.
JOURNAL_LINE = json.dumps({"payload": {"delta": [{
    "entry": {"type": "message", "role": "assistant",
              "content": [{"text": "hello from the transcript"}],
              "createdAt": 1700000000000}}]}})

#: A tool call + its result, the other two event kinds.
TOOL_LINE = json.dumps({"payload": {
    "kind": "tool",
    "request": {"call": {"name": "file_system.bash",
                         "arguments": {"command": "ls -la"}}}}})


def make_session_dir(root: Path, name: str = "session") -> Path:
    """A stand-in for `sessions/<card>/<round>/export/session`."""
    session = root / name
    (session / "journal").mkdir(parents=True)
    (session / "journal" / "0000000000000001.jsonl").write_text(JOURNAL_LINE + "\n")
    (session / "journal" / "0000000000000002.jsonl").write_text(TOOL_LINE + "\n")
    (session / "chunks").mkdir()
    (session / "chunks" / "0001.bin").write_bytes(b"x" * 4096)
    (session / "meta.json").write_text(json.dumps({"session_id": "sess-1"}))
    return session


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


@pytest.fixture()
def store(tmp_path) -> TranscriptStore:
    return TranscriptStore(tmp_path / "transcripts.db")


# ------------------------------------------------------------------ the store

def test_save_get_extract_round_trip(store, tmp_path):
    session = make_session_dir(tmp_path / "round")
    sizes = store.save("card1", "run-1", session)

    assert sizes["raw_bytes"] == dir_bytes(session)
    blob = store.get("card1", "run-1")
    assert isinstance(blob, bytes)
    assert sizes["gz_bytes"] == len(blob)
    assert sizes["gz_bytes"] < sizes["raw_bytes"]

    dest = tmp_path / "out"
    root = store.extract("card1", "run-1", dest)
    assert root == dest / "session"
    assert (root / "journal" / "0000000000000001.jsonl").read_text() == JOURNAL_LINE + "\n"
    assert (root / "chunks" / "0001.bin").read_bytes() == b"x" * 4096
    assert json.loads((root / "meta.json").read_text())["session_id"] == "sess-1"


def test_get_unknown_round_is_none(store):
    assert store.get("nope", "run-1") is None


def test_save_replaces_the_same_round(store, tmp_path):
    session = make_session_dir(tmp_path / "round")
    store.save("card1", "run-1", session)
    (session / "meta.json").write_text(json.dumps({"session_id": "sess-2"}))
    store.save("card1", "run-1", session)

    rows = store.list("card1")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "sess-2"


def test_list_returns_metadata_never_the_blob(store, tmp_path):
    store.save("card1", "run-1", make_session_dir(tmp_path / "a"))
    store.save("card1", "review-1", make_session_dir(tmp_path / "b"))
    store.save("card2", "run-1", make_session_dir(tmp_path / "c"))

    rows = store.list("card1")
    assert {r["round"] for r in rows} == {"run-1", "review-1"}
    for row in rows:
        assert "blob" not in row
        assert row["card_id"] == "card1"
        assert row["raw_bytes"] > 0 and row["gz_bytes"] > 0
        assert row["created_at"]
    assert len(store.list()) == 3


def test_stats_reports_rows_and_compression_ratio(store, tmp_path):
    assert store.stats() == {"rows": 0, "raw_bytes": 0, "gz_bytes": 0, "ratio": 0.0}

    first = store.save("card1", "run-1", make_session_dir(tmp_path / "a"))
    second = store.save("card1", "run-2", make_session_dir(tmp_path / "b"))
    stats = store.stats()
    assert stats["rows"] == 2
    assert stats["raw_bytes"] == first["raw_bytes"] + second["raw_bytes"]
    assert stats["gz_bytes"] == first["gz_bytes"] + second["gz_bytes"]
    assert stats["ratio"] == round(stats["raw_bytes"] / stats["gz_bytes"], 2)


def test_env_var_overrides_the_database_path(tmp_path, monkeypatch):
    target = tmp_path / "elsewhere" / "custom.db"
    monkeypatch.setenv("VIBE_HARNESS_TRANSCRIPTS_DB", str(target))
    assert TranscriptStore().db_path == target
    # An explicit path still wins over the env var.
    assert TranscriptStore(tmp_path / "explicit.db").db_path == tmp_path / "explicit.db"


def test_store_runs_in_wal_mode(store, tmp_path):
    store.save("card1", "run-1", make_session_dir(tmp_path / "a"))
    with sqlite3.connect(store.db_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_backfill_imports_and_removes_the_directories(store, tmp_path):
    sessions = tmp_path / "sessions"
    first = make_session_dir(sessions / "card1" / "run-1" / "export")
    second = make_session_dir(sessions / "card1" / "review-1" / "export")
    # Not a transcript: no export/session directory.
    (sessions / "card2" / "run-1" / "export").mkdir(parents=True)
    (sessions / "card2" / "run-1" / "export" / "export.json").write_text("{}")
    expected_raw = dir_bytes(first) + dir_bytes(second)

    result = store.backfill(sessions)

    assert result["imported"] == 2
    assert result["skipped"] == 0
    assert result["raw_bytes"] == expected_raw
    assert result["gz_bytes"] > 0
    assert not first.exists() and not second.exists()
    # export.json is what the harness reads: it stays.
    assert (sessions / "card2" / "run-1" / "export" / "export.json").exists()
    assert {r["round"] for r in store.list("card1")} == {"run-1", "review-1"}


def test_backfill_is_idempotent(store, tmp_path):
    sessions = tmp_path / "sessions"
    make_session_dir(sessions / "card1" / "run-1" / "export")
    assert store.backfill(sessions)["imported"] == 1

    # A second pass over the same tree: nothing left to import, nothing doubled.
    again = store.backfill(sessions)
    assert again == {"imported": 0, "skipped": 0, "raw_bytes": 0, "gz_bytes": 0}
    assert len(store.list("card1")) == 1

    # A directory that reappears for an already-stored round is skipped (and
    # removed — the row already holds it), never duplicated.
    make_session_dir(sessions / "card1" / "run-1" / "export")
    third = store.backfill(sessions)
    assert third["imported"] == 0 and third["skipped"] == 1
    assert not (sessions / "card1" / "run-1" / "export" / "session").exists()
    assert len(store.list("card1")) == 1


def test_backfill_leaves_a_failed_save_in_place(store, tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    session = make_session_dir(sessions / "card1" / "run-1" / "export")

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(store, "save", boom)
    result = store.backfill(sessions)
    assert result["imported"] == 0
    assert session.exists()


def test_read_events_parses_the_stored_journal(store, tmp_path):
    store.save("card1", "run-1", make_session_dir(tmp_path / "a"))
    events = store.read_events("card1", "run-1")
    assert [e["kind"] for e in events] == ["text", "tool"]
    assert events[0]["text"] == "hello from the transcript"
    assert events[1]["tool"] == "file_system.bash"
    # Unknown round: no events, no exception.
    assert store.read_events("card1", "run-9") == []


def test_read_events_is_capped(store, tmp_path):
    session = make_session_dir(tmp_path / "a")
    lines = "\n".join(JOURNAL_LINE for _ in range(50)) + "\n"
    (session / "journal" / "0000000000000001.jsonl").write_text(lines)
    store.save("card1", "run-1", session)
    assert len(store.read_events("card1", "run-1", limit=5)) == 5


def test_format_events_renders_text_tool_and_result_lines():
    text = format_events([
        {"kind": "text", "at": "2026-01-01T00:00:00+00:00", "text": "hi"},
        {"kind": "tool", "at": None, "tool": "file_system.bash", "summary": "ls"},
        {"kind": "result", "at": None, "ok": False, "summary": "boom"},
    ])
    assert "2026-01-01T00:00:00+00:00 text hi" in text
    assert "tool file_system.bash: ls" in text
    assert "result failed: boom" in text


# -------------------------------------------------------------------- the API

FAKE_VIBE_TRANSCRIPT = """#!/bin/sh
# Fake vibe: writes a session transcript next to export.json, like the real CLI.
workdir=""
outdir=""
agent=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2;;
    --output-dir) outdir="$2"; shift 2;;
    --agent) agent="$2"; shift 2;;
    *) shift;;
  esac
done
mkdir -p "$workdir" "$outdir/session/journal" "$outdir/session/chunks"
echo 'print("hi")' > "$workdir/hello.py"
echo '{"session_id": "fake-session"}' > "$outdir/session/meta.json"
echo '{"payload": {"delta": [{"entry": {"type": "message", "role": "assistant", "content": [{"text": "hello from the transcript"}], "createdAt": 1700000000000}}]}}' > "$outdir/session/journal/0000000000000001.jsonl"
head -c 4096 /dev/zero | tr '\\0' 'x' > "$outdir/session/chunks/0001.bin"
cat > "$outdir/export.json" <<'EOF'
{"usage": {"total_tokens": 42}, "config": {"active_model": "fake-model"}, "stop_reason": "end_turn", "session_id": "fake-session"}
EOF
if [ "$agent" = "ask" ]; then
  echo "VERDICT: APPROVED"
fi
exit 0
"""


def make_env(tmp_path: Path, script: str = FAKE_VIBE_TRANSCRIPT) -> SimpleNamespace:
    cards_dir = tmp_path / "cards"
    sessions_dir = tmp_path / "sessions"
    store = CardStore(cards_dir)
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(script)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30)
    projects = ProjectStore(tmp_path / "projects.json")
    settings = SettingsStore(tmp_path / "settings.json")
    runner = SessionRunner(store, config, sessions_dir=sessions_dir,
                           projects=projects, settings=settings)
    archives = ArchiveStore(tmp_path / "archives", store, projects)
    app = create_app(config=config, store=store, runner=runner, projects=projects,
                     archives=archives, settings=settings)
    return SimpleNamespace(client=TestClient(app), store=store, runner=runner,
                           sessions_dir=sessions_dir, tmp_path=tmp_path,
                           transcripts=runner.transcripts)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def create_card(client, title="Transcripts", body="do a thing") -> dict:
    r = client.post("/api/cards", json={"title": title, "body": body})
    assert r.status_code == 201, r.text
    return r.json()


def wait_for_status(client, card_id, statuses, timeout=15.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/cards/{card_id}")
        assert r.status_code == 200, r.text
        if r.json()["status"] in statuses:
            return r.json()
        time.sleep(0.05)
    raise AssertionError(f"card {card_id} did not reach {statuses} in {timeout}s")


def test_transcript_stats_endpoint(env):
    card = create_card(env.client)
    env.transcripts.save(card["id"], "run-1",
                         make_session_dir(env.tmp_path / "handmade"))

    r = env.client.get("/api/transcripts/stats")
    assert r.status_code == 200
    stats = r.json()
    assert stats["rows"] == 1
    assert stats["raw_bytes"] > stats["gz_bytes"] > 0
    assert stats["ratio"] == round(stats["raw_bytes"] / stats["gz_bytes"], 2)


def test_card_transcripts_endpoint(env):
    card = create_card(env.client)
    env.transcripts.save(card["id"], "run-1",
                         make_session_dir(env.tmp_path / "handmade"))

    r = env.client.get(f"/api/cards/{card['id']}/transcripts")
    assert r.status_code == 200
    rows = r.json()
    assert [row["round"] for row in rows] == ["run-1"]
    assert "blob" not in rows[0]

    assert env.client.get("/api/cards/deadbe/transcripts").status_code == 404


def test_transcript_download_is_a_tar_gz(env):
    card = create_card(env.client)
    env.transcripts.save(card["id"], "run-1",
                         make_session_dir(env.tmp_path / "handmade"))

    r = env.client.get(f"/api/cards/{card['id']}/transcripts/run-1")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert "attachment" in r.headers["content-disposition"]
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
        names = tar.getnames()
    assert "session/journal/0000000000000001.jsonl" in names
    assert "session/meta.json" in names

    assert env.client.get(f"/api/cards/{card['id']}/transcripts/run-9").status_code == 404


def test_transcript_events_endpoint(env):
    card = create_card(env.client)
    env.transcripts.save(card["id"], "run-1",
                         make_session_dir(env.tmp_path / "handmade"))

    r = env.client.get(f"/api/cards/{card['id']}/transcripts/run-1/events")
    assert r.status_code == 200
    body = r.json()
    assert [e["kind"] for e in body["events"]] == ["text", "tool"]
    assert "hello from the transcript" in body["text"]
    # A round that was never stored is a 404, like the download.
    assert env.client.get(
        f"/api/cards/{card['id']}/transcripts/run-9/events").status_code == 404


def test_backfill_endpoint(env):
    card = create_card(env.client)
    session = make_session_dir(env.sessions_dir / card["id"] / "run-1" / "export")

    r = env.client.post("/api/transcripts/backfill")
    assert r.status_code == 200
    result = r.json()
    assert result["imported"] == 1
    assert result["raw_bytes"] > result["gz_bytes"] > 0
    assert not session.exists()
    assert env.client.get(f"/api/cards/{card['id']}/transcripts").json()[0]["round"] == "run-1"

    # Idempotent: a second call imports nothing.
    again = env.client.post("/api/transcripts/backfill").json()
    assert again["imported"] == 0


# ------------------------------------------------------- the finalize hook

def test_finalize_stores_the_transcript_and_removes_the_directory(env):
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/run")
    final = wait_for_status(env.client, card["id"], {"done", "failed"})
    assert final["status"] == "done", final

    run_dir = env.sessions_dir / card["id"] / "run-1"
    assert not (run_dir / "export" / "session").exists()
    # export.json is what the harness reads: it must survive.
    assert (run_dir / "export" / "export.json").exists()
    # Token accounting is unchanged by the capture.
    assert final["session"]["tokens"] == 42
    assert final["session"]["model"] == "fake-model"

    rows = env.transcripts.list(card["id"])
    assert {row["round"] for row in rows} == {"run-1", "review-1"}
    assert all(row["session_id"] == "fake-session" for row in rows)
    # The stored blob still carries the journal.
    events = env.transcripts.read_events(card["id"], "run-1")
    assert events[0]["text"] == "hello from the transcript"


def test_failed_save_keeps_the_directory_and_the_round_succeeds(env, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(env.transcripts, "save", boom)
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/run")
    final = wait_for_status(env.client, card["id"], {"done", "failed"})

    assert final["status"] == "done", final
    # Nothing was lost: the transcript is still on disk.
    assert (env.sessions_dir / card["id"] / "run-1" / "export" / "session").is_dir()
    assert env.transcripts.list(card["id"]) == []


def test_resolver_outcome_stores_the_transcript(env):
    """The merge resolver's round is captured too (its own call site)."""
    card = create_card(env.client)
    rdir = env.sessions_dir / card["id"] / "resolve-1"
    make_session_dir(rdir / "export")
    (rdir / "export" / "export.json").write_text('{"usage": {"total_tokens": 7}}')

    # exit code 1: the resolver gave up, so the card is not concluded — the
    # transcript capture must happen regardless of the outcome.
    env.runner._record_resolver_outcome(
        card["id"], {"proc": SimpleNamespace(wait=lambda: 1), "round": 1})

    assert not (rdir / "export" / "session").exists()
    assert (rdir / "export" / "export.json").exists()
    assert [row["round"] for row in env.transcripts.list(card["id"])] == ["resolve-1"]
