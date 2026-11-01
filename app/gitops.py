"""Git workflow for card runs ("ship it").

When a card runs in a real work folder (project folder or workdir override —
not the isolated sandbox), the run happens on its own branch, checked out in
a per-card LINKED WORKTREE (``sessions/<id>/worktree``) — the project folder
itself (the repo's main worktree) stays on its base branch, so several cards
in the same repository can run concurrently:

1. ``ensure_repo`` — ``git init`` the folder if it is not a repository yet
   (with an initial empty commit so the base branch exists as a real commit),
   and set a repo-local commit identity if none is configured.
2. ``prepare_card_branch`` — stash leftover uncommitted changes in the MAIN
   worktree (first run or re-run — e.g. left behind by an interrupted previous
   run; the stash is kept for manual recovery), drop any stale registered
   worktree at the card's worktree path, then
   ``git worktree add <worktree_path> -B <prefix>/<card_id> <base>`` so the
   card branch is checked out in the linked worktree only.
3. ``commit_workspace`` — after each successful implementation round, stage
   and commit vibe's result on the card branch (callers pass the worktree).
4. ``merge_card_branch`` — on accept, merge in the MAIN worktree (repo root,
   base branch still checked out there) with ``--no-ff``, so every accepted
   card is exactly ONE merge commit (the diff view shows
   ``merge_commit^..merge_commit`` and a future revert has one commit to
   revert). The card branch and its linked worktree are KEPT after accept —
   they are what the card's diff is read from — and removed only when the
   card is deleted or archived (``remove_card_worktree``). A conflict raises
   ``GitMergeConflict`` and the conflicted merge is LEFT IN PLACE in the main
   worktree (no abort) so the runner can spawn a resolver session there; the
   card branch AND the linked worktree are kept.
5. ``revert_merge_commit`` — undo an accepted card: ``git revert`` its merge
   commit in the MAIN worktree. All-or-nothing: a conflict ABORTS the revert
   (nothing is left in place, no resolver is spawned) and raises
   ``RevertConflict`` with the conflicting paths.

Sandbox runs (``sessions/<id>/workspace``) do not use git: there is no project
repository to branch from or merge into.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

GIT_IDENTITY_NAME = "Vibe Harness"
GIT_IDENTITY_EMAIL = "vibe-harness@localhost"

# Written to repos that vibe-harness initializes itself, so test runs and
# tooling never dirty the tree (which would break the clean-tree merge check).
# Existing repositories are never touched.
AUTO_GITIGNORE = """__pycache__/
*.py[cod]
.pytest_cache/
.mypy_cache/
.ruff_cache/
.venv/
venv/
node_modules/
dist/
build/
*.egg-info/
"""


class GitError(Exception):
    """Base for git workflow failures; maps to HTTP 409."""


class GitBusyError(GitError):
    """Another card is currently running in the same repository."""


class GitMergeConflict(GitError):
    """Merging the card branch into its base branch conflicted. The conflicted
    merge is LEFT IN PLACE (MERGE_HEAD set) for a resolver session — or manual
    resolution."""


class RevertConflict(GitError):
    """Reverting the card's merge commit conflicted. Unlike a merge conflict,
    the revert is ABORTED: nothing is left in place and no resolver is
    spawned. ``paths`` lists the files that conflicted."""

    def __init__(self, message: str, paths: list[str] | None = None):
        super().__init__(message)
        self.paths = list(paths or [])


def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    except OSError as exc:
        # e.g. cwd no longer exists (repo deleted while the server was down)
        if check:
            raise GitError(f"git {' '.join(args)} failed in {cwd}: {exc}")
        return subprocess.CompletedProcess(["git", *args], 127, "", str(exc))
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {cwd}: "
            f"{(proc.stderr or proc.stdout).strip()}")
    return proc


def repo_root(path: Path) -> Path | None:
    """The enclosing repository root, or None if `path` is not inside a repo."""
    proc = run_git(["rev-parse", "--show-toplevel"], path, check=False)
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip())


def current_branch(cwd: Path) -> str:
    proc = run_git(["symbolic-ref", "--short", "HEAD"], cwd, check=False)
    if proc.returncode != 0:
        raise GitError(f"cannot determine the current branch in {cwd} (detached HEAD?)")
    return proc.stdout.strip()


def is_dirty(cwd: Path) -> bool:
    return bool(run_git(["status", "--porcelain"], cwd).stdout.strip())


def branch_exists(cwd: Path, branch: str) -> bool:
    return run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                   cwd, check=False).returncode == 0


def registered_worktrees(root: Path) -> set[Path]:
    """Absolute paths of all worktrees git currently knows about for `root`."""
    proc = run_git(["worktree", "list", "--porcelain"], root, check=False)
    paths = set()
    for line in proc.stdout.splitlines():
        if line.startswith("worktree "):
            paths.add(Path(line[len("worktree "):].strip()).resolve())
    return paths


def _drop_worktree_at(root: Path, worktree_path: Path) -> None:
    """Remove whatever sits at `worktree_path`: a registered worktree
    (``git worktree remove --force``) or a stale leftover directory (rm -rf),
    then prune the repo's worktree registry."""
    run_git(["worktree", "prune"], root, check=False)
    if worktree_path.resolve() in registered_worktrees(root):
        run_git(["worktree", "remove", "--force", str(worktree_path)], root,
                check=False)
    if worktree_path.exists() or worktree_path.is_symlink():
        shutil.rmtree(worktree_path, ignore_errors=True)
    run_git(["worktree", "prune"], root, check=False)


