"""Tests for app.mdstore — the in-repo YAML-frontmatter reader/writer.

mdstore replaces the python-frontmatter dependency: it splits a leading
`---` YAML block from the markdown body and dumps it back. The on-disk
format must stay byte-compatible with the files python-frontmatter wrote
(existing cards/*.md, archives/*/archive.md), so these tests pin the exact
bytes: sorted YAML keys, quoted ISO timestamps, folded long lines,
unescaped unicode, and no trailing newline after the body.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app import mdstore

# A real card file from the board (cards/retarget-stale-...-f1e7c1.md),
# exactly as the card writer produced it.
EXISTING_CARD = """---
accepted: false
auto_accept: false
auto_run: false
created_at: '2026-11-04T08:00:43.222631+00:00'
depends_on: []
id: f1e7c1
model: null
parent_id: d6b907
project: Desert game
session: null
status: draft
template: null
title: Retarget stale native drive/travel checks at the current noise worldgen
updated_at: '2026-11-04T08:00:43.222631+00:00'
workdir: null
---

Follow-up from card d6b907 (OS port). On macOS, `scripts/test-godot.sh --headless` passes 530/547 checks; the 17 failures are pre-existing at HEAD on every OS (proven platform-independent with a pure `sim::World` repro, see NOTES.md in the d6b907 branch). They are stale hardcoded expectations, not OS bugs:

- `bridge_drive_boost` asserts |vx-128|<=16 (SLOPE_G) on EVERY drive tick, impossible with ACCEL=20 from rest (vx climbs 20,40,60,...). Fix the assertion to match momentum driving (e.g. bounded once near top speed, monotonic climb, position advanced by exactly vx).
- `travel_empty_destination_and_explicit_residency` drives to (39 cells, y) and expects host row 5, but seed 42 settlement 5 sits at (45,104); the Voronoi boundary with settlement 4 (25,32) at y=51 is x~96, so x=39 can never host row 5. Pick a target inside settlement 5's region (or compute the host from positions) and keep all occupancy/personnel/fold assertions.
- `one_session_foreign_crossing_and_return` drives map-west from the seed 42 home (12,56) but no settlement lies west; 0 transfers result. Retarget the drive direction/waypoints to actually cross into a foreign settlement's region (check settlement coordinates for seed 42 first), and re-check the TRAVEL_CHECKPOINT phases. Note the README/AGENTS travel hint (W+A then S+D) may need the same retarget.
- `vehicle_slope_settlement_ground_and_smoothed_airborne_pitch` and `camera_exact_horizontal_smoothed_height_and_recenter` fail too; check whether their assertions still match current slope/camera smoothing behavior.

Constraints: do not change `sim` behavior (no state-hash movement; if a fix needs a sim change, follow `.factory/skills/determinism-change/SKILL.md`). Keep assertions strong — repair coordinates/expectations, never weaken them. Verify with `./scripts/test-godot.sh --headless` on macOS/Linux (and the ps1 twin on Windows): the suite must reach DESERT_NATIVE_TESTS_PASSED. Update docs that quote stale travel directions."""

# A card with a full session dict, unicode and a long folded line.
# SYNTH_TEXT is the exact output python-frontmatter.dumps produced for it.
SYNTH_METADATA = {
    "id": "a1b2c3",
    "title": "Unicode café — naïve résumé ✓",
    "status": "accepted",
    "created_at": "2026-11-04T08:00:43.222631+00:00",
    "updated_at": "2026-11-04T09:00:00.000000+00:00",
    "template": None,
    "auto_run": True,
    "auto_accept": False,
    "accepted": True,
    "project": "Vibe Harness",
    "workdir": None,
    "depends_on": ["ca111d", "e39c52"],
    "parent_id": None,
    "session": {
        "pid": 49868,
        "started_at": "2026-11-03T22:00:06.630248+00:00",
        "finished_at": "2026-11-03T23:00:00+00:00",
        "exit_code": 0,
        "verified": True,
        "model": "mistral-large-4-0-3",
        "tokens": 552022,
        "stop_reason": None,
        "workdir": "/Users/maxim/programming/python/vibe_harness/sessions/8d2128/worktree",
        "log": "/Users/maxim/programming/python/vibe_harness/sessions/8d2128/run-1/session.log",
        "export_dir": "/x/export",
        "review": {"approved": True, "round": 1},
        "reviews": [{"approved": True, "round": 1}],
        "review_rounds": 1,
        "git": None,
        "resolver": None,
        "test_output": "837 passed, 2 skipped\n",
        "mailbox": None,
        "worker_session_id": "3b1c8191",
        "last_reviewed_commit": "aa2f8c59",
        "diff_stat": {"files": 12, "added": 300, "removed": 40},
        "reverted_at": None,
        "revert_commit": None,
        "pr_url": "https://github.com/x/y/pull/7",
        "pr_number": 7,
        "pr_opened_at": None,
        "note": "a very long line that will definitely exceed the default yaml dump width of eighty characters and must be folded exactly like pyyaml folds it",
    },
    "archived": False,
    "archive": None,
    "archived_at": None,
}
SYNTH_BODY = """Body with unicode: héllo wörld — ✓

