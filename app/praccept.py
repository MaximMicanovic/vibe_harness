"""Accept a card by opening a pull request (``accept_mode`` = ``pr``).

In ``local`` mode (the default) accepting a card merges its branch into the
base branch in the project folder. In ``pr`` mode the harness never merges: it
pushes the card branch to ``origin`` and opens a pull request whose body
carries the audit trail — the card spec, the review verdict, the test output
tail and the diff stat — so the change is reviewed and merged where the team
already works.

Everything here is a thin wrapper around ``git`` / ``gh``; the body itself is
built by :func:`build_pr_body`, a pure function (no git, no network) so it can
be tested without either. Every command goes through :func:`_run`, the single
seam the tests replace.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from app.gitops import GitError, GitPreflightError, branch_exists

# Cap on the test output embedded in the PR body: the tail is what matters
# (the last failure or the summary line), not the whole suite log.
PR_TEST_OUTPUT_MAX_CHARS = 4000

_PR_URL_RE = re.compile(r"https?://\S+/pull/\d+")
_PR_NUMBER_RE = re.compile(r"/pull/(\d+)")


# ----------------------------------------------------------------------
# the body: a pure function
# ----------------------------------------------------------------------

def _tail(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


def _fenced(text: str | None, empty: str) -> str:
    text = (text or "").strip()
    return f"_{empty}_" if not text else "```\n" + text + "\n```"


def _verdict_section(review: dict | None) -> str:
    """The reviewer's verdict as two markdown bullets: verdict + reason."""
    review = review or {}
    round_no = review.get("round")
    model = (review.get("model") or "").strip()
    if review.get("approved") is True:
        verdict = "approved"
    elif review.get("exhausted") is True:
        verdict = ("not approved — the review/fix loop exhausted its rounds, "
                   "accepted manually")
    elif review.get("approved") is False:
        verdict = "rejected"
    else:
        verdict = "no review on record"
    details = []
    if round_no is not None:
        details.append(f"round {round_no}")
    if model:
        details.append(f"model `{model}`")
    if details:
        verdict += " (" + ", ".join(details) + ")"
    reason = (review.get("reason") or "").strip() or "—"
    return f"- **Verdict:** {verdict}\n- **Reason:** {reason}"


def build_pr_body(*, card: dict, review: dict | None = None,
                  test_output: str | None = None, diff_stat: str | None = None,
                  branch: str | None = None,
                  base_branch: str | None = None) -> str:
    """The pull request body: spec, verdict, test output tail, diff stat.

    Pure — no git, no network — so the audit trail is unit-tested directly.
    """
    title = (card.get("title") or "").strip() or "vibe-harness card"
    card_id = card.get("id") or ""
    spec = (card.get("body") or "").strip() or "_no spec recorded_"
    origin = f"Opened by vibe-harness from card `{card_id}`"
    if branch:
        origin += f" on branch `{branch}`"
        if base_branch:
            origin += f" → `{base_branch}`"
    parts = [
        f"# {title}",
        "",
        origin + ".",
        "",
        "## Card spec",
        "",
        spec,
        "",
        "## Review verdict",
        "",
        _verdict_section(review),
        "",
        "## Test output",
        "",
        _fenced(_tail(test_output, PR_TEST_OUTPUT_MAX_CHARS),
                "no test output recorded"),
        "",
        "## Diff stat",
        "",
        _fenced(diff_stat, "no diff stat recorded"),
        "",
    ]
    return "\n".join(parts)


# ----------------------------------------------------------------------
# what gh prints
# ----------------------------------------------------------------------

def parse_pr_url(stdout: str | None) -> str | None:
    """The PR URL `gh pr create` prints (the last one, if it prints several)."""
    matches = _PR_URL_RE.findall(stdout or "")
    return matches[-1] if matches else None


def parse_pr_number(url: str | None) -> int | None:
    match = _PR_NUMBER_RE.search(url or "")
    return int(match.group(1)) if match else None


# ----------------------------------------------------------------------
# the side effects: git push, gh pr create
# ----------------------------------------------------------------------

def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run one git/gh command. The single seam the tests stub."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_authenticated() -> bool:
    """`gh auth status` green — the CLI has a usable token for this host."""
    return _run(["gh", "auth", "status"]).returncode == 0


def has_origin(root: Path) -> bool:
    proc = _run(["git", "remote", "get-url", "origin"], root)
    return proc.returncode == 0 and bool((proc.stdout or "").strip())


def preflight(git_info: dict) -> None:
    """Fail fast — naming the missing piece — before anything is pushed.

    Raises ``GitPreflightError`` (a ``GitError``, so the API answers 409) when
    `gh` is not installed, `gh auth status` is not green, the repo has no
    `origin` remote, or the card branch is gone. Nothing is changed.
    """
    root = Path(git_info["repo"])
    branch = git_info.get("branch") or ""
    if not gh_available():
        raise GitPreflightError(
            "`gh` (the GitHub CLI) is not installed — install it from "
            "https://cli.github.com, or switch accept_mode to local")
    if not gh_authenticated():
        raise GitPreflightError(
            "`gh auth status` is not green — run `gh auth login` first, "
            "or switch accept_mode to local")
    if not has_origin(root):
        raise GitPreflightError(
            f"{root} has no `origin` remote — add one with "
            f"`git remote add origin <url>`, or switch accept_mode to local")
    if branch and not branch_exists(root, branch):
        raise GitPreflightError(
            f"the card branch {branch} no longer exists in {root} — "
            f"there is nothing to push")


def push_branch(git_info: dict) -> None:
    """``git push -u origin <branch>`` — publish the card branch."""
    root = Path(git_info["repo"])
    branch = git_info["branch"]
    proc = _run(["git", "push", "-u", "origin", branch], root)
    if proc.returncode != 0:
        raise GitError(
            f"git push -u origin {branch} failed in {root}: "
            f"{(proc.stderr or proc.stdout).strip()}")


def open_pr(git_info: dict, title: str, body: str) -> str | None:
    """``gh pr create`` on the card branch; returns the PR URL gh prints."""
    root = Path(git_info["repo"])
    branch = git_info["branch"]
    proc = _run(["gh", "pr", "create", "--title", title, "--body", body,
                 "--head", branch], root)
    if proc.returncode != 0:
        raise GitError(
            f"gh pr create failed for {branch}: "
            f"{(proc.stderr or proc.stdout).strip()}")
    return parse_pr_url(proc.stdout)


def diff_stat(git_info: dict) -> str:
    """``git diff --stat <base>...<branch>`` — what the PR changes."""
    root = Path(git_info["repo"])
    branch = git_info.get("branch") or ""
    base = git_info.get("base_branch") or ""
    if not (branch and base):
        return ""
    proc = _run(["git", "diff", "--stat", f"{base}...{branch}"], root)
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()
