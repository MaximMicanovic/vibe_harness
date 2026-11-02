"""What a card actually changed — the diff behind the board's Diff popup.

A reviewer session approves a change the user never sees; this module is what
makes it inspectable. Three ranges, all read from the card's git session
record (``session.git``):

- ``card``     — what accept merged: ``merge-base(base_branch, harness/<id>)
                 ..harness/<id>`` before accept, ``merge_commit^..merge_commit``
                 after. Both are the same set of changes, so the view does not
                 change when the card is accepted. (Diffing from the base TIP
                 would show a branch that is merely behind its base as
                 reverting whatever landed there since — the false "revert".)
                 The card branch and its linked worktree are kept after accept,
                 so this keeps working once the card is accepted.
- ``round``    — what the last review round judged: the commits between the
                 previous round's commit and this round's commit (the harness
                 advances ``last_reviewed_commit`` to HEAD when a round ends,
                 so the literal ``last_reviewed_commit..HEAD`` is empty right
                 after a review; the review history reconstructs the range).
                 Round 1 has no previous round, so it starts at the merge-base
                 of the card branch and its base branch — never at the base
                 tip, which would show the base's newer commits as deletions.
- ``worktree`` — uncommitted changes in the card's linked worktree
                 (``git diff HEAD``).

Sandbox runs (no ``session.git``) are not git-backed: the response says so
instead of failing. The diff is capped at ``DIFF_MAX_BYTES`` and flagged
``truncated`` so a giant card cannot hang the panel.
"""
from __future__ import annotations

from pathlib import Path

from app import gitops

DIFF_MAX_BYTES = 400 * 1024
RANGES = ("card", "round", "worktree")
DEFAULT_RANGE = "card"


class DiffRangeError(Exception):
    """Unknown range — maps to HTTP 400."""


def _rev(cwd: Path, ref: str) -> str:
    return gitops.run_git(["rev-parse", ref], cwd).stdout.strip()


def _merge_base(cwd: Path, a: str, b: str) -> str:
    """The commit `b` was cut from `a` — what accept actually merges.

    Diffing from the base TIP instead would show the card reverting everything
    that landed on the base after the branch was cut: a card branch that is
    merely behind its base would look like it deletes other people's work.
    """
    proc = gitops.run_git(["merge-base", a, b], cwd, check=False)
    return (proc.stdout or "").strip() or _rev(cwd, a)


def _cap(text: str) -> tuple[str, bool]:
    """Cut `text` to DIFF_MAX_BYTES of UTF-8 and flag it."""
    raw = text.encode("utf-8")
    if len(raw) <= DIFF_MAX_BYTES:
        return text, False
    cut = raw[:DIFF_MAX_BYTES].decode("utf-8", errors="ignore")
    return cut + "\n... [diff truncated]\n", True


def _files(cwd: Path, args: list[str]) -> list[dict]:
    """Per-file +/- counts for the same range (``git diff --numstat``)."""
    proc = gitops.run_git(["diff", "--numstat", *args], cwd, check=False)
    files = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        files.append({
            "path": path,
            # Binary files report "-" for both counts.
            "additions": int(added) if added.isdigit() else 0,
            "deletions": int(deleted) if deleted.isdigit() else 0,
        })
    return files


def _last_review(session: dict) -> dict | None:
    """The verdict record for the range: the last round's record."""
    reviews = session.get("reviews") or []
    if reviews:
        return reviews[-1]
    return session.get("review")


def _card_range(session: dict, git_info: dict) -> tuple[str, str, list[str], Path]:
    root = Path(git_info["repo"])
    merge_commit = git_info.get("merge_commit")
    if merge_commit:
        base = _rev(root, f"{merge_commit}^")
        return base, merge_commit, [f"{base}..{merge_commit}"], root
    branch = git_info.get("branch")
    base_branch = git_info.get("base_branch") or "HEAD"
    if not branch or not gitops.branch_exists(root, branch):
        raise gitops.GitError(
            "the card branch is gone (accepted before branches were kept) — "
            "no diff to show")
    # From the merge-base, not the base tip: that is what accept merges (and
    # what `merge_commit^..merge_commit` shows after accept).
    base = _merge_base(root, base_branch, branch)
    head = _rev(root, branch)
    return base, head, [f"{base}..{head}"], root


def _round_range(session: dict, git_info: dict) -> tuple[str, str, list[str], Path]:
    root = Path(git_info["repo"])
    reviews = session.get("reviews") or []
    last = reviews[-1] if reviews else (session.get("review") or {})
    head = last.get("commit") or session.get("last_reviewed_commit")
    if not head:
        head = _rev(root, git_info.get("branch") or "HEAD")
    previous = reviews[-2] if len(reviews) > 1 else None
    base = (previous or {}).get("commit")
    if not base:
        # Round 1: from the merge-base, not the base tip. A branch that is
        # merely behind its base must not look like it deletes the base's
        # newer commits — the same false revert the card range avoids.
        base = _merge_base(root, git_info.get("base_branch") or "HEAD", head)
    return base, head, [f"{base}..{head}"], root


def _worktree_range(session: dict, git_info: dict) -> tuple[str, str, list[str], Path]:
    worktree = git_info.get("worktree")
    if not worktree or not Path(worktree).is_dir():
        raise gitops.GitError("the card's worktree is gone — no uncommitted "
                              "changes to show")
    wt = Path(worktree)
    head = _rev(wt, "HEAD")
    return head, "worktree", ["HEAD"], wt


_RANGE_READERS = {
    "card": _card_range,
    "round": _round_range,
    "worktree": _worktree_range,
}


def card_diff(card: dict, range: str = DEFAULT_RANGE) -> dict:
    """The card's diff for `range` plus the verdict record for it.

    Response: ``{range, base, head, diff, truncated, files, review}`` — plus
    ``reason`` when there is nothing to show (sandbox run, pruned worktree,
    deleted branch). Never raises for a git problem: the panel shows the
    reason instead of an error page.
    """
    if range not in RANGES:
        raise DiffRangeError(
            f"unknown diff range {range!r} — use one of {', '.join(RANGES)}")
    session = card.get("session") or {}
    git_info = session.get("git") or {}
    review = _last_review(session)
    empty = {"range": range, "base": None, "head": None, "diff": None,
             "truncated": False, "files": [], "review": review}
    if not git_info.get("repo"):
        return {**empty, "reason": "not git-backed"}
    try:
        base, head, args, cwd = _RANGE_READERS[range](session, git_info)
        proc = gitops.run_git(["diff", *args], cwd, check=False)
        diff, truncated = _cap(proc.stdout or "")
        files = _files(cwd, args)
    except (gitops.GitError, OSError) as exc:
        return {**empty, "reason": str(exc)}
    return {"range": range, "base": base, "head": head, "diff": diff,
            "truncated": truncated, "files": files, "review": review}
