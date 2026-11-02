"""The mechanical clone detector (`app/dupes.py`).

Spec (card 26671e): "A pure, dependency-free clone detector: `find_clones(root,
min_lines=6)` normalizes each source line (strip whitespace, drop blank lines
and comment-only lines), then finds windows of `min_lines` consecutive
identical normalized lines that appear in two or more places."

The detector is what makes "stop growing freely" mechanical instead of a
matter of taste: the reviewer gets the same numbers the endpoint serves, so a
diff that copies a block is visible without reading the whole workspace.
"""
from __future__ import annotations

from pathlib import Path

from app import dupes

BLOCK = """\
def compute_total(rows):
    total = 0
    for row in rows:
        if row is None:
            continue
        total += row["amount"]
    return total
"""


def _write(root: Path, name: str, text: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ----------------------------------------------------------------------
# the detector: positive and negative fixtures
# ----------------------------------------------------------------------

def test_finds_a_known_duplicate_in_a_fixture(tmp_path):
    """Two files carrying the same 8-line block are one clone, with the file
    and the 1-based line number of each occurrence."""
    _write(tmp_path, "app/one.py", "import os\n\n" + BLOCK)
    _write(tmp_path, "app/two.py", "import sys\n\n\n" + BLOCK)

    clones = dupes.find_clones(tmp_path)

    assert len(clones) == 1, clones
    clone = clones[0]
    assert clone["length"] == 7
    assert clone["occurrences"] == [
        {"file": "app/one.py", "start": 3},
        {"file": "app/two.py", "start": 4},
    ]


def test_reports_nothing_for_a_clean_fixture(tmp_path):
    """Two files with nothing in common produce no clones at all."""
    _write(tmp_path, "app/one.py", "def a():\n    return 1\n")
    _write(tmp_path, "app/two.py", "def b():\n    return 2\n")

    assert dupes.find_clones(tmp_path) == []


def test_blank_and_comment_lines_do_not_break_a_clone(tmp_path):
    """Normalization drops blank lines and comment-only lines, so the same
    code with different comments and spacing is still one clone."""
    _write(tmp_path, "app/one.py", BLOCK)
    # Same code, re-indented with tabs, with a comment line and a blank line
    # between every line.
    commented = "\n".join(
        "# a comment\n\n" + line.replace("    ", "\t")
        for line in BLOCK.splitlines())
    _write(tmp_path, "app/two.py", commented)

    clones = dupes.find_clones(tmp_path)

    assert len(clones) == 1, clones
    assert clones[0]["length"] == 7


def test_a_run_shorter_than_min_lines_is_not_a_clone(tmp_path):
    """Five identical lines are below the default threshold of six."""
    short = "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"
    _write(tmp_path, "app/one.py", short)
    _write(tmp_path, "app/two.py", short)

    assert dupes.find_clones(tmp_path) == []
    # ...and it IS a clone once the caller lowers the threshold.
    assert len(dupes.find_clones(tmp_path, min_lines=5)) == 1


def test_a_clone_is_extended_to_its_maximal_length(tmp_path):
    """A 9-line duplicate reports length 9, not one clone per 6-line window."""
    block = BLOCK + "def other():\n    return 1\n"
    _write(tmp_path, "app/one.py", block)
    _write(tmp_path, "app/two.py", block)

    clones = dupes.find_clones(tmp_path)

    assert len(clones) == 1, clones
    assert clones[0]["length"] == 9


def test_a_block_copied_twice_inside_one_file_is_a_clone(tmp_path):
    """Two places can be two places in the same file."""
    _write(tmp_path, "app/one.py", BLOCK + "\n\n" + BLOCK)

    clones = dupes.find_clones(tmp_path)

    assert len(clones) == 1, clones
    assert [o["file"] for o in clones[0]["occurrences"]] == ["app/one.py", "app/one.py"]
    assert [o["start"] for o in clones[0]["occurrences"]] == [1, 10]


def test_covers_py_js_css_and_html_only(tmp_path):
    """The four source suffixes are scanned; a markdown copy is not."""
    for name in ("app/a.py", "app/b.js", "app/c.css", "app/d.html"):
        _write(tmp_path, name, BLOCK)
    _write(tmp_path, "app/notes.md", BLOCK)

    clones = dupes.find_clones(tmp_path)

    assert len(clones) == 1, clones
    files = [o["file"] for o in clones[0]["occurrences"]]
    assert files == ["app/a.py", "app/b.js", "app/c.css", "app/d.html"]


def test_skips_tests_sessions_archives_venv_and_node_modules(tmp_path):
    """A copy inside a skipped folder is not reported."""
    _write(tmp_path, "app/one.py", BLOCK)
    _write(tmp_path, "app/two.py", BLOCK)
    for skipped in ("tests", "sessions", "archives", ".venv", "node_modules"):
        _write(tmp_path, f"{skipped}/three.py", BLOCK)

    clones = dupes.find_clones(tmp_path)

    assert len(clones) == 1, clones
    assert [o["file"] for o in clones[0]["occurrences"]] == [
        "app/one.py", "app/two.py"]


def test_is_deterministic(tmp_path):
    """Same tree, same report — twice."""
    _write(tmp_path, "app/one.py", BLOCK)
    _write(tmp_path, "app/two.py", BLOCK)
    _write(tmp_path, "app/three.py", "x = 1\n" + BLOCK)

    assert dupes.find_clones(tmp_path) == dupes.find_clones(tmp_path)


def test_a_missing_root_is_an_empty_report(tmp_path):
    assert dupes.find_clones(tmp_path / "nope") == []


# ----------------------------------------------------------------------
# the report the review prompt and the endpoint share
# ----------------------------------------------------------------------

DIFF = """\
diff --git a/app/one.py b/app/one.py
--- a/app/one.py
+++ b/app/one.py
@@ -1,3 +1,4 @@
 import os
+import sys
diff --git a/app/static/app.js b/app/static/app.js
--- a/app/static/app.js
+++ b/app/static/app.js
@@ -1,2 +1,2 @@
-old
+new
"""


def test_diff_files_reads_the_touched_paths():
    assert dupes.diff_files(DIFF) == ["app/one.py", "app/static/app.js"]


def test_diff_files_ignores_a_deleted_file():
    """`+++ /dev/null` is a deletion: there is no file to look for clones in."""
    diff = "--- a/app/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    assert dupes.diff_files(diff) == []


def test_diff_files_of_no_diff_is_empty():
    assert dupes.diff_files(None) == []
    assert dupes.diff_files("") == []


def test_report_keeps_only_clones_touching_the_given_files(tmp_path):
    """The review prompt gets the clones in the files the diff touches — with
    every occurrence kept, so the reviewer can name the existing duplicate."""
    _write(tmp_path, "app/one.py", BLOCK)
    _write(tmp_path, "app/two.py", BLOCK)
    _write(tmp_path, "app/three.py", "def other():\n    return 1\n" + BLOCK)

    everything = dupes.report(tmp_path)
    assert len(everything) == 1
    assert len(everything[0]["occurrences"]) == 3

    touched = dupes.report(tmp_path, files=["app/one.py"])
    assert len(touched) == 1
    # All three places are reported: the two outside the diff are what the
    # reviewer has to name.
    assert [o["file"] for o in touched[0]["occurrences"]] == [
        "app/one.py", "app/three.py", "app/two.py"]

    assert dupes.report(tmp_path, files=["app/unrelated.py"]) == []


def test_report_accepts_paths_relative_to_the_scanned_root(tmp_path):
    """A diff names `app/one.py`; the detector names `one.py` under `app/`.
    Both forms find the same clone."""
    _write(tmp_path, "app/one.py", BLOCK)
    _write(tmp_path, "app/two.py", BLOCK)

    assert dupes.report(tmp_path / "app", files=["app/one.py"]) == \
        dupes.report(tmp_path / "app", files=["one.py"])
    assert len(dupes.report(tmp_path / "app", files=["app/one.py"])) == 1


def test_report_without_files_is_the_whole_tree(tmp_path):
    _write(tmp_path, "app/one.py", BLOCK)
    _write(tmp_path, "app/two.py", BLOCK)

    assert dupes.report(tmp_path) == dupes.find_clones(tmp_path)
