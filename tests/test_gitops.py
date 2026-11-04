"""Unit tests for the git workflow (app/gitops.py). Real git, temp dirs only."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app import gitops
from app.gitops import GitError

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path) -> Path:
    """A git repo with one commit on the default branch."""
    root = tmp_path / "repo"
    root.mkdir()
    git(["init", "-q", "-b", "main"], root)
    git(["config", "user.email", "test@example.com"], root)
    git(["config", "user.name", "Test"], root)
    (root / "README.md").write_text("hi\n")
    git(["add", "-A"], root)
    git(["commit", "-q", "-m", "initial"], root)
    return root


def test_ensure_repo_inits_plain_folder(tmp_path):
    folder = tmp_path / "plain"
    folder.mkdir()
    root = gitops.ensure_repo(folder)
    assert root == folder.resolve()
    assert git(["rev-parse", "--is-inside-work-tree"], folder) == "true"
    # The base branch exists as a real commit and a commit identity resolves
    # (repo-local fallback only when the machine has none).
    assert git(["rev-parse", "--verify", "HEAD"], folder)
    assert git(["config", "user.email"], folder)
    assert git(["config", "user.name"], folder)


def test_ensure_repo_sets_identity_when_unconfigured(tmp_path, monkeypatch):
    # Hide the machine's global/system git config so no identity is visible.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global-config"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    folder = tmp_path / "plain"
    folder.mkdir()
    gitops.ensure_repo(folder)
    assert git(["config", "user.email"], folder) == gitops.GIT_IDENTITY_EMAIL
    assert git(["config", "user.name"], folder) == gitops.GIT_IDENTITY_NAME
    # The identity is repo-local; the (hidden) global config stays untouched.
    assert not Path(os.environ["GIT_CONFIG_GLOBAL"]).exists()


def test_ensure_repo_existing_repo_untouched(repo):
    before = git(["rev-parse", "HEAD"], repo)
    root = gitops.ensure_repo(repo)
    assert root == repo.resolve()
    assert git(["rev-parse", "HEAD"], repo) == before


def test_ensure_repo_subdir_of_repo_returns_parent_root(repo):
    sub = repo / "sub"
    sub.mkdir()
    assert gitops.ensure_repo(sub) == repo.resolve()


def _wt(tmp_path, name: str) -> Path:
    """A linked-worktree path OUTSIDE the repo (like sessions/<id>/worktree
    sits outside an arbitrary project folder)."""
    return tmp_path / "worktrees" / name


def test_prepare_card_branch_first_run(repo, tmp_path):
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    assert info["branch"] == "harness/abc123"
    assert info["base_branch"] == "main"
    assert info["stashed"] is False
    assert info["auto_stashed"] is False
    assert info["repo"] == str(repo.resolve())
    assert info["worktree"] == str(wt.resolve())
    # The card branch is checked out in the LINKED worktree...
    assert git(["symbolic-ref", "--short", "HEAD"], wt) == "harness/abc123"
    # ...while the main worktree (the project folder) stays on the base branch.
    assert git(["symbolic-ref", "--short", "HEAD"], repo) == "main"
    assert gitops.branch_exists(repo, "harness/abc123")
    assert wt.resolve() in gitops.registered_worktrees(repo)


def test_prepare_two_card_worktrees_same_repo(repo, tmp_path):
    """Worktree-per-card: two cards can hold branches in the same repo at the
    same time, each checked out in its own linked worktree."""
    wt1 = _wt(tmp_path, "aaa")
    wt2 = _wt(tmp_path, "bbb")
    i1 = gitops.prepare_card_branch(repo, "harness/aaa", wt1)
    i2 = gitops.prepare_card_branch(repo, "harness/bbb", wt2)
    assert git(["symbolic-ref", "--short", "HEAD"], wt1) == "harness/aaa"
    assert git(["symbolic-ref", "--short", "HEAD"], wt2) == "harness/bbb"
    assert git(["symbolic-ref", "--short", "HEAD"], repo) == "main"
    assert i1["base_branch"] == i2["base_branch"] == "main"
    assert not gitops.is_dirty(repo)


def test_prepare_card_branch_dirty_first_run_auto_stashes(repo, tmp_path):
    """A dirty MAIN worktree on a first run (e.g. left by an interrupted
    previous run) is stashed automatically — kept in the stash for manual
    recovery — and the card branch is created in the linked worktree; the run
    is not blocked."""
    (repo / "dirty.txt").write_text("wip\n")
    wt = _wt(tmp_path, "newcard")
    info = gitops.prepare_card_branch(repo, "harness/newcard", wt)
    assert info["stashed"] is True
    assert info["auto_stashed"] is True
    assert info["branch"] == "harness/newcard"
    assert gitops.branch_exists(repo, "harness/newcard")
    assert git(["symbolic-ref", "--short", "HEAD"], wt) == "harness/newcard"
    assert git(["symbolic-ref", "--short", "HEAD"], repo) == "main"
    assert not (repo / "dirty.txt").exists()
    assert not gitops.is_dirty(repo)
    # The stash is kept for manual recovery and holds the leftover change.
    assert "vibe-harness" in git(["stash", "list"], repo)
    assert "dirty.txt" in git(["stash", "show", "--include-untracked", "--name-only"],
                              repo)


def test_prepare_card_branch_rerun_stashes_leftovers(repo, tmp_path):
    wt = _wt(tmp_path, "abc123")
    gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (repo / "leftover.txt").write_text("partial work\n")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    assert info["stashed"] is True
    assert info["auto_stashed"] is False
    assert not (repo / "leftover.txt").exists()
    assert not gitops.is_dirty(repo)
    # The stash is kept for manual recovery.
    assert "vibe-harness" in git(["stash", "list"], repo)
    # The stale worktree was replaced (exactly one at wt), the branch was
    # reset to the base, and the main worktree is still on the base branch.
    assert git(["symbolic-ref", "--short", "HEAD"], wt) == "harness/abc123"
    assert git(["symbolic-ref", "--short", "HEAD"], repo) == "main"
    assert git(["rev-parse", "harness/abc123"], repo) == git(["rev-parse", "main"], repo)
    assert sum(1 for p in gitops.registered_worktrees(repo) if p == wt.resolve()) == 1


def test_prepare_card_branch_rerun_stashes_dirty_worktree(repo, tmp_path):
    """An interrupted earlier run leaves uncommitted work in the card's linked
    worktree; on re-run that work is stashed (kept for recovery) before the
    stale worktree is dropped and re-created."""
    wt = _wt(tmp_path, "abc123")
    gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "partial.txt").write_text("wip in the worktree\n")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    assert info["stashed"] is True
    assert "partial.txt" in git(["stash", "show", "--include-untracked", "--name-only"],
                                repo)
    assert not (wt / "partial.txt").exists()
    assert git(["symbolic-ref", "--short", "HEAD"], wt) == "harness/abc123"
    assert git(["rev-parse", "harness/abc123"], repo) == git(["rev-parse", "main"], repo)


def test_prepare_card_branch_merge_in_progress_raises(repo, tmp_path):
    """A conflicted merge left in the main worktree blocks new cards in the
    repo (it must be resolved first) instead of being stashed away."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "README.md").write_text("card change\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    (repo / "README.md").write_text("user change\n")
    git(["commit", "-q", "-am", "user edit"], repo)
    proc = subprocess.run(["git", "merge", "harness/abc123"], cwd=repo,
                          capture_output=True, text=True)
    assert proc.returncode != 0          # conflicted, left in place
    assert gitops.merge_in_progress(repo)
    with pytest.raises(GitError, match="merge is in progress"):
        gitops.prepare_card_branch(repo, "harness/other", _wt(tmp_path, "other"))


