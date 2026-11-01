"""Best-effort LIVE token usage of a running vibe session (spec: live per-card
and total token display with low performance impact).

Vibe 3.x appends a journal under
``$VIBE_HOME/logs/session/unified/<session-id>/journal/*.jsonl`` *while* a
session runs. Its projection records carry the session's cumulative usage as
``... "session": {"contextUsage": {"totalTokens": N, "inputTokens": …,
"outputTokens": …, "cachedInputTokens": …}}`` — the per-type breakdown behind
the hover tooltip on the token counters. Active sessions
are listed as ``$VIBE_HOME/logs/session/active/<session-id>.lock.json`` with a
``process_id`` — the harness maps the pid of the process it spawned to the
journal via that lock (directly, or through the spawned process tree, since
the lock may name a child like `vibe-app-server`).

Performance: scans read only the bytes appended since the last scan (per-file
offsets), at most once per ``LIVE_USAGE_SCAN_INTERVAL`` per live card, and only
while a session is actually running. When nothing is live, this module does
zero I/O. Everything is defensive: a missing file, a vibe upgrade changing the
journal schema, or an unreadable line just means "no live data", and the
dashboard falls back to the authoritative ``export.json`` numbers recorded
when the session ends.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def vibe_home() -> Path:
    """Vibe's home dir — same resolution as the vibe CLI (VIBE_HOME or ~/.vibe).

    Read from the environment on every call so tests can point it at a fake
    home; the spawned vibe processes inherit the same env, so this is also the
    home they write to.
    """
    return Path(os.environ.get("VIBE_HOME") or Path.home() / ".vibe").expanduser()


def _active_locks(home: Path) -> list[tuple[str, int]]:
    """(session_id, process_id) for every active non-child session lock."""
    active = home / "logs" / "session" / "active"
    locks: list[tuple[str, int]] = []
    try:
        files = sorted(active.glob("*.lock.json"))
    except OSError:
        return locks
    for lock in files:
        sid = lock.name[: -len(".lock.json")]
        if sid.startswith("child-"):
            continue  # sessions spawned from inside another session
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("process_id"), int):
            locks.append((sid, data["process_id"]))
    return locks


def _children_map() -> dict[int, list[int]]:
    """pid -> child pids from the process table (one cheap `ps` call)."""
    try:
        out = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return {}
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return children


def _descendants(pid: int, children: dict[int, list[int]], limit: int = 500) -> set[int]:
    """All descendant pids of `pid` (BFS over the process tree, cycle-safe;
    the root itself is never included)."""
    seen: set[int] = set()
    visited = {pid}
    stack = [pid]
    while stack and len(seen) < limit:
        cur = stack.pop()
        for child in children.get(cur, ()):
            if child not in visited:
                visited.add(child)
                seen.add(child)
                stack.append(child)
    return seen


def find_session_id_by_pid(home: Path, pid: int,
                           children: dict[int, list[int]] | None = None) -> str | None:
    """The active vibe session whose process tree contains `pid`, or None.

    The lock's `process_id` is not always the process we spawned: the `vibe`
    launcher may spawn `vibe-rs`, which in turn spawns `vibe-app-server` — and
    the lock records the app-server pid. So a direct pid match is tried first,
    then any lock whose pid is a *descendant* of `pid` (the harness spawns vibe
    with start_new_session, so the whole tree is ours).
    """
    locks = _active_locks(home)
    for sid, lock_pid in locks:
        if lock_pid == pid:
            return sid
    if children is None:
        children = _children_map()
    desc = _descendants(pid, children)
    for sid, lock_pid in locks:
        if lock_pid in desc:
            return sid
    return None


def _context_usage(obj) -> dict | None:
    """Token usage of a decoded record: the `contextUsage` dict with the highest
    `totalTokens`, normalized to {"total", "input", "output", "cached"}.

    Real vibe 3.x journals carry `contextUsage: {inputTokens, outputTokens,
    cachedInputTokens, totalTokens, ...}`. Missing fields come back as None
    (unknown), never as 0. Returns None when the record has no usable usage.
    """
    best_total: int | None = None
    best_usage: dict | None = None
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            cu = cur.get("contextUsage")
            if isinstance(cu, dict):
                t = cu.get("totalTokens")
                if isinstance(t, (int, float)) and not isinstance(t, bool):
                    t = int(t)
                    if best_total is None or t > best_total:
                        best_total = t
                        best_usage = cu
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    if best_total is None or best_usage is None:
        return None

    def num(key: str) -> int | None:
        v = best_usage.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return int(v)
        return None

    return {"total": best_total,
            "input": num("inputTokens"),
            "output": num("outputTokens"),
            "cached": num("cachedInputTokens")}


def scan_journal_dir(journal_dir: Path, offsets: dict[str, int]) -> dict | None:
    """Scan newly appended bytes of every journal file for cumulative usage.

    `offsets` maps file name -> bytes already consumed and is updated in place:
    only complete lines (up to the last newline) are consumed, so a partially
    written line is re-read on the next scan. Returns the usage dict (see
    `_context_usage`) of the record with the max `totalTokens` seen in the new
    bytes, or None when nothing usable was found.
    """
    best: dict | None = None
    try:
        files = sorted(journal_dir.glob("*.jsonl"))
    except OSError:
        return None
    for f in files:
        try:
            size = f.stat().st_size
        except OSError:
            continue
        off = offsets.get(f.name, 0)
        if size <= off:
            continue
        try:
            with open(f, "rb") as fh:
                fh.seek(off)
                data = fh.read()
        except OSError:
            continue
        cut = data.rfind(b"\n")
        if cut < 0:
            continue  # no complete new line yet — leave the offset alone
        offsets[f.name] = off + cut + 1
        for line in data[: cut + 1].split(b"\n"):
            if not line or b'"contextUsage"' not in line:
                continue  # cheap pre-filter: skip lines without usage state
            try:
                rec = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            usage = _context_usage(rec)
            if usage is not None and (best is None or usage["total"] > best["total"]):
                best = usage
    return best


def scan_session_journal(home: Path, session_id: str, offsets: dict[str, int]) -> dict | None:
    """Scan the live journal of one vibe session; see scan_journal_dir."""
    return scan_journal_dir(
        home / "logs" / "session" / "unified" / session_id / "journal", offsets)