def ensure_repo(ws: Path) -> Path:
    """Make `ws` a git repository if it is not one yet; return the repo root.

    A freshly initialized repo gets a minimal .gitignore (committed in the
    initial commit, so test artifacts never dirty the tree), an initial empty
    commit (so the base branch exists as a real commit), and a repo-local
    identity if none is configured. Repos that already existed are left alone.
    """
    root = repo_root(ws)
    fresh = False
    if root is None:
        run_git(["init", "-q", "-b", "main"], ws)
        root = repo_root(ws)
        assert root is not None
        (root / ".gitignore").write_text(AUTO_GITIGNORE, encoding="utf-8")
        fresh = True
    # Repo-local identity, only when nothing is configured (never global).
    if not run_git(["config", "user.email"], root, check=False).stdout.strip():
        run_git(["config", "user.email", GIT_IDENTITY_EMAIL], root)
    if not run_git(["config", "user.name"], root, check=False).stdout.strip():
        run_git(["config", "user.name", GIT_IDENTITY_NAME], root)
    # An empty repo has no commits; the base branch must exist for branching.
    if run_git(["rev-parse", "--verify", "HEAD"], root, check=False).returncode != 0:
        # The initial commit folds in the harness files dropped by project
        # registration, so the first card run sees a clean tree.
        if fresh:
            run_git(["add", ".gitignore"], root)
        for extra in ("route.md", "agent.md"):
            if (root / extra).exists():
                run_git(["add", extra], root)
        run_git(["commit", "--allow-empty", "-q", "-m",
                 "Initial commit (vibe-harness)"], root)
    return root