def test_commit_workspace(repo, tmp_path):
    wt = _wt(tmp_path, "abc123")
    gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "hello.py").write_text("print('hi')\n")
    assert gitops.commit_workspace(wt, "vibe-harness: test") is True
    assert not gitops.is_dirty(wt)
    assert "hello.py" in git(["show", "--name-only", "--format=", "HEAD"], wt)
    # Nothing to commit -> False, no new commit.
    assert gitops.commit_workspace(wt, "again") is False
    # The main worktree is untouched.
    assert not (repo / "hello.py").exists()
    assert git(["symbolic-ref", "--short", "HEAD"], repo) == "main"


def test_merge_card_branch_keeps_branch_and_worktree(repo, tmp_path):
    """Accept merges with --no-ff (one merge commit per card) and KEEPS the
    card branch and its linked worktree, so the card's diff stays inspectable
    after accept. The merge result is returned for the session record."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "hello.py").write_text("print('hi')\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    result = gitops.merge_card_branch(info)
    assert git(["symbolic-ref", "--short", "HEAD"], repo) == "main"
    assert (repo / "hello.py").exists()
    assert "hello.py" in git(["ls-tree", "--name-only", "main"], repo)
    # The branch and the linked worktree survive the accept.
    assert gitops.branch_exists(repo, "harness/abc123")
    assert wt.exists()
    assert wt.resolve() in gitops.registered_worktrees(repo)
    assert git(["symbolic-ref", "--short", "HEAD"], wt) == "harness/abc123"
    # Exactly ONE merge commit, with two parents (never a fast-forward).
    head = git(["rev-parse", "HEAD"], repo)
    assert result["merge_commit"] == head
    assert result["merge_was_ff"] is False
    assert result["merged_at"]
    assert len(git(["rev-list", "--parents", "-n", "1", "HEAD"], repo).split()) == 3
    # The card's own commit is reachable from the merge commit's second parent.
    assert git(["rev-parse", "HEAD^2"], repo) == git(["rev-parse", "harness/abc123"], repo)


def test_merge_card_branch_no_ff_even_when_base_did_not_move(repo, tmp_path):
    """A card whose base never moved would fast-forward; --no-ff still gives
    the single merge commit that means "this card" (diff view + revert)."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "hello.py").write_text("print('hi')\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    before = git(["rev-parse", "HEAD"], repo)
    result = gitops.merge_card_branch(info)
    assert result["merge_commit"] != before
    assert git(["rev-parse", "HEAD^1"], repo) == before


