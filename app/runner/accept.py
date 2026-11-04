"""AcceptMixin: accept/revert and the git orchestration behind them (local
merge, pull request, conflict resolver sessions, per-repo accept queue) —
moved out of the runner core (`app/runner/__init__.py`).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from app import gitops, praccept
from app.backends import get_backend
from app.cards import CardNotFound, InvalidTransition
from app.gitops import GitBusyError, GitError, GitMergeConflict
from .prompts import export_token_breakdown, now_iso, resolve_prompt_template
from .supervise import TestFailedError
from app.tokens import export_cost_usd


class AcceptMixin:

    def accept_mode(self) -> str:
        """How accepting a card ships the change: `local` (merge) or `pr`.

        The sidebar choice (settings.json) wins over the config default
        (VIBE_HARNESS_ACCEPT_MODE), so the board can switch without a restart.
        """
        if self.settings is not None:
            return self.settings.accept_mode(self.config.accept_mode)
        return self.config.accept_mode

    def accept_card(self, card_id: str, *, force: bool = False) -> dict:
        """Accept a done card: gate first, then ship it.

        `force` is the explicit override for a card whose review loop was
        exhausted without an approval (review.exhausted): without it the
        review gate refuses (InvalidTransition -> 409). It is refused for any
        other card. auto_accept and the accept-all drain never pass it.

        In `pr` mode the branch is pushed and a pull request is opened instead
        of merging locally (see `_accept_with_pr`). In `local` mode the card
        branch is merged into its base branch (if any).

        Only ONE accept may merge into a repository at a time: every accept
        merges in the repo's MAIN worktree, and git allows exactly one merge
        in progress there. A second accept for the same repo does not merge —
        the card is queued (`session.accept_queued` + `queue_reason`) and
        merged automatically when the repo frees (see `_drain_accepts`).
        Different repos still accept concurrently.

        A merge conflict does NOT fail the accept: the conflicted merge is left
        in place, the card stays 'done' (Done column) flagged `resolving`, and
        a resolver vibe session is spawned. When the resolver concludes the
        merge, the accept finishes automatically (merge recorded -> accepted).
        The repo stays locked until that resolver concludes or fails; a second
        accept for the SAME card while its resolver is live raises
        GitBusyError (-> 409).

        The merge result is recorded on the session git record
        (``merge_commit`` / ``merge_was_ff`` / ``merged_at``) and the card
        branch + linked worktree are KEPT, so the card's diff stays
        inspectable after accept (``GET /api/cards/<id>/diff``).
        """
        card = self.store.check_accept(card_id, force=force)
        if force:
            # Remember the override on the session so the resolver-conclude
            # path (_conclude_resolved_merge) can finish THIS accept without
            # re-asking — and no other path can pick it up.
            self.store.patch_session(card_id, {"accept_forced": True})
        git_info = (card.get("session") or {}).get("git")
        if self.accept_mode() == "pr":
            result = self._accept_with_pr(card, git_info, force=force)
        else:
            repo = Path(git_info["repo"]) if git_info and git_info.get("repo") else None
            if repo is not None:
                if gitops.merge_in_progress(repo) and self.is_running(card_id):
                    # Retry accept while THIS card's resolver is live.
                    raise GitBusyError(
                        f"a conflict resolver is already running for card {card_id}")
                if not self._claim_accept_repo(repo):
                    return self._queue_accept(card, repo)
            try:
                result = self._accept_card_locked(card, git_info, force=force)
            finally:
                # A resolver owns the merge now: the repo stays locked until it
                # concludes (_conclude_resolved_merge) or fails.
                if repo is not None and not self._resolver_owns_merge(card_id):
                    self._release_accept_repo(repo)
        if result.get("status") == "accepted":
            # Shipped: the card is no longer waiting for a merge slot.
            result = self._drop_from_accept_queue(card_id) or result
        return result

    def _accept_card_locked(self, card: dict, git_info: dict | None,
                            *, force: bool = False) -> dict:
        """The accept itself, with the card's repo already claimed."""
        card_id = card["id"]
        if git_info and gitops.merge_in_progress(Path(git_info["repo"])):
            # A conflicted merge is waiting for a resolver (retry accept).
            self._start_resolver(card, git_info, "a merge is already in progress")
            return self.store.get(card_id)
        if git_info:
            root = Path(git_info["repo"])
            branch = git_info.get("branch")
            if branch and not gitops.branch_exists(root, branch) \
                    and not gitops.merge_in_progress(root):
                # Legacy card: its branch was deleted by an accept from before
                # branches were kept. Finish the accept without re-merging.
                self.store.patch_session(card_id, {"git": {
                    **git_info, "merged": True, "resolving": False,
                    "merge_conflict": False, "merge_error": None,
                    "merged_at": now_iso()}})
                accepted = self.store.accept(card_id, force=force)
                try:
                    self._drain_dependents()
                except Exception:
                    pass
                return accepted
        # Ship only green: the test command must pass in the workspace right
        # before merging/accepting (raises TestFailedError -> 409 otherwise).
        self._green_gate(card)
        if not git_info:
            accepted = self.store.accept(card_id, force=force)
        else:
            try:
                merged = gitops.merge_card_branch(git_info)
            except GitMergeConflict as exc:
                self._start_resolver(card, git_info, str(exc))
                return self.store.get(card_id)
            # The merge commit is what "this card" means: the diff view shows
            # merge_commit^..merge_commit and a future revert has one commit
            # to revert. The branch and the linked worktree are kept.
            self.store.patch_session(card_id, {"git": {
                **git_info, "merged": True, "resolving": False,
                "merge_conflict": False, "merge_error": None, **merged}})
            accepted = self.store.accept(card_id, force=force)
        # Accepted counts as completed: start drafts waiting on this card.
        try:
            self._drain_dependents()
        except Exception:
            pass
        return accepted

    def _claim_accept_repo(self, repo: Path) -> bool:
        """Take the repo's accept lock; False when another accept (or a live
        resolver) already owns a merge in that repo."""
        with self._accept_repo_lock:
            if repo in self._accepting_repos:
                return False
            self._accepting_repos.add(repo)
            return True

    def _release_accept_repo(self, repo: Path) -> None:
        """Free the repo's accept lock and merge whatever was queued behind
        it — through the existing accept drain, so no second click is needed."""
        with self._accept_repo_lock:
            self._accepting_repos.discard(repo)
        try:
            if self._next_accept_queued() is not None:
                self._start_accept_drain()
        except Exception:
            pass  # never let the queue drain break an accept

    def _resolver_owns_merge(self, card_id: str) -> bool:
        """True when a conflict resolver still owns the card's merge — the
        repo must stay locked until it concludes or fails."""
        try:
            card = self.store.get(card_id)
        except CardNotFound:
            return False
        return bool(((card.get("session") or {}).get("git") or {}).get("resolving"))

    def _queue_accept(self, card: dict, repo: Path) -> dict:
        """Another accept is merging into `repo`: queue this one instead of
        merging into the same main worktree. No exception — the UI renders
        `queue_reason` — and the drain merges it when the repo frees."""
        session = card.get("session") or {}
        return self.store.patch_session(card["id"], {
            "accept_queued": True,
            # Keep the original queue time: a second click must not jump the
            # card ahead of the ones queued before it.
            "accept_queued_at": session.get("accept_queued_at") or now_iso(),
            "queue_reason": f"waiting for the merge in {repo} to finish",
        })

    def revert_card(self, card_id: str) -> dict:
        """Undo an accepted card: revert its merge commit and reopen it.

        The card must be accepted and must carry the merge commit its accept
        recorded (``session.git.merge_commit``, or ``session.merge_commit``
        for cards that record it there). A card accepted before merge commits
        were recorded has nothing to revert -> GitError (409).

        The revert is all-or-nothing: a conflict aborts it and raises
        ``RevertConflict`` (409, with the conflicting paths) — no conflicted
        revert is left in place and no resolver session is spawned. On
        success the card goes back to draft with its session history intact
        (tokens, logs, reviews) and ``reverted_at`` / ``revert_commit``
        recorded.
        """
        card = self.store.get(card_id)                  # CardNotFound -> 404
        if card["status"] != "accepted":
            raise InvalidTransition(
                f"cannot revert card in status {card['status']!r}; only an "
                "accepted card can be reverted")
        session = card.get("session") or {}
        git_info = session.get("git") or {}
        merge_commit = session.get("merge_commit") or git_info.get("merge_commit")
        if not merge_commit:
            raise GitError(
                f"card {card_id!r} has no recorded merge commit — it was "
                "accepted before merge commits were recorded, so there is "
                "nothing to revert automatically (undo the change by hand)")
        root = git_info.get("repo")
        if not root:
            raise GitError(
                f"card {card_id!r} has no repository on record — its merge "
                "commit cannot be reverted")
        result = gitops.revert_merge_commit(Path(root), merge_commit)
        reopened = self.store.reopen_after_revert(card_id, {
            "reverted_at": result["reverted_at"],
            "revert_commit": result["revert_commit"],
            # The merge is undone: the card is no longer merged into its base.
            "git": {**git_info, "merged": False, "reverted": True,
                    "revert_commit": result["revert_commit"],
                    "reverted_at": result["reverted_at"]},
        })
        return self.annotate_card(reopened)

    # ------------------------------------------------------------ accept all

    def accept_all(self) -> dict:
        """Queue every card currently in Done and merge them one at a time.

        The snapshot is taken NOW: a card that reaches Done while the batch is
        running is not added to it — it renders below the button and the next
        click picks it up. Merges are serialized because they all happen in the
        same main worktree, and a conflicted accept hands over to a resolver
        that the batch waits for before starting the next card.

        The whole snapshot — read Done, skip what is already queued or being
        resolved, patch `accept_queued`, decide whether to start the drain — is
        ONE critical section under `_accept_lock`. The read and the patch are
        not atomic on their own: two clients clicking at the same moment would
        both snapshot the same cards and queue every one of them twice. The
        second click now finds them already queued and returns an empty batch.
        """
        with self._accept_lock:
            queued: list[str] = []
            for card in self.store.list():
                if card["status"] != "done":
                    continue
                session = card.get("session") or {}
                git_info = session.get("git") or {}
                if session.get("accept_queued") or git_info.get("resolving"):
                    continue      # already in this batch, or a resolver owns it
                self.store.patch_session(card["id"], {
                    "accept_queued": True, "accept_queued_at": now_iso(),
                    "accept_error": None})
                queued.append(card["id"])
            if queued:
                self._start_accept_drain_locked()
            return {"queued": queued, "running": self._accept_draining}

    def _start_accept_drain(self) -> None:
        """Start the batch drain unless one is already running.

        A caller that arrives while a drain is running sets the wakeup flag:
        the running drain re-checks the queue before it exits, so a card
        queued behind a repo that just freed is never left waiting.
        """
        with self._accept_lock:
            self._start_accept_drain_locked()

    def _start_accept_drain_locked(self) -> None:
        """`_start_accept_drain` with `_accept_lock` already held.

        `accept_all` decides whether to start the drain inside its snapshot
        critical section, so the flag it reports and the thread it starts
        cannot race a second click.
        """
        if self._accept_draining:
            self._accept_drain_wakeup = True
            return
        self._accept_draining = True
        self._accept_drain_wakeup = False
        threading.Thread(target=self._drain_accepts, daemon=True).start()

    def _next_accept_queued(self, skip: set[str] | None = None) -> dict | None:
        """The oldest Done card still marked accept_queued, or None.

        `skip` holds the cards this drain pass already found blocked (their
        repo is busy), so one busy repo does not stall the others.
        """
        queued = [c for c in self.store.list()
                  if c["status"] == "done"
                  and (c.get("session") or {}).get("accept_queued")
                  and c["id"] not in (skip or ())]
        if not queued:
            return None
        queued.sort(key=lambda c: ((c.get("session") or {}).get("accept_queued_at") or "",
                                   c["id"]))
        return queued[0]

    def _drain_accepts(self) -> None:
        """Merge the queued Done cards one at a time (daemon thread)."""
        skipped: set[str] = set()
        try:
            while True:
                card = self._next_accept_queued(skip=skipped)
                if card is None:
                    return
                card_id = card["id"]
                try:
                    # The drain finishes accepts the user already authorized:
                    # force rides along only when the original accept was
                    # explicitly forced (session.accept_forced) — accept-all
                    # never sets it, so an exhausted card queued by a batch
                    # is refused here and left in Done with the error.
                    self.accept_card(
                        card_id,
                        force=bool((card.get("session") or {}).get("accept_forced")))
                except Exception as exc:
                    # Green gate / git problem: leave the card in Done and drop
                    # it out of the batch so it renders below the button again.
                    self._drop_from_accept_queue(card_id, error=str(exc))
                    continue
                # A conflicted accept hands over to a resolver: wait for it, so
                # the next merge does not start in the same worktree.
                self._wait_for_resolve(card_id)
                # Re-read the card: the accept_card() return value is a
                # snapshot from BEFORE the resolver ran, and the card has
                # moved on since (accepted, conflict still open, or re-queued
                # behind a busy repo). Driving the loop off the stale snapshot
                # re-queues a failed resolver forever and leaves an accepted
                # card flagged accept_queued.
                try:
                    current = self.store.get(card_id)
                except CardNotFound:
                    continue
                session = current.get("session") or {}
                git_info = session.get("git") or {}
                if current["status"] == "done" and git_info.get("merge_conflict"):
                    # The resolver failed (or was stopped): the conflict is
                    # still open. Drop the card out of the batch — re-queuing
                    # it would spawn resolver after resolver.
                    self._drop_from_accept_queue(card_id)
                    continue
                if current["status"] == "done" and session.get("accept_queued"):
                    # The card's repo was busy: accept_card queued it behind
                    # the merge in progress. Leave it queued and try the next
                    # one — releasing the repo starts a fresh drain.
                    skipped.add(card_id)
                    continue
                self._drop_from_accept_queue(card_id)
        finally:
            with self._accept_lock:
                self._accept_draining = False
                wakeup = self._accept_drain_wakeup
                self._accept_drain_wakeup = False
            if wakeup:
                self._start_accept_drain()

    def _drop_from_accept_queue(self, card_id: str,
                                error: str | None = None) -> dict | None:
        """Take a card out of the accept queue: it is no longer waiting for a
        merge slot, so `accept_queued` / `accept_queued_at` / `queue_reason`
        are cleared (an accepted card must not keep the queue chip). `error`
        records why the attempt failed. Never raises — a drain thread must not
        die on one card."""
        patch: dict = {"accept_queued": False, "accept_queued_at": None,
                       "queue_reason": None}
        if error is not None:
            patch["accept_error"] = error
        try:
            card = self.store.get(card_id)
            session = card.get("session") or {}
            if error is None and not (session.get("accept_queued")
                                      or session.get("accept_queued_at")
                                      or session.get("queue_reason")):
                return card      # already out of the queue: nothing to write
            return self.store.patch_session(card_id, patch)
        except Exception:
            return None      # card deleted or moved on: nothing left to clear

    def _wait_for_resolve(self, card_id: str, timeout: float = 3600.0) -> None:
        """Block until the card's conflict resolver is done (or the timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                card = self.store.get(card_id)
            except CardNotFound:
                return
            git_info = (card.get("session") or {}).get("git") or {}
            if not git_info.get("resolving"):
                return
            time.sleep(1.0)

    def _accept_with_pr(self, card: dict, git_info: dict | None,
                         *, force: bool = False) -> dict:
        """Accept in `pr` mode: push the card branch and open a pull request.

        Preflight first (gh installed, `gh auth status` green, an `origin`
        remote, the branch still there) so a missing piece fails with a 409
        naming it and changes nothing. Then the same ship-only-green gate as a
        local accept, then `git push -u origin <branch>` and `gh pr create`
        with the audit trail as the body (spec, verdict, test output tail, diff
        stat). The card is accepted with `session.pr_url` / `pr_number` /
        `pr_opened_at` recorded; the branch AND its worktree are kept, because
        the PR may need follow-up commits. Nothing is merged locally.
        """
        card_id = card["id"]
        if not git_info:
            raise GitError(
                "PR accept needs a git-backed run: this card has no branch to "
                "push (run it in a project folder, or switch accept_mode to local)")
        praccept.preflight(git_info)
        gate_output = self._green_gate(card)
        card = self.store.get(card_id)
        session = card.get("session") or {}
        body = praccept.build_pr_body(
            card=card,
            review=session.get("review"),
            # The accept-time run is the freshest evidence; a card accepted
            # without a test command falls back to the recorded output.
            test_output=gate_output or session.get("test_output"),
            diff_stat=praccept.diff_stat(git_info),
            branch=git_info.get("branch"),
            base_branch=git_info.get("base_branch"),
        )
        praccept.push_branch(git_info)
        url = praccept.open_pr(git_info, title=card.get("title") or card_id,
                               body=body)
        self.store.patch_session(card_id, {
            "pr_url": url,
            "pr_number": praccept.parse_pr_number(url),
            "pr_opened_at": now_iso(),
            "git": {**git_info, "merged": False, "resolving": False,
                    "merge_conflict": False, "merge_error": None},
        })
        accepted = self.store.accept(card_id, force=force)
        # Accepted counts as completed: start drafts waiting on this card.
        try:
            self._drain_dependents()
        except Exception:
            pass
        return accepted

    def _start_resolver(self, card: dict, git_info: dict, reason: str) -> None:
        """Flag the card as resolving and spawn the conflict-resolver session."""
        card_id = card["id"]
        self.store.patch_session(card_id, {"git": {**git_info, "merged": False,
                                                  "merge_conflict": True,
                                                  "resolving": True,
                                                  "merge_error": reason}})
        try:
            self._spawn_resolver(card)
        except OSError as exc:
            self.store.patch_session(
                card_id, {"git": {**git_info, "resolving": False}})
            raise GitError(f"could not start the conflict resolver: {exc}")

    def _spawn_resolver(self, card: dict) -> None:
        """Spawn a vibe session to resolve an accept-time merge conflict.

        The card's status stays 'done'; artifacts go to sessions/<id>/resolve-N/.
        The resolver runs with cwd = the PROJECT FOLDER (the repo's main
        worktree), because the conflicted merge is left in place there — NOT
        in the card's linked worktree.
        """
        card_id = card["id"]
        git_info = (card.get("session") or {}).get("git") or {}
        sdir = self.sessions_dir / card_id
        sdir.mkdir(parents=True, exist_ok=True)
        round_no = self._next_round(card_id, "resolve")
        rdir = sdir / f"resolve-{round_no}"
        rdir.mkdir(parents=True, exist_ok=True)

        prompt_path = rdir / "prompt.md"
        prompt_path.write_text(
            resolve_prompt_template(card.get("title") or "", card["body"],
                                    git_info.get("branch") or "",
                                    git_info.get("base_branch") or ""),
            encoding="utf-8")

        ws, _is_sandbox, trust_flags = self._resolve_ws(card)
        # The resolver is its own role: it can run on its own model, and a
        # fresh session may use a different backend than the worker.
        backend = self._role_backend("resolve")
        model = self._role_model("resolve")
        log_path = rdir / "session.log"
        log_file = open(log_path, "ab")
        try:
            # kind="resolve": the resolver is its own role, so it gets its own
            # model (and, for Vibe, the same non-review flags a run gets).
            cmd = self._build_cmd(prompt_path, ws, rdir, kind="resolve",
                                  backend=backend, model=model) + trust_flags
            env = {**os.environ, **self._model_env("resolve", backend, model)}
            stdin_file = open(prompt_path, "rb") if backend.prompt_on_stdin else None
            try:
                proc = subprocess.Popen(
                    cmd, stdin=stdin_file, stdout=log_file,
                    stderr=subprocess.STDOUT, cwd=ws, start_new_session=True, env=env)
            finally:
                if stdin_file is not None:
                    stdin_file.close()
            if not backend.native_time_limit:
                self._watch_time_limit(card_id, proc)
        finally:
            log_file.close()

        with self._lock:
            self._registry[card_id] = {"proc": proc, "stop_requested": False,
                                      "kind": "resolve", "round": round_no,
                                      "resumed": False,
                                      "backend": backend.key,
                                      "model": model,
                                      "live_tokens": None, "live_breakdown": None,
                                      "vibe_session_id": None,
                                      "usage_offsets": {}, "usage_scanned_at": 0.0}
        self.store.patch_session(card_id, {
            "resolver": {"pid": proc.pid, "started_at": now_iso(),
                         "finished_at": None, "exit_code": None, "round": round_no,
                         "workdir": str(ws), "log": str(log_path),
                         "export_dir": str(rdir / "export")},
        })
        threading.Thread(target=self._finalize_resolver, args=(card_id,),
                         daemon=True).start()

    def _finalize_resolver(self, card_id: str) -> None:
        """Resolver session finished: if it concluded the merge (exit 0,
        MERGE_HEAD gone, tree clean), finish the accept — delete the card
        branch, mark merged, accept the card. Otherwise leave the card done
        with the conflict still open and `resolving` cleared.

        Like _finalize, the slot is released in a `finally` so a failure while
        recording the outcome cannot leave a stale registry entry.
        """
        with self._lock:
            entry = self._registry.get(card_id)
        if entry is None:
            return
        try:
            self._record_resolver_outcome(card_id, entry)
        finally:
            self._release_slot(card_id, entry)

    def _record_resolver_outcome(self, card_id: str, entry: dict) -> None:
        proc = entry["proc"]
        exit_code = proc.wait()

        card = self.store.get(card_id)
        session = card.get("session") or {}
        git_info = session.get("git") or {}
        resolver = session.get("resolver") or {}
        # The resolver's own cost, read from the same export.json path the run
        # and review rounds use. Without it a resolver's whole session was
        # missing from every number on the board.
        round_no = entry.get("round") or resolver.get("round") or 1
        rdir = self.sessions_dir / card_id / f"resolve-{round_no}"
        # The round's own backend: a resolver may run on a different platform
        # than the worker, and each backend writes its output somewhere else.
        backend = get_backend(entry.get("backend"))
        export = self._read_export(rdir, backend)
        self._capture_transcript(card_id, f"resolve-{round_no}", rdir)
        usage = export.get("usage") or export.get("token_usage") or {}
        resolver_patch = {
            **resolver,
            "finished_at": now_iso(),
            "exit_code": exit_code,
            "tokens": usage.get("total_tokens", usage.get("total")),
        }
        token_breakdown = (export_token_breakdown(usage)
                           if isinstance(usage, dict) else None)
        if token_breakdown is not None:
            resolver_patch["token_breakdown"] = token_breakdown
        # The resolver's own USD cost, same as its tokens.
        resolver_cost = export_cost_usd(export)
        if resolver_cost is not None:
            resolver_patch["cost_usd"] = resolver_cost
        # A refused model is why the merge is still conflicted: name it rather
        # than reporting a bare exit code.
        model_error = self._model_error(rdir, entry.get("model"), exit_code,
                                        backend)
        if model_error:
            resolver_patch["model_error"] = model_error
        root = Path(git_info["repo"]) if git_info.get("repo") else None
        concluded = (exit_code == 0 and root is not None
                     and not gitops.merge_in_progress(root)
                     and not gitops.is_dirty(root))
        # The last live total shown for the card, kept as a floor so the
        # display does not dip when the resolver's live counter goes away.
        high_water = self._live_high_water(card)
        high_water_patch = ({"live_high_water": high_water}
                            if high_water is not None else {})
        if concluded:
            self._conclude_resolved_merge(card, resolver_patch,
                                          extra_patch=high_water_patch)
        else:
            git_patch = {**git_info, "resolving": False}
            if exit_code != 0:
                git_patch["resolver_error"] = (
                    model_error or f"resolver session exited with code {exit_code}")
            self.store.patch_session(card_id, {"git": git_patch,
                                               "resolver": resolver_patch,
                                               **high_water_patch})
            # The resolver gave up on the merge: the card leaves the accept
            # queue (a retry is a deliberate click, not an endless loop) and
            # the repo is free again for the accepts queued behind it.
            self._drop_from_accept_queue(card_id)
            if root is not None:
                self._release_accept_repo(root)

    def _conclude_resolved_merge(self, card: dict,
                                 resolver_patch: dict | None = None,
                                 extra_patch: dict | None = None) -> None:
        """The in-progress merge was concluded (MERGE_HEAD gone, tree clean):
        finish the accept — post-merge green gate, record the resolver's
        commit as the card's merge commit, mark merged + resolving:false, and
        accept the card. The card branch and its linked worktree are kept (the
        diff view reads them). Used by _finalize_resolver and by startup
        recovery (a resolver that died after concluding the merge but before
        its finalize ran).

        Only concludes when the merge that finished is THIS card's: the card
        branch must be an ancestor of HEAD. A merge that belongs to another
        card (or a resolver that committed something else) leaves the card
        done with a merge_error — never accepted with someone else's commit.
        """
        card_id = card["id"]
        session = card.get("session") or {}
        git_info = session.get("git") or {}
        base_git = {**git_info, "resolving": False}
        root = Path(git_info["repo"]) if git_info.get("repo") else None

        def patch(extra_git: dict) -> None:
            p = {"git": {**base_git, **extra_git}}
            if resolver_patch is not None:
                p["resolver"] = resolver_patch
            if extra_patch:
                p.update(extra_patch)
            self.store.patch_session(card_id, p)

        try:
            if root is None:
                raise GitError("the card has no repository on record")
            branch = git_info.get("branch")
            if branch and not gitops.is_ancestor(root, branch):
                # The merge that finished is not this card's: accepting would
                # record another card's commit as this card's merge.
                patch({"merged": False, "merge_error": (
                    f"the merge in {root} is not this card's — {branch} is not "
                    "in HEAD; the card stays done for a retry")})
                return
            # Post-merge green gate: tests must pass on the merged result
            # before the accept finishes.
            self._green_gate(card)
            # The resolver concluded the merge in the main worktree: its
            # commit IS the card's merge commit. The card branch and its
            # linked worktree are kept (the diff view reads them).
            head = gitops.run_git(["rev-parse", "HEAD"], root).stdout.strip()
            patch({"merged": True, "merge_conflict": False, "merge_error": None,
                   "merge_commit": head, "merge_was_ff": False,
                   "merged_at": now_iso()})
            # force only when the original accept was explicitly forced.
            self.store.accept(card_id,
                              force=bool(session.get("accept_forced")))
        except GitError as exc:
            patch({"merge_error": str(exc)})
        except TestFailedError as exc:
            # Merge concluded but tests fail post-merge: stay done, keep the
            # branch, record the failure; a retry accept re-runs the gate
            # (the merge itself is already up to date).
            patch({"test_error": str(exc)})
        finally:
            # The accept attempt is over — accepted, or left done for a retry:
            # either way the card is no longer waiting for a merge slot, so it
            # leaves the accept queue before the repo is released (otherwise
            # the release would pick it straight back up).
            self._drop_from_accept_queue(card_id)
            # The resolver is done with the repo either way: free it so the
            # accepts queued behind it can merge.
            if root is not None:
                self._release_accept_repo(root)

    def _cancel_questions(self, card_id: str) -> None:
        """Cancel any still-pending question of a card (a long-poll waiter
        gets status "cancelled"); never let question cleanup break recovery."""
        try:
            self.questions.cancel(card_id)
        except Exception:
            pass

    def _recover_resolving(self, card: dict) -> None:
        """Startup recovery for a `done` card still flagged git.resolving (the
        server died while a conflict resolver was live).

        If the resolver process is still alive it is re-adopted, so
        _finalize_resolver finishes the accept when it exits. If the resolver
        is dead and the merge already concluded (MERGE_HEAD gone, tree
        clean), the accept is finished right away instead of leaving the card
        stuck. If MERGE_HEAD still exists, the conflict is left open for a
        resolver (a retry accept spawns a fresh one)."""
        session = card.get("session") or {}
        git_info = session.get("git") or {}
        resolver = session.get("resolver") or {}
        rpid = resolver.get("pid")
        # Spec item 4: the question waiters died with the old server — cancel
        # pending questions in every branch (adopted / concluded / left open).
        self._cancel_questions(card["id"])
        if rpid and self._pid_alive(rpid):
            self._adopt_session(card, rpid, export_dir=resolver.get("export_dir"))
            return
        root = Path(git_info["repo"]) if git_info.get("repo") else None
        if (root is not None and root.is_dir()
                and not gitops.merge_in_progress(root)
                and not gitops.is_dirty(root)):
            # The resolver died after concluding the merge but before
            # _finalize_resolver ran: finish the accept now.
            self._conclude_resolved_merge(
                card, {**resolver, "finished_at": now_iso(), "exit_code": None})