def prepare_card_branch(ws: Path, branch: str, worktree_path: Path) -> dict:
    """Check out the card's branch in a per-card LINKED WORKTREE at
    `worktree_path`; return the git session record
    ``{repo, branch, base_branch, worktree, stashed, auto_stashed}``.

    The repo's main worktree (the project folder) stays on its base branch, so
    concurrent cards in the same repo each get their own worktree. A dirty
    main worktree never blocks a run: leftover uncommitted changes (e.g.
    left behind by an interrupted previous run) are stashed with
    ``git stash push -u`` — on a first run and on re-runs alike — and the
    stash is kept for manual recovery. ``stashed`` records that a stash
    happened; ``auto_stashed`` additionally flags that it happened on a first
    run (the branch did not exist yet). The base branch is the branch that was
    checked out in the main worktree when the run started.

    A stale worktree still registered at `worktree_path` (e.g. from an
    interrupted earlier run of this card) is removed first — git checks a
    branch out in only one worktree, so ``-B`` would otherwise fail.
    """
    root = ensure_repo(ws)
    base = current_branch(root)
    if merge_in_progress(root):
        raise GitError(f"a merge is in progress in {root} — resolve it before "
                       f"starting another card there")
    stashed = False
    auto_stashed = False
    rerun = branch_exists(root, branch)
    if is_dirty(root):
        message = ("vibe-harness: leftover changes before re-run" if rerun else
                   "vibe-harness: leftover changes stashed on first run "
                   "(interrupted previous run?)")
        run_git(["stash", "push", "-u", "-q", "-m", message], root)
        stashed = True
        auto_stashed = not rerun
    worktree_path = Path(worktree_path).resolve()
    # A stale worktree from an interrupted earlier run may hold uncommitted
    # work; stash it too (the stash lives in the repo) before dropping the
    # worktree, so nothing is lost on a re-run.
    if worktree_path in registered_worktrees(root) and is_dirty(worktree_path):
        run_git(["stash", "push", "-u", "-q", "-m",
                 "vibe-harness: leftover changes in the card's worktree before re-run"],
                worktree_path)
        stashed = True
    _drop_worktree_at(root, worktree_path)
    run_git(["worktree", "add", str(worktree_path), "-B", branch, base], root)
    return {"repo": str(root), "branch": branch, "base_branch": base,
            "worktree": str(worktree_path),
            "stashed": stashed, "auto_stashed": auto_stashed}


def commit_workspace(ws: Path, message: str) -> bool:
    """Stage and commit everything under `ws` on the current branch.
    Returns False when there was nothing to commit."""
    run_git(["add", "-A", "--", "."], ws)
    if not is_dirty(ws):
        return False
    run_git(["commit", "-q", "-m", message], ws)
    return True


def merge_in_progress(root: Path) -> bool:
    """True when a merge is in progress (MERGE_HEAD exists) — e.g. a conflicted
    merge left in place for a resolver session."""
    return run_git(["rev-parse", "-q", "--verify", "MERGE_HEAD"], root,
                   check=False).returncode == 0


def revert_in_progress(root: Path) -> bool:
    """True when a revert is in progress (REVERT_HEAD exists)."""
    return run_git(["rev-parse", "-q", "--verify", "REVERT_HEAD"], root,
                   check=False).returncode == 0


def is_merge_commit(root: Path, sha: str) -> bool:
    """True when `sha` is a merge commit (it has more than one parent).

    Raises GitError when the commit cannot be read (unknown sha, not a repo).
    """
    proc = run_git(["rev-list", "--parents", "-n", "1", sha], root, check=False)
    if proc.returncode != 0:
        raise GitError(
            f"cannot read commit {sha!r} in {root}: "
            f"{(proc.stderr or proc.stdout).strip()}")
    return len(proc.stdout.split()) > 2


def _conflicted_paths(root: Path) -> list[str]:
    """Paths left unmerged by a failed revert (empty when it failed for
    another reason, e.g. the revert would be empty)."""
    proc = run_git(["diff", "--name-only", "--diff-filter=U"], root, check=False)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def revert_merge_commit(root: Path, sha: str) -> dict:
    """Undo an accepted card: ``git revert`` its merge commit in the MAIN
    worktree (the repo root, where the base branch is checked out) and return
    ``{revert_commit, reverted_commit, reverted_at}``.

    A merge commit is reverted with ``-m 1`` (the base branch side is the
    mainline); any other commit is reverted plainly — ``-m`` on a non-merge
    commit is an error.

    All-or-nothing: a conflict ABORTS the revert and raises ``RevertConflict``
    carrying the conflicting paths, so the repository is left exactly as it
    was. No resolver session is spawned for a revert — unlike an accept-time
    merge conflict, there is nothing here the user asked to keep.
    """
    root = Path(root)
    if merge_in_progress(root):
        raise GitError(
            f"a merge is in progress in {root} — resolve it before reverting a card")
    if revert_in_progress(root):
        raise GitError(
            f"a revert is already in progress in {root} — finish or abort it first")
    if is_dirty(root):
        raise GitError(f"{root} has uncommitted changes — commit or stash them first")
    args = ["revert", "--no-edit"]
    if is_merge_commit(root, sha):
        args += ["-m", "1"]
    proc = run_git([*args, sha], root, check=False)
    if proc.returncode != 0:
        paths = _conflicted_paths(root)
        run_git(["revert", "--abort"], root, check=False)
        if paths:
            raise RevertConflict(
                f"reverting {sha} conflicted in {', '.join(paths)} — the revert "
                f"was aborted and the repository left untouched", paths)
        # No conflicted paths: the revert did not apply at all (most often the
        # change is already undone, so the revert would be empty).
        raise RevertConflict(
            f"reverting {sha} did not apply — the change is already undone (or "
            f"the commit is empty); the revert was aborted and the repository "
            f"left untouched", [])
    head = run_git(["rev-parse", "HEAD"], root).stdout.strip()
    return {"revert_commit": head, "reverted_commit": sha,
            "reverted_at": datetime.now(timezone.utc).isoformat()}