def test_merge_card_branch_already_up_to_date(repo, tmp_path):
    """Merging a branch that is already contained in the base creates no new
    commit; the result says so (merge_was_ff) and reports the current HEAD."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "hello.py").write_text("print('hi')\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    first = gitops.merge_card_branch(info)
    second = gitops.merge_card_branch(info)
    assert second["merge_commit"] == first["merge_commit"]
    assert second["merge_was_ff"] is True
    assert second["merged_at"]


def test_is_ancestor(repo, tmp_path):
    """is_ancestor answers `git merge-base --is-ancestor`: True only when the
    branch is contained in HEAD (the wrong-merge guard's check)."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "hello.py").write_text("print('hi')\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    # The card's work is not in the base branch yet.
    assert gitops.is_ancestor(repo, "harness/abc123") is False
    gitops.merge_card_branch(info)
    assert gitops.is_ancestor(repo, "harness/abc123") is True
    # A revision that does not exist is not an ancestor either: a broken check
    # must never let an accept through.
    assert gitops.is_ancestor(repo, "harness/nope") is False


def test_merge_card_branch_conflict_left_in_place(repo, tmp_path):
    """A conflict raises GitMergeConflict and the conflicted merge is LEFT in
    place in the MAIN worktree (no abort) for the resolver session; the branch
    AND the linked worktree are kept. A resolver can conclude it with add +
    commit in the main worktree, after which the worktree is removed and the
    branch deleted."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "README.md").write_text("card change\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    # The user edits the same file on the base branch (main worktree).
    (repo / "README.md").write_text("user change\n")
    git(["commit", "-q", "-am", "user edit"], repo)
    with pytest.raises(gitops.GitMergeConflict):
        gitops.merge_card_branch(info)
    # Merge still in progress on the base branch in the main worktree,
    # conflicted; branch and linked worktree kept.
    assert gitops.merge_in_progress(repo)
    assert git(["symbolic-ref", "--short", "HEAD"], repo) == "main"
    assert gitops.is_dirty(repo)
    assert gitops.branch_exists(repo, "harness/abc123")
    assert wt.exists()
    assert git(["symbolic-ref", "--short", "HEAD"], wt) == "harness/abc123"
    # A resolver concludes the merge in the main worktree: stage + commit.
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "resolver: conclude merge"], repo)
    assert not gitops.merge_in_progress(repo)
    assert not gitops.is_dirty(repo)
    gitops.remove_card_worktree(info)
    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(repo)


def test_merge_card_branch_dirty_main_worktree_auto_stashes(repo, tmp_path):
    """A dirty MAIN worktree at accept time — a tracked modification AND an
    untracked file (anything `git status --porcelain` counts, e.g. report.md)
    — does NOT block the merge: the changes are stashed with
    `git stash push -u` (kept for manual recovery, never popped
    automatically), the stash is recorded on the session git record
    (stashed=True), and the merge proceeds."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "hello.py").write_text("print('hi')\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    # The main worktree gets dirty between run start and accept: a tracked
    # modification, an untracked file, and an untracked hello.py whose later
    # stash pop would conflict with the merged one (that must not block the
    # merge — the merge happens first and the stash is simply kept).
    (repo / "README.md").write_text("local edit\n")
    (repo / "report.md").write_text("untracked report\n")
    (repo / "hello.py").write_text("local version\n")
    assert gitops.is_dirty(repo)
    result = gitops.merge_card_branch(info)
    # The merge happened: exactly one merge commit, the card's change in main.
    assert result["merge_was_ff"] is False
    assert result["merge_commit"] == git(["rev-parse", "HEAD"], repo)
    assert git(["rev-parse", "HEAD^2"], repo) == git(["rev-parse", "harness/abc123"], repo)
    assert (repo / "hello.py").read_text() == "print('hi')\n"
    assert git(["symbolic-ref", "--short", "HEAD"], repo) == "main"
    # The stash was recorded on the card's session git record.
    assert info["stashed"] is True
    # The dirty changes were stashed, not merged: the main worktree is clean
    # and holds the committed content, not the local edits.
    assert not gitops.is_dirty(repo)
    assert (repo / "README.md").read_text() == "hi\n"
    assert not (repo / "report.md").exists()
    # The stash is KEPT for manual recovery (never auto-popped or dropped).
    stash_list = git(["stash", "list"], repo)
    assert "vibe-harness: stashed before merging harness/abc123" in stash_list
    stashed_files = git(["stash", "show", "--include-untracked", "--name-only"], repo)
    assert "README.md" in stashed_files
    assert "report.md" in stashed_files
    assert "hello.py" in stashed_files


def test_merge_card_branch_merge_in_progress_still_raises(repo, tmp_path):
    """A merge already in progress in the main worktree is genuinely
    unrecoverable for an accept: still a hard GitError (never stashed away),
    and the in-progress merge is left untouched for its resolver."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "README.md").write_text("card change\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    (repo / "README.md").write_text("user change\n")
    git(["commit", "-q", "-am", "user edit"], repo)
    proc = subprocess.run(["git", "merge", "harness/abc123"], cwd=repo,
                          capture_output=True, text=True)
    assert proc.returncode != 0          # conflicted, left in place
    assert gitops.merge_in_progress(repo)
    with pytest.raises(GitError, match="merge is in progress"):
        gitops.merge_card_branch(info)
    # The conflicted merge is left alone; nothing was stashed away.
    assert gitops.merge_in_progress(repo)
    assert git(["stash", "list"], repo) == ""
    assert info.get("stashed") is not True


def test_remove_card_worktree_unregistered_dir(repo, tmp_path):
    """remove_card_worktree also cleans a stale, no-longer-registered dir."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    git(["worktree", "remove", "--force", str(wt)], repo)  # unregisters, deletes dir
    wt.mkdir()                                            # stale leftover dir
    (wt / "junk").write_text("x")
    gitops.remove_card_worktree(info)
    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(repo)


def test_ensure_repo_writes_gitignore_in_new_repo(tmp_path):
    """A repo initialized by vibe-harness gets a minimal .gitignore, committed
    in the initial commit, so test artifacts never dirty the tree."""
    folder = tmp_path / "plain"
    folder.mkdir()
    gitops.ensure_repo(folder)
    gi = (folder / ".gitignore").read_text()
    assert "__pycache__/" in gi
    assert ".pytest_cache/" in gi
    assert "node_modules/" in gi
    assert ".gitignore" in git(["show", "--name-only", "--format=", "HEAD"], folder)
    # Test artifacts stay ignored -> the tree stays clean.
    (folder / "__pycache__").mkdir()
    (folder / "__pycache__" / "x.pyc").write_text("x")
    assert git(["status", "--porcelain"], folder) == ""


def test_ensure_repo_existing_repo_gets_no_gitignore(repo):
    """Repos that already existed are never modified."""
    assert not (repo / ".gitignore").exists()
    gitops.ensure_repo(repo)
    assert not (repo / ".gitignore").exists()


def test_ensure_repo_commits_existing_route_md(tmp_path):
    """A route.md dropped into a plain folder by project registration joins the
    initial commit on auto-init, so the first card run sees a clean tree."""
    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "route.md").write_text("# routing\n")
    gitops.ensure_repo(folder)
    assert "route.md" in git(["show", "--name-only", "--format=", "HEAD"], folder)
    assert git(["status", "--porcelain"], folder) == ""