Second paragraph with `code` and a --- fence inside:

---
not frontmatter
---"""
SYNTH_TEXT = r"""---
accepted: true
archive: null
archived: false
archived_at: null
auto_accept: false
auto_run: true
created_at: '2026-11-04T08:00:43.222631+00:00'
depends_on:
- ca111d
- e39c52
id: a1b2c3
parent_id: null
project: Vibe Harness
session:
  diff_stat:
    added: 300
    files: 12
    removed: 40
  exit_code: 0
  export_dir: /x/export
  finished_at: '2026-11-03T23:00:00+00:00'
  git: null
  last_reviewed_commit: aa2f8c59
  log: /Users/maxim/programming/python/vibe_harness/sessions/8d2128/run-1/session.log
  mailbox: null
  model: mistral-large-4-0-3
  note: a very long line that will definitely exceed the default yaml dump width of
    eighty characters and must be folded exactly like pyyaml folds it
  pid: 49868
  pr_number: 7
  pr_opened_at: null
  pr_url: https://github.com/x/y/pull/7
  resolver: null
  revert_commit: null
  reverted_at: null
  review:
    approved: true
    round: 1
  review_rounds: 1
  reviews:
  - approved: true
    round: 1
  started_at: '2026-11-03T22:00:06.630248+00:00'
  stop_reason: null
  test_output: '837 passed, 2 skipped

    '
  tokens: 552022
  verified: true
  workdir: /Users/maxim/programming/python/vibe_harness/sessions/8d2128/worktree
  worker_session_id: 3b1c8191
status: accepted
template: null
title: Unicode café — naïve résumé ✓
updated_at: '2026-11-04T09:00:00.000000+00:00'
workdir: null
---

Body with unicode: héllo wörld — ✓

Second paragraph with `code` and a --- fence inside:

---
not frontmatter
---"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "card.md"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_reads_frontmatter_and_body(tmp_path):
    path = _write(tmp_path, EXISTING_CARD)
    md, body = mdstore.load(path)
    assert md["id"] == "f1e7c1"
    assert md["title"] == ("Retarget stale native drive/travel checks at the "
                           "current noise worldgen")
    assert md["status"] == "draft"
    assert md["parent_id"] == "d6b907"
    assert md["project"] == "Desert game"
    assert md["depends_on"] == []
    assert md["session"] is None
    assert body.startswith("Follow-up from card d6b907")
    assert body.endswith("quote stale travel directions.")


def test_existing_card_file_round_trips_byte_for_byte(tmp_path):
    """dumps(load(file)) reproduces an existing on-disk card exactly."""
    path = _write(tmp_path, EXISTING_CARD)
    assert mdstore.dumps(*mdstore.load(path)) == EXISTING_CARD


def test_dumps_matches_python_frontmatter_bytes():
    assert mdstore.dumps(SYNTH_METADATA, SYNTH_BODY) == SYNTH_TEXT


def test_load_synthetic_card_round_trip(tmp_path):
    path = _write(tmp_path, SYNTH_TEXT)
    md, body = mdstore.load(path)
    assert md == SYNTH_METADATA
    assert body == SYNTH_BODY
    assert mdstore.dumps(md, body) == SYNTH_TEXT


def test_load_without_frontmatter_returns_empty_metadata(tmp_path):
    path = _write(tmp_path, "just markdown, no fm\n")
    assert mdstore.load(path) == ({}, "just markdown, no fm")


def test_load_with_unclosed_fence_returns_whole_text(tmp_path):
    text = "---\nfoo: bar\nno closing fence\n"
    path = _write(tmp_path, text)
    assert mdstore.load(path) == ({}, text.strip())


def test_load_with_non_dict_frontmatter(tmp_path):
    path = _write(tmp_path, "---\n- a\n- b\n---\nbody\n")
    assert mdstore.load(path) == ({}, "body")


def test_load_with_broken_yaml_raises(tmp_path):
    path = _write(tmp_path, "---\n: : :\n---\nnot yaml: [")
    with pytest.raises(yaml.YAMLError):
        mdstore.load(path)


def test_load_accepts_longer_fences(tmp_path):
    path = _write(tmp_path, "----\na: 1\n----\nbody\n")
    assert mdstore.load(path) == ({"a": 1}, "body")


def test_load_strips_surrounding_whitespace(tmp_path):
    path = _write(tmp_path, "\n---\na: 1\n---\n\n\nhello\n\n\n")
    assert mdstore.load(path) == ({"a": 1}, "hello")


def test_dumps_empty_body():
    assert mdstore.dumps({"a": 1}, "") == "---\na: 1\n---"


def test_dumps_empty_metadata():
    assert mdstore.dumps({}, "body") == "---\n{}\n---\n\nbody"


def test_utf8_round_trip(tmp_path):
    path = _write(tmp_path, "---\ntitle: café\n---\nhéllo — ✓\n")
    md, body = mdstore.load(path)
    assert md == {"title": "café"}
    assert body == "héllo — ✓"
    assert mdstore.dumps(md, body) == "---\ntitle: café\n---\n\nhéllo — ✓"
