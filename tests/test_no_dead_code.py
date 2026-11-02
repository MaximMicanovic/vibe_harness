"""The dead-code guard: `ruff check app/` must stay clean.

A vulture + pyflakes + AST-reference sweep found a handful of leftovers (an
unused import, module constants nothing read, a branch-deleting helper the
accept path stopped calling). Removing them once is not enough — the next
unused import would land silently, because nothing in the suite looks at
imports at all.

So this test runs ruff over `app/` with the project's minimal config
(`pyproject.toml`: `F401` unused import, `F841` unused local) and fails on any
finding. It skips when ruff is not installed, the same way the Playwright test
skips — the dev extra installs it, a bare checkout does not.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"


def _find_ruff() -> str | None:
    """The ruff binary: the one in this interpreter's bin dir (the dev extra
    installs it there, and `pytest` is usually run without that dir on PATH),
    else whatever PATH offers."""
    beside_python = Path(sys.executable).parent / "ruff"
    if beside_python.exists():
        return str(beside_python)
    return shutil.which("ruff")


ruff = _find_ruff()
pytestmark = pytest.mark.skipif(ruff is None, reason="ruff is required for the dead-code guard")


def test_ruff_check_app_is_clean():
    """No unused imports, no unused locals anywhere in `app/`."""
    proc = subprocess.run(
        [ruff, "check", str(APP_DIR)],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        "ruff check app/ found dead code:\n" + proc.stdout + proc.stderr
    )


#: Names the sweep removed. They are gone from `app/` for good: each one was
#: either unreferenced or a duplicate of something the app reads directly.
REMOVED = (
    "ARCHIVE_TYPES",
    "NO_VERDICT_REASON",
    "_TOOL_NAMES",
    "delete_card_branch",
    "children_of",
)


def test_the_removed_names_stay_removed():
    """Nothing in `app/` mentions the names the sweep deleted."""
    hits = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            for name in REMOVED:
                if name in line:
                    hits.append(f"{path.relative_to(APP_DIR.parent)}:{lineno}: {line.strip()}")
    assert hits == [], "dead code came back:\n" + "\n".join(hits)