def test_ensure_repo_commits_existing_agent_md(tmp_path):
    """An agent.md dropped by project registration joins the initial commit."""
    folder = tmp_path / "plain2"
    folder.mkdir()
    (folder / "agent.md").write_text("# rules\n")
    (folder / "route.md").write_text("")
    gitops.ensure_repo(folder)
    names = git(["show", "--name-only", "--format=", "HEAD"], folder)
    assert "agent.md" in names and "route.md" in names
    assert git(["status", "--porcelain"], folder) == ""


def test_ensure_card_worktree_recreates_from_branch(repo, tmp_path):
    """ensure_card_worktree re-creates a pruned linked worktree from the card
    branch (used when a card's worktree was pruned at startup while the card
    still awaits accept); it is a no-op while the worktree is registered."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "f.txt").write_text("work\n")
    git(["add", "-A"], wt)
    git(["commit", "-q", "-m", "work"], wt)
    assert gitops.ensure_card_worktree(info) == wt.resolve()   # already there
    gitops.remove_card_worktree(info)
    assert not wt.exists()
    assert wt.resolve() not in gitops.registered_worktrees(repo)
    again = gitops.ensure_card_worktree(info)
    assert again == wt.resolve()
    assert wt.resolve() in gitops.registered_worktrees(repo)
    assert git(["symbolic-ref", "--short", "HEAD"], wt) == "harness/abc123"
    assert (wt / "f.txt").exists()          # the branch's content is back


def test_ensure_card_worktree_without_branch_returns_none(repo, tmp_path):
    """No branch (deleted / never created / not a repo) -> None, nothing created."""
    wt = _wt(tmp_path, "gone")
    info = {"repo": str(repo.resolve()), "branch": "harness/nope",
            "base_branch": "main", "worktree": str(wt.resolve())}
    assert gitops.ensure_card_worktree(info) is None
    assert not wt.exists()
    bad = {"repo": str(tmp_path / "not-a-repo"), "branch": "harness/x",
           "base_branch": "main", "worktree": str(wt.resolve())}
    assert gitops.ensure_card_worktree(bad) is None


# --------------------------------------------------------------------- revert
# Undoing an accepted card: `git revert` of the card's merge commit. The
# revert must be all-or-nothing — a conflict aborts and leaves the repo
# exactly as it was (no conflicted revert left behind, no resolver spawned).


def _accepted_card(repo: Path, tmp_path: Path, name: str = "abc123",
                   path: str = "hello.py", content: str = "print('hi')\n") -> dict:
    """Run one card end to end: branch in a linked worktree, commit, --no-ff
    merge into the base. Returns the merge result (merge_commit, ...)."""
    wt = _wt(tmp_path, name)
    info = gitops.prepare_card_branch(repo, f"harness/{name}", wt)
    (wt / path).write_text(content)
    gitops.commit_workspace(wt, "vibe-harness: test")
    return gitops.merge_card_branch(info)


def test_revert_merge_commit_undoes_the_card(repo, tmp_path):
    """Reverting the card's merge commit removes the change from the working
    tree and records the revert commit."""
    merged = _accepted_card(repo, tmp_path)
    assert (repo / "hello.py").exists()
    result = gitops.revert_merge_commit(repo, merged["merge_commit"])
    # The change is gone from the working tree and from the branch tip.
    assert not (repo / "hello.py").exists()
    assert "hello.py" not in git(["ls-tree", "--name-only", "main"], repo)
    # A new commit records the revert; the tree is clean afterwards.
    assert result["revert_commit"] == git(["rev-parse", "HEAD"], repo)
    assert result["revert_commit"] != merged["merge_commit"]
    assert result["reverted_commit"] == merged["merge_commit"]
    assert result["reverted_at"]
    assert not gitops.is_dirty(repo)
    assert not gitops.revert_in_progress(repo)
    assert not gitops.merge_in_progress(repo)


def test_revert_plain_commit_uses_no_m1(repo):
    """A non-merge commit (a card accepted before --no-ff, or a hand-made
    commit) is reverted without `-m 1` — `-m` on a non-merge commit fails."""
    (repo / "plain.txt").write_text("x\n")
    git(["add", "-A"], repo)
    git(["commit", "-q", "-m", "plain change"], repo)
    sha = git(["rev-parse", "HEAD"], repo)
    assert not gitops.is_merge_commit(repo, sha)
    result = gitops.revert_merge_commit(repo, sha)
    assert not (repo / "plain.txt").exists()
    assert result["revert_commit"] == git(["rev-parse", "HEAD"], repo)
    assert not gitops.is_dirty(repo)


def test_revert_dirty_tree_auto_stashes(repo, tmp_path):
    """A dirty main worktree does NOT block the revert either: the changes
    (tracked modification AND untracked file) are stashed with
    `git stash push -u` — kept for manual recovery, never auto-popped —
    recorded on the card's session git record when one is passed
    (stashed=True), and the revert proceeds."""
    wt = _wt(tmp_path, "abc123")
    info = gitops.prepare_card_branch(repo, "harness/abc123", wt)
    (wt / "hello.py").write_text("print('hi')\n")
    gitops.commit_workspace(wt, "vibe-harness: test")
    merged = gitops.merge_card_branch(info)
    assert (repo / "hello.py").exists()
    # The main worktree gets dirty after the accept.
    (repo / "README.md").write_text("local edit\n")
    (repo / "notes.txt").write_text("untracked\n")
    assert gitops.is_dirty(repo)
    result = gitops.revert_merge_commit(repo, merged["merge_commit"], git_info=info)
    # The revert went through; the card's change is gone from tree and branch.
    assert not (repo / "hello.py").exists()
    assert "hello.py" not in git(["ls-tree", "--name-only", "main"], repo)
    assert result["revert_commit"] == git(["rev-parse", "HEAD"], repo)
    assert result["reverted_commit"] == merged["merge_commit"]
    # The stash was recorded on the session git record; the tree is clean and
    # no revert is left in progress.
    assert info["stashed"] is True
    assert not gitops.is_dirty(repo)
    assert not gitops.revert_in_progress(repo)
    # The stash is kept for manual recovery with the revert-time message; the
    # user's work (tracked + untracked) is in it, not lost.
    assert "vibe-harness: stashed before reverting" in git(["stash", "list"], repo)
    stashed_files = git(["stash", "show", "--include-untracked", "--name-only"], repo)
    assert "README.md" in stashed_files
    assert "notes.txt" in stashed_files
    assert not (repo / "notes.txt").exists()


def test_revert_dirty_tree_without_git_record_still_stashes(repo, tmp_path):
    """revert_merge_commit without a session git record (the old call shape)
    still auto-stashes a dirty tree and reverts; there is just nothing to
    record the stash on."""
    merged = _accepted_card(repo, tmp_path)
    (repo / "wip.txt").write_text("x\n")
    result = gitops.revert_merge_commit(repo, merged["merge_commit"])
    assert not (repo / "hello.py").exists()
    assert result["revert_commit"] == git(["rev-parse", "HEAD"], repo)
    assert not gitops.is_dirty(repo)
    assert "vibe-harness" in git(["stash", "list"], repo)
    assert not (repo / "wip.txt").exists()


def test_revert_refuses_merge_in_progress(repo, tmp_path):
    """A merge already in progress (e.g. a conflicted accept awaiting its
    resolver) blocks the revert."""
    merged = _accepted_card(repo, tmp_path)
    # Start a conflicting merge in the main worktree.
    git(["checkout", "-q", "-b", "other"], repo)
    (repo / "hello.py").write_text("other\n")
    git(["commit", "-q", "-am", "other"], repo)
    git(["checkout", "-q", "main"], repo)
    (repo / "hello.py").write_text("main\n")
    git(["commit", "-q", "-am", "main"], repo)
    subprocess.run(["git", "merge", "other"], cwd=repo, capture_output=True)
    assert gitops.merge_in_progress(repo)
    with pytest.raises(GitError) as exc:
        gitops.revert_merge_commit(repo, merged["merge_commit"])
    assert "merge" in str(exc.value).lower()
    # The in-progress merge is left alone (it belongs to the resolver).
    assert gitops.merge_in_progress(repo)


def test_revert_refuses_revert_in_progress(repo, tmp_path):
    """A revert already in progress blocks a second one."""
    merged = _accepted_card(repo, tmp_path)
    (repo / ".git" / "REVERT_HEAD").write_text(merged["merge_commit"] + "\n")
    with pytest.raises(GitError) as exc:
        gitops.revert_merge_commit(repo, merged["merge_commit"])
    assert "revert" in str(exc.value).lower()
    assert (repo / "hello.py").exists()


def test_revert_conflict_aborts_and_leaves_repo_untouched(repo, tmp_path):
    """A conflicting revert aborts cleanly: the conflicting paths are reported,
    no conflicted revert is left in place, and the repo is exactly as before."""
    merged = _accepted_card(repo, tmp_path, path="README.md",
                            content="card change\n")
    # The user edits the same file on the base branch after the accept.
    (repo / "README.md").write_text("later user change\n")
    git(["commit", "-q", "-am", "later edit"], repo)
    before = git(["rev-parse", "HEAD"], repo)
    with pytest.raises(gitops.RevertConflict) as exc:
        gitops.revert_merge_commit(repo, merged["merge_commit"])
    assert exc.value.paths == ["README.md"]
    assert "README.md" in str(exc.value)
    # Aborted: no revert in progress, clean tree, HEAD unchanged, content kept.
    assert not gitops.revert_in_progress(repo)
    assert not gitops.is_dirty(repo)
    assert git(["rev-parse", "HEAD"], repo) == before
    assert (repo / "README.md").read_text() == "later user change\n"


def test_revert_twice_is_refused_and_leaves_repo_clean(repo, tmp_path):
    """Reverting the same merge commit twice is refused with a clear reason
    (the change is already undone) and leaves no stuck revert behind."""
    merged = _accepted_card(repo, tmp_path)
    gitops.revert_merge_commit(repo, merged["merge_commit"])
    head = git(["rev-parse", "HEAD"], repo)
    with pytest.raises(gitops.RevertConflict) as exc:
        gitops.revert_merge_commit(repo, merged["merge_commit"])
    assert exc.value.paths == []
    assert "already undone" in str(exc.value)
    assert not gitops.revert_in_progress(repo)
    assert not gitops.is_dirty(repo)
    assert git(["rev-parse", "HEAD"], repo) == head
