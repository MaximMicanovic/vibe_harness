"""A mechanical clone detector — the numbers behind "stop growing freely".

Every card adds lines; nothing in the pipeline ever asks whether the change
re-implements something that already exists. This module answers that question
without an LLM: it normalizes every source line, then looks for runs of
``min_lines`` consecutive identical normalized lines that appear in two or more
places. The reviewer gets the report (``report``), the board serves it
(``GET /api/duplication``), and both read the same function — there is no
second, divergent notion of "duplicate".

Pure and dependency-free on purpose: reading files is the only I/O, the result
is deterministic (sorted walk, sorted output), and nothing here imports the
rest of the app.

Normalization is deliberately blunt — strip the whitespace, drop blank lines
and comment-only lines — because the point is to catch a block that was copied
and re-indented, not to build a parser. A line that *starts* with a comment
marker is dropped even when it is really code (a CSS ``* {`` selector, a
``#id`` rule): losing one line only makes the detector slightly less
sensitive, never wrong about the lines it does compare.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

#: The source kinds the detector reads. Anything else (markdown, JSON, images)
#: is not code and would only add noise.
SOURCE_SUFFIXES = (".py", ".js", ".css", ".html")

#: Folders that never hold the project's own source: test fixtures, run
#: artifacts, archived cards, virtualenvs and installed packages.
SKIP_DIRS = frozenset({
    "tests", "sessions", "archives", ".venv", "venv", "node_modules",
    "__pycache__", ".git", ".pytest_cache", ".ruff_cache", ".mypy_cache",
})

#: A line starting with one of these carries no code.
COMMENT_PREFIXES = ("#", "//", "/*", "*/", "*", "<!--", "-->")

#: The default window: six consecutive lines is long enough to be a real block
#: and short enough to catch a copied helper.
DEFAULT_MIN_LINES = 6

#: `+++ b/path` in a unified diff, but only right after its `--- a/path` line —
#: an added line that happens to start with "++ " would otherwise look like a
#: file header.
_NEW_FILE_RE = re.compile(r"^\+\+\+ (?:b/)?(.+)$")


# ----------------------------------------------------------------------
# normalization
# ----------------------------------------------------------------------

def normalize(line: str) -> str | None:
    """The line with its whitespace stripped, or None when it carries no code."""
    text = line.strip()
    if not text or text.startswith(COMMENT_PREFIXES):
        return None
    return text


def _stream(path: Path) -> list[tuple[int, str]]:
    """(1-based line number, normalized line) for every code line in `path`."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for lineno, line in enumerate(text.splitlines(), 1):
        norm = normalize(line)
        if norm is not None:
            out.append((lineno, norm))
    return out


def _source_files(root: Path) -> list[Path]:
    """Every source file under `root`, in a deterministic order."""
    if not root.is_dir():
        return []
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if Path(name).suffix.lower() in SOURCE_SUFFIXES:
                found.append(Path(dirpath) / name)
    return found


# ----------------------------------------------------------------------
# the detector
# ----------------------------------------------------------------------

def _extend(streams: dict[str, list[tuple[int, str]]],
            places: list[tuple[str, int]], length: int) -> int:
    """Grow a match while every occurrence still has the same next line."""
    while True:
        following = set()
        for rel, index in places:
            pairs = streams[rel]
            if index + length >= len(pairs):
                return length
            following.add(pairs[index + length][1])
        if len(following) != 1:
            return length
        length += 1


def _contains(outer: tuple[int, list[tuple[str, int]]],
              inner: tuple[int, list[tuple[str, int]]]) -> bool:
    """True when every occurrence of `inner` sits inside one of `outer`.

    Positions are indexes into the normalized line stream, not line numbers:
    two copies of the same block can sit at different line numbers when one
    side has more comments or blank lines between its lines.
    """
    outer_length, outer_places = outer
    inner_length, inner_places = inner
    spans: dict[str, list[tuple[int, int]]] = {}
    for rel, index in outer_places:
        spans.setdefault(rel, []).append((index, index + outer_length))
    for rel, index in inner_places:
        end = index + inner_length
        if not any(s <= index and end <= e for s, e in spans.get(rel, [])):
            return False
    return True


