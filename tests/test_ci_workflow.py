"""The CI workflow must exist and must not silently skip the tool-gated checks.

Issue #13: with no `.github/` at all, the test suite and the ruff dead-code
gate only ran when someone ran them locally — and they SKIP silently when a
tool is missing (`tests/node_check.py` marks the front-end checks
skipif(node is None); `tests/test_no_dead_code.py` skips when ruff is absent).
`.github/workflows/ci.yml` runs both gates on every push/PR with node and ruff
installed, so the skipif guards never trigger in CI. The guards themselves
stay: local runs without the tools may still skip.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


def _workflow() -> dict:
    assert CI_YML.is_file(), (
        f"{CI_YML.relative_to(ROOT)} is missing — add the CI workflow "
        "(push/PR -> checkout, install uv, uv sync --extra dev, install node, "
        "uv run pytest -q, uv run ruff check app/)"
    )
    doc = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), "ci.yml must be a YAML mapping"
    return doc


def _steps(doc: dict) -> list[dict]:
    jobs = doc.get("jobs") or {}
    assert jobs, "ci.yml has no jobs"
    steps: list[dict] = []
    for job in jobs.values():
        steps.extend(job.get("steps") or [])
    assert steps, "ci.yml job has no steps"
    return steps


def _uses(steps: list[dict]) -> list[str]:
    return [str(s.get("uses", "")) for s in steps if s.get("uses")]


def _runs(steps: list[dict]) -> list[str]:
    return [str(s.get("run", "")) for s in steps if s.get("run")]


def _triggers(doc: dict) -> set[str]:
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    on = doc.get("on", doc.get(True))
    if isinstance(on, dict):
        return set(on)
    if isinstance(on, list):
        return set(on)
    return {str(on)} if on else set()


def test_ci_workflow_exists_and_parses():
    _workflow()


def test_ci_runs_on_push_and_pull_request():
    triggers = _triggers(_workflow())
    assert "push" in triggers, f"ci.yml must trigger on push, got {sorted(triggers)}"
    assert "pull_request" in triggers, (
        f"ci.yml must trigger on pull_request, got {sorted(triggers)}")


def test_ci_checks_out_the_repo():
    assert any(u.startswith("actions/checkout@") for u in _uses(_steps(_workflow()))), (
        "ci.yml must have an actions/checkout step")


def test_ci_installs_uv_and_syncs_the_dev_extra():
    steps = _steps(_workflow())
    assert any("setup-uv" in u for u in _uses(steps)), (
        "ci.yml must install uv (uv's own installer, astral-sh/setup-uv)")
    assert any("uv sync" in r and "--extra dev" in r for r in _runs(steps)), (
        "ci.yml must run `uv sync --extra dev` (installs pytest + ruff)")


def test_ci_installs_node_so_the_frontend_checks_run_not_skip():
    steps = _steps(_workflow())
    node_steps = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-node@")]
    assert node_steps, "ci.yml must have an actions/setup-node step"
    assert any(s.get("with", {}).get("node-version") for s in node_steps), (
        "the setup-node step must pin a node-version so the ~23 node checks "
        "in tests/ run in CI instead of skipif(node is None)")


def test_ci_fails_loudly_if_node_or_ruff_is_missing():
    runs = _runs(_steps(_workflow()))
    assert any("node --version" in r for r in runs), (
        "ci.yml must verify node is present (e.g. `node --version`) so a missing "
        "node fails the build instead of silently skipping the front-end checks")
    assert any("ruff --version" in r for r in runs), (
        "ci.yml must verify ruff is present (e.g. `uv run ruff --version`) so a "
        "missing ruff fails the build instead of silently skipping the dead-code gate")


def test_ci_runs_pytest_and_the_ruff_dead_code_gate():
    runs = _runs(_steps(_workflow()))
    assert any(r.strip() == "uv run pytest -q" for r in runs), (
        "ci.yml must run `uv run pytest -q`")
    assert any("uv run ruff check app/" in r for r in runs), (
        "ci.yml must run `uv run ruff check app/`")


def test_the_local_skip_guards_stay_in_place():
    """CI must not be green by weakening the tests: the skipif guards that let
    a bare local checkout skip stay, and CI installs the tools instead."""
    node_check = (ROOT / "tests" / "node_check.py").read_text(encoding="utf-8")
    assert "skipif" in node_check, "tests/node_check.py lost its skipif guard"
    dead_code = (ROOT / "tests" / "test_no_dead_code.py").read_text(encoding="utf-8")
    assert "skipif" in dead_code, "tests/test_no_dead_code.py lost its skipif guard"
