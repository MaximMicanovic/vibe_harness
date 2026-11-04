"""ReviewMixin: the review loop of `SessionRunner` — reading the reviewer's
verdict, diff/test gates, and fixer rounds — moved out of the runner core (`app/runner/__init__.py`).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from app import gitops
from app.gitops import GitError
from .prompts import (
    REVIEW_DIFF_MAX_CHARS,
    fix_prompt_template,
    now_iso,
    parse_verdict,
)
from .supervise import TestFailedError


class ReviewMixin:

    @staticmethod
    def _read_verdict(rdir: Path, ws: Path) -> tuple[bool, str | None] | None:
        """Reviewer verdict: last VERDICT: line in its session log, else REVIEW.md."""
        try:
            text = (rdir / "session.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        verdict = parse_verdict(text[-8000:])
        if verdict is not None:
            return verdict
        try:
            return parse_verdict((ws / "REVIEW.md").read_text(encoding="utf-8",
                                                             errors="replace"))
        except OSError:
            return None

    def _review_diff(self, session: dict, ws: Path, full: bool = False) -> tuple[str | None, str | None]:
        """The diff a fresh reviewer should look at: `git diff <base>..HEAD` in
        the card's worktree.

        The base is the commit the previous review round covered
        (``session.last_reviewed_commit``), else the MERGE-BASE of the card
        branch and its base branch. With ``full=True`` (config.review_diff is
        false) the base is ALWAYS the merge-base — the WHOLE card diff,
        ignoring ``last_reviewed_commit``.
        The merge-base matters: diffing from the base TIP makes a branch that
        is merely behind its base look like it
        reverts whatever landed there since, and the reviewer rejects the card
        for someone else's commit.
        Returns (diff_text, range_description); (None, None) when the run is
        not git-backed (sandbox) or git fails. The diff is capped so the
        reviewer's context stays small.
        """
        git_info = session.get("git") or {}
        wt = git_info.get("worktree")
        if not wt:
            return None, None
        try:
            base = None if full else session.get("last_reviewed_commit")
            if not base:
                base_ref = git_info.get("base_branch") or "HEAD"
                base = gitops.run_git(["merge-base", base_ref, "HEAD"],
                                      Path(wt), check=False).stdout.strip()
                if not base:
                    base = gitops.run_git(["rev-parse", base_ref],
                                          Path(wt)).stdout.strip()
            head = gitops.run_git(["rev-parse", "HEAD"], Path(wt)).stdout.strip()
            proc = gitops.run_git(["diff", f"{base}..{head}"], Path(wt), check=False)
            diff = proc.stdout or ""
            if len(diff) > REVIEW_DIFF_MAX_CHARS:
                diff = diff[:REVIEW_DIFF_MAX_CHARS] + "\n... [diff truncated]\n"
            return diff, f"{base[:12]}..{head[:12]} (full: {base}..{head})"
        except (GitError, OSError):
            return None, None

    def _reviewed_commit(self, session: dict) -> str | None:
        """HEAD of the card's worktree right now — the commit this review round
        covers (recorded as `last_reviewed_commit` for the next round's diff)."""
        git_info = session.get("git") or {}
        wt = git_info.get("worktree")
        if not wt:
            return None
        try:
            return gitops.run_git(["rev-parse", "HEAD"], Path(wt)).stdout.strip()
        except (GitError, OSError):
            return None

    # ------------------------------------------------------------ test gate

    def _run_tests(self, ws: Path) -> tuple[bool, str] | None:
        """Run the configured test command in the workspace.

        Returns None when no test command is configured, else
        (passed, combined output). A timeout counts as a failure and the
        captured output is included.
        """
        if not self.config.test_cmd:
            return None
        try:
            proc = subprocess.run(self.config.test_cmd, shell=True, cwd=ws,
                                  capture_output=True, text=True,
                                  timeout=self.config.test_timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            out, err = exc.stdout or "", exc.stderr or ""
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
            if isinstance(err, bytes):
                err = err.decode(errors="replace")
            return False, (f"test command timed out after "
                           f"{self.config.test_timeout_seconds}s\n{out}\n{err}")
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode == 0, output

    def _green_gate(self, card: dict) -> str | None:
        """Ship-only-green gate at accept: the test command must pass in the
        workspace right before the card is accepted/merged. Raises
        TestFailedError (-> 409) on failure; records verified=True on pass and
        returns the passing output (the PR body quotes it), or None when no
        test command is configured. The unset case NEVER blocks the accept
        (the board must not deadlock) — it is surfaced loudly instead:
        `test_cmd_configured` in GET /api/settings and the card's `warnings`.
        """
        if not self.config.test_cmd:
            return None
        session = card.get("session") or {}
        ws = Path(session.get("workdir")
                  or (self.sessions_dir / card["id"] / "workspace"))
        if not ws.exists():
            # The recorded work folder is gone (e.g. the card's linked
            # worktree was pruned at startup while the card still awaited
            # accept): re-create it from the card branch when possible.
            git_info = session.get("git") or {}
            recreated = (gitops.ensure_card_worktree(git_info)
                         if git_info.get("worktree") else None)
            if recreated is not None:
                ws = recreated
        tested = self._run_tests(ws)
        if tested is None or not tested[0]:
            tail = (tested[1] if tested else "")[-2000:]
            raise TestFailedError(f"tests failed at accept:\n{tail}")
        self.store.patch_session(card["id"], {"verified": True})
        return tested[1]

    def _loop_reject(self, card_id: str, card: dict, reason: str, *,
                     source: str, review_record: dict | None = None,
                     session_patch: dict | None = None) -> None:
        """Shared rejection path for the review loop: the reviewer's verdict
        (source='review') and the test gate (source='test') both land here.

        When the loop cap (max_review_rounds) is hit the card lands in 'done'
        flagged exhausted (manual accept required); otherwise it goes back to
        'running' and a fixer session receives the reason. A test-gate
        rejection consumes a loop round even though no review session ran.
        (session_patch rides along in the spawn/done patch — patch_session is
        not allowed while the card is live.)
        """
        session = card.get("session") or {}
        rounds = session.get("review_rounds") or 0
        extra = dict(session_patch or {})
        if source == "test":
            rounds += 1
            extra["review_rounds"] = rounds
        record = review_record
        if record is None:
            record = {"approved": False, "reason": reason, "round": rounds,
                      "source": "test", "tokens": None, "log": None,
                      "export_dir": None}
        extra["review"] = record
        if rounds >= self.config.max_review_rounds:
            record["exhausted"] = True
            self.store.set_status(card_id, "done", session_patch={
                **extra,
                "finished_at": now_iso(),
            })
        else:
            # Persistent worker: resume the previous worker session (its
            # session id from the last round's export.json) instead of
            # spawning a fresh one that would re-read the whole workspace.
            # The id rides in session_patch for test-gate rejections (the
            # run outcome) and in the card's session for review rejections.
            resume = None
            if self.config.worker_resume:
                resume = (extra.get("worker_session_id")
                          or (card.get("session") or {}).get("worker_session_id"))
            self._spawn(card_id, kind="run",
                        prompt=fix_prompt_template(card["body"], reason,
                                                   self._mcp_name(),
                                                   resumed=bool(resume),
                                                   tdd=self.config.tdd,
                                                   mailbox=self._prompt_mailbox(
                                                       "run", card, resume)),
                        extra_patch=extra, resume=resume)