def _drop_contained(found: list[tuple[int, list[tuple[str, int]]]]
                    ) -> list[tuple[int, list[tuple[str, int]]]]:
    """Keep only maximal clones.

    A 9-line duplicate also matches at offsets 1..3 (each a shorter clone of
    the same block). Those are the same finding, so a clone that lies entirely
    inside another one is dropped.
    """
    unique: dict[tuple, tuple[int, list[tuple[str, int]]]] = {}
    for clone in found:
        length, places = clone
        unique.setdefault((length, tuple(places)), clone)
    ordered = [unique[sig] for sig in sorted(unique)]
    return [clone for clone in ordered
            if not any(other is not clone and _contains(other, clone)
                       for other in ordered)]


def find_clones(root: str | Path, min_lines: int = DEFAULT_MIN_LINES) -> list[dict]:
    """Every duplicated block under `root`, longest first.

    Each clone is ``{"length": N, "occurrences": [{"file": ..., "start": ...}]}``
    with `file` relative to `root` and `start` the 1-based line number of the
    first line of the block. A block that appears twice in one file is a clone
    too — "two or more places" is not "two or more files".
    """
    if min_lines < 1:
        raise ValueError("min_lines must be at least 1")
    root = Path(root)
    streams = {path.relative_to(root).as_posix(): _stream(path)
               for path in _source_files(root)}

    windows: dict[tuple, list[tuple[str, int]]] = {}
    for rel, pairs in streams.items():
        for index in range(len(pairs) - min_lines + 1):
            key = tuple(norm for _, norm in pairs[index:index + min_lines])
            windows.setdefault(key, []).append((rel, index))

    found = []
    for places in windows.values():
        if len(places) < 2:
            continue
        found.append((_extend(streams, places, min_lines), places))

    clones = [{"length": length,
               "occurrences": [{"file": rel, "start": streams[rel][index][0]}
                               for rel, index in places]}
              for length, places in _drop_contained(found)]
    clones.sort(key=lambda c: (-c["length"],
                               c["occurrences"][0]["file"],
                               c["occurrences"][0]["start"]))
    return clones


# ----------------------------------------------------------------------
# the report the review prompt and the endpoint share
# ----------------------------------------------------------------------

def diff_files(diff: str | None) -> list[str]:
    """The paths a unified diff touches (``+++ b/<path>``), in diff order.

    A deletion (``+++ /dev/null``) names no file, so it is skipped: there is
    nothing left to look for clones in.
    """
    lines = (diff or "").splitlines()
    out: list[str] = []
    for index, line in enumerate(lines):
        if not line.startswith("+++ "):
            continue
        if index == 0 or not lines[index - 1].startswith("--- "):
            continue
        path = _NEW_FILE_RE.match(line).group(1).split("\t")[0].strip()
        if not path or path == "/dev/null":
            continue
        if path not in out:
            out.append(path)
    return out


def _wanted(files, root_name: str) -> set[str]:
    """The touched paths, in both the tree-relative and root-relative form.

    A diff names files relative to the repository (``app/one.py``) while the
    detector names them relative to the scanned root (``one.py``); accepting
    both means the caller does not have to know which one it holds.
    """
    wanted: set[str] = set()
    prefix = f"{root_name}/" if root_name else ""
    for raw in files:
        path = str(raw).replace("\\", "/").lstrip("./")
        wanted.add(path)
        if prefix and path.startswith(prefix):
            wanted.add(path[len(prefix):])
    return wanted


def report(root: str | Path, files=None,
           min_lines: int = DEFAULT_MIN_LINES) -> list[dict]:
    """The clones that touch `files` (the whole tree when `files` is None).

    A clone is kept when ANY of its occurrences is in a touched file, and it
    keeps ALL its occurrences: the reviewer has to be able to name the existing
    thing the diff duplicates, which is usually the occurrence outside the
    diff.
    """
    clones = find_clones(root, min_lines=min_lines)
    if files is None:
        return clones
    root = Path(root)
    wanted = _wanted(files, root.name)
    return [clone for clone in clones
            if any(occ["file"] in wanted for occ in clone["occurrences"])]