def delete_card_branch(git_info: dict) -> None:
    """Delete the card's branch (safe delete: only if already merged)."""
    run_git(["branch", "-q", "-d", git_info["branch"]], Path(git_info["repo"]))


def remove_card_worktree(git_info: dict) -> None:
    """Remove the card's linked worktree: ``git worktree remove --force`` when
    it is still registered, rm -rf of a stale leftover directory, then
    ``git worktree prune``. Safe to call when there is no worktree."""
    worktree = git_info.get("worktree")
    if not worktree:
        return
    _drop_worktree_at(Path(git_info["repo"]), Path(worktree))


def ensure_card_worktree(git_info: dict) -> Path | None:
    """Re-create the card's linked worktree from its branch when it is no
    longer registered — e.g. pruned at startup while the card still awaits
    accept (``git worktree add <worktree> <branch>``). Returns the worktree
    path, or None when it cannot be re-created (no repo / branch gone)."""
    root_s = git_info.get("repo")
    branch = git_info.get("branch")
    worktree = git_info.get("worktree")
    if not (root_s and branch and worktree):
        return None
    root = Path(root_s)
    wt = Path(worktree).resolve()
    if not root.is_dir():
        return None
    if not branch_exists(root, branch):
        return None          # also covers root not being a repository
    if wt in registered_worktrees(root):
        return wt
    _drop_worktree_at(root, wt)
    run_git(["worktree", "add", str(wt), branch], root)
    return wt


def merge_card_branch(git_info: dict) -> dict:
    """Merge the card's branch into its base branch IN THE MAIN WORKTREE (the
    repo root, where the base branch is still checked out) and return the
    merge result ``{merge_commit, merge_was_ff, merged_at}``.

    Always ``--no-ff``: every accepted card produces exactly ONE merge commit
    whose second parent is the card branch, so "this card" is a single commit
    — the diff view can show ``merge_commit^..merge_commit`` and a future
    revert has something to revert. A fast-forwarded card would have no such
    commit.

    The card branch and its linked worktree are KEPT: the diff of what the
    card changed stays inspectable after accept. They are removed only when
    the card is deleted or archived (``remove_card_worktree``).

    ``merge_was_ff`` is True only when the merge created no commit at all
    (the branch was already contained in the base — nothing to merge).

    A conflict raises ``GitMergeConflict`` and the conflicted merge is LEFT IN
    PLACE in the main worktree (no abort) so a resolver session can fix it
    there; the card branch AND the linked worktree are kept. Leaves the main
    worktree on the base branch.
    """
    root = Path(git_info["repo"])
    branch = git_info["branch"]
    base = git_info["base_branch"]
    if is_dirty(root):
        raise GitError(f"{root} has uncommitted changes — commit or stash them first")
    run_git(["checkout", "-q", base], root)
    before = run_git(["rev-parse", "HEAD"], root).stdout.strip()
    proc = run_git(["merge", "--no-ff", "--no-edit", "--quiet", branch], root,
                   check=False)
    if proc.returncode != 0:
        raise GitMergeConflict(
            f"merging {branch} into {base} conflicted — left in place for "
            f"resolution (branch {branch} was kept)")
    head = run_git(["rev-parse", "HEAD"], root).stdout.strip()
    return {"merge_commit": head, "merge_was_ff": head == before,
            "merged_at": datetime.now(timezone.utc).isoformat()}
