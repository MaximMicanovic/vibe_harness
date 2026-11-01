"""Per-project harness files: git init + agent.md + route.md.

When a project is registered ("opening a new project"), the harness sets up its
folder so every vibe session that works there has context and the folder is a
git repo from day one:

- ``git init`` (via gitops.ensure_repo) with the initial commit folding in
  ``.gitignore``, ``agent.md`` and ``route.md`` — the work tree starts clean.
- ``agent.md`` — a MANAGED rules file for agents working in this folder (it
  carries a marker comment). Re-synced on every server start so rule updates
  propagate; remove the marker to make it user-owned (never overwritten).
- ``route.md`` — starts EMPTY and is agent-owned: the harness only creates it
  if missing and never modifies an existing one. The agent.md rules tell every
  agent to keep route.md up to date when a change is routing/token-relevant.

In existing git repos, new/updated files are committed immediately (just those
files) so the tree stays clean for card runs.
"""
from __future__ import annotations

from pathlib import Path

from app import gitops

ROUTE_MD_FILENAME = "route.md"     # per-project, starts empty, agent-owned
AGENT_MD_FILENAME = "agent.md"     # managed rules file for agents
AGENT_MD_MARKER = "<!-- vibe-harness:agent.md managed -->"

# Rules handed to every vibe session working in a project folder. Tests
# monkeypatch AGENT_MD_BODY to simulate rule updates.
AGENT_MD_BODY = """# agent.md — rules for agents working in this project

This folder is a **vibe-harness** project. Dashboard cards are specs, and agent
sessions (implement / fix / review / merge-resolve) work in this folder.

Rules:

- **Always keep `route.md` up to date when relevant.** If your change affects
  how work is routed through this project or how tokens are spent — pipeline
  order, model/agent choice, test gates, tool allowlists, guardrails, cost or
  time caps — update `route.md` in this folder to reflect it (create the file
  if it is missing). Keep it short and factual.
- Run the project's test suite before finishing. The harness's test gate runs
  it too, and a red suite rejects the round back to a fixer.
- Work autonomously; when something is ambiguous, make a reasonable choice and
  record it in `NOTES.md`.
"""


def render_agent_md() -> str:
    """The managed agent.md content: marker + rules."""
    return f"{AGENT_MD_MARKER}\n\n{AGENT_MD_BODY}"


def ensure_project_files(folder: Path) -> dict[str, str]:
    """Create the project's route.md (empty, once) and sync agent.md (managed).

    Returns {"route_md": "created"|"exists", "agent_md": "created"|"updated"|
    "unchanged"|"custom"}. An existing route.md is NEVER modified — it is
    agent-owned.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)

    route_target = folder / ROUTE_MD_FILENAME
    route_status = "exists"
    if not route_target.exists():
        route_target.write_text("", encoding="utf-8")
        route_status = "created"

    agent_target = folder / AGENT_MD_FILENAME
    content = render_agent_md()
    if not agent_target.exists():
        agent_target.write_text(content, encoding="utf-8")
        agent_status = "created"
    else:
        try:
            existing = agent_target.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        if AGENT_MD_MARKER not in existing:
            agent_status = "custom"          # user-owned: never overwrite
        elif existing != content:
            agent_target.write_text(content, encoding="utf-8")
            agent_status = "updated"
        else:
            agent_status = "unchanged"

    return {"route_md": route_status, "agent_md": agent_status}


def commit_project_files_if_repo(folder: Path) -> bool:
    """Commit route.md/agent.md (only those files) when `folder` is in a git
    repo that has commits, keeping the work tree clean for card runs. Returns
    True when a commit was made.

    Empty repos (no HEAD yet) are skipped: the files join the initial commit
    that ensure_repo creates.
    """
    folder = Path(folder)
    root = gitops.repo_root(folder)
    if root is None:
        return False
    if gitops.run_git(["rev-parse", "--verify", "HEAD"], root, check=False).returncode != 0:
        return False
    status = gitops.run_git(
        ["status", "--porcelain", "--", ROUTE_MD_FILENAME, AGENT_MD_FILENAME],
        root, check=False)
    if not status.stdout.strip():
        return False
    # Repo-local identity fallback, same rule as ensure_repo.
    if not gitops.run_git(["config", "user.email"], root, check=False).stdout.strip():
        gitops.run_git(["config", "user.email", gitops.GIT_IDENTITY_EMAIL], root)
    if not gitops.run_git(["config", "user.name"], root, check=False).stdout.strip():
        gitops.run_git(["config", "user.name", gitops.GIT_IDENTITY_NAME], root)
    gitops.run_git(["add", "--", ROUTE_MD_FILENAME, AGENT_MD_FILENAME], root)
    gitops.run_git(["commit", "-q", "-m", "vibe-harness: sync project files "
                                         "(agent.md, route.md)"], root)
    return True


def setup_project(folder: Path, *, init_repo: bool) -> dict:
    """Full setup when a project is registered: harness files + git repo.

    With init_repo=True the folder is git-init'ed (initial commit folds in
    .gitignore + agent.md + route.md). With init_repo=False an existing repo
    just gets the files committed; a non-repo folder only gets the files (the
    repo is created on the first card run).
    """
    folder = Path(folder)
    statuses = ensure_project_files(folder)
    if init_repo:
        # Idempotent: inits a plain folder (initial commit folds in .gitignore
        # + agent.md + route.md) and creates the initial commit for a
        # pre-existing empty repo.
        root = gitops.ensure_repo(folder)
    else:
        root = gitops.repo_root(folder)
    if root is not None:
        # Existing repo with commits: commit just the harness files so the
        # tree stays clean. (Fresh/empty repos already committed them above.)
        commit_project_files_if_repo(folder)
    statuses["repo"] = str(root) if root is not None else None
    return statuses


def sync_all_projects(projects) -> dict[str, dict]:
    """Startup sync: make sure every registered project folder has an (empty)
    route.md and a managed agent.md, committed when the folder is a repo.
    Never raises; per-project failures are reported as {"error": ...}."""
    results: dict[str, dict] = {}
    for p in projects.list():
        try:
            results[p["name"]] = setup_project(Path(p["path"]), init_repo=False)
        except Exception as exc:
            results[p["name"]] = {"error": str(exc)}
    return results
