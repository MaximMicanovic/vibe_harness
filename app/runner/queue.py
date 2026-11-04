"""QueueMixin: the run queue, dependency gating, and the concurrency slots —
`SessionRunner`'s card-starting policy, moved out of the runner core (`app/runner/__init__.py`).
"""
from __future__ import annotations



from app import gitops
from app.cards import COMPLETED_STATUSES, CardNotFound, LIVE_STATUSES


class QueueMixin:

    def _merge_in_progress_for(self, card: dict) -> bool:
        """True when the card's target repo currently has a merge in progress
        (MERGE_HEAD set) — e.g. a conflicted accept merge left in place while
        a resolver session applies it. Sandbox runs have no repo."""
        ws, is_sandbox, _ = self._resolve_ws(card)
        if is_sandbox:
            return False
        root = gitops.repo_root(ws)
        return root is not None and gitops.merge_in_progress(root)

    def _max_concurrent(self) -> int:
        """Concurrent-session cap in force right now; 0 = unlimited.

        The sidebar setting (settings.json) wins, Config.max_concurrent is the
        fallback when it is unset. Read per run, so changing the setting takes
        effect without a restart.
        """
        if self.settings is not None:
            return self.settings.max_concurrent(self.config.max_concurrent)
        return self.config.max_concurrent

    def has_pending_work(self) -> bool:
        """True when a session is live or a card is waiting to start.

        Drives the sleep guard: while this is true the machine must not sleep,
        or the session process is suspended and the CLI reports `interrupted`
        (exit 3) — a card that looks failed but was only paused.

        `waiting_for` (a dependency that has not been merged yet) is NOT work:
        nothing happens until the user merges that dependency, so holding the
        machine awake for it would keep it awake indefinitely.
        """
        if self.running_count() > 0:
            return True
        for card in self.store.list():
            session = card.get("session") or {}
            if session.get("queued") or session.get("accept_queued"):
                return True
            if (session.get("git") or {}).get("resolving"):
                return True
        return False

    def running_count(self) -> int:
        """Live sessions holding a slot — one per card, because run -> review
        -> fixer are sequential (a card in the review loop holds one slot)."""
        with self._lock:
            return len(self._registry)

    def _drain_queued_runs(self) -> None:
        """Start queued runs while slots are free, oldest queue time first.

        Two things queue a run: an in-progress merge conflict in the card's
        repo, and no free session slot (the concurrency cap). FIFO by
        `queued_at` so no card starves — `store.list()` order is
        most-recently-updated first, which would let a fresh card jump the
        queue. The loop stops as soon as the cap is reached. Called at the end
        of every session, after stop(), after a resolver concludes, and at
        startup, so queued drafts do not wait for another manual Run click.

        Serialized by `_drain_lock`: sessions end concurrently, and two drains
        interleaving between the limit check and the queue patch would start
        the same card twice. A second caller waits for the running drain and
        then re-reads the board, so no wakeup is lost.
        """
        with self._drain_lock:
            queued = []
            for card in self.store.list():
                session = card.get("session") or {}
                if not session.get("queued") or card["status"] in LIVE_STATUSES:
                    continue
                queued.append(card)
            # Cards queued before queued_at existed sort first (empty string).
            queued.sort(key=lambda c: ((c.get("session") or {}).get("queued_at") or "",
                                       c["id"]))
            for card in queued:
                limit = self._max_concurrent()
                if limit > 0 and self.running_count() >= limit:
                    return  # no free slot: the rest keep waiting, in order
                try:
                    if self._merge_in_progress_for(card):
                        continue  # still waiting for the merge conflict to resolve
                    self.store.patch_session(card["id"], {"queued": False,
                                                          "queued_at": None,
                                                          "queue_reason": None})
                    self.run(card["id"])
                except Exception:
                    pass  # raced with another action; a later drain or manual Run retries

    # ---------------------------------------------------------- dependencies

    def unmet_dependencies(self, card: dict) -> list[str]:
        """Ids from the card's depends_on that have not completed yet
        (status accepted — merged). A dependency that is merely done
        (reviewer-approved but not merged) still blocks: the dependent must
        build on the merged result. A dependency whose card was deleted counts
        as satisfied (delete() also strips the reference)."""
        unmet = []
        for dep in card.get("depends_on") or []:
            try:
                dep_card = self.store.get(dep)
            except CardNotFound:
                continue
            if dep_card["status"] not in COMPLETED_STATUSES:
                unmet.append(dep)
        return unmet

    def run_if_ready(self, card_id: str) -> dict:
        """Run a draft card now if all its dependencies are satisfied;
        otherwise mark it waiting (session.waiting_for) so _drain_dependents
        starts it automatically once they are merged. Non-drafts are returned
        unchanged, and so are drafts that neither depend on anything nor were
        waiting. Used when a dependency is added or removed (a draft linked
        onto an already-merged card runs immediately; unlinking the last
        blocking edge releases a waiting draft)."""
        card = self.store.get(card_id)
        if card["status"] != "draft":
            return card
        session = card.get("session") or {}
        was_waiting = bool(session.get("waiting_for"))
        if not card.get("depends_on") and not was_waiting:
            return card
        unmet = self.unmet_dependencies(card)
        if unmet:
            return self.store.patch_session(card_id, {
                "waiting_for": unmet,
                "queue_reason": "waiting for " + ", ".join(unmet) + " to be merged",
            })
        return self.run(card_id)

    def _drain_dependents(self) -> None:
        """Start the drafts that were waiting on dependencies (session
        waiting_for set) and whose dependencies have now all been merged
        (accepted). Called whenever a card reaches a terminal status, after an
        accept, and at startup — the accept is what actually releases them.

        Serialized by `_drain_lock` like _drain_queued_runs: two sessions
        ending together must not both start the same ready draft, and the
        starts must not interleave past the cap check.
        """
        with self._drain_lock:
            for card in self.store.list():
                if card["status"] != "draft":
                    continue
                session = card.get("session") or {}
                if not session.get("waiting_for"):
                    continue
                try:
                    if self.unmet_dependencies(card):
                        continue  # still waiting for some dependency
                    self.store.patch_session(card["id"], {
                        "waiting_for": None, "queued": False, "queued_at": None,
                        "queue_reason": None,
                    })
                    self.run(card["id"])
                except Exception:
                    pass  # raced with another action; a later drain or manual Run retries

    def _release_slot(self, card_id: str, entry: dict) -> None:
        """A session is over: drop its registry entry and start what it
        unblocked (the next queued run, then waiting dependents).

        Always runs, even when recording the outcome failed — a stale entry
        would hold a slot forever and silently lower the cap.
        """
        with self._lock:
            # Only drop the registry entry if it is still ours — a follow-up
            # session (reviewer/fixer) may have re-registered in the meantime.
            if self._registry.get(card_id) is entry:
                self._registry.pop(card_id, None)
        # This session is over: cancel any question it left pending (a waiter
        # gets status "cancelled"; the card stops showing it as live).
        try:
            self.questions.cancel(card_id)
        except Exception:
            pass  # never let question cleanup break the loop
        # A session slot just freed: start the next queued run (capacity queue
        # or merge-conflict queue), oldest queue time first.
        try:
            self._drain_queued_runs()
        except Exception:
            pass  # never let the queue drain break the loop
        # The card may have reached a terminal status (done/failed/stopped):
        # start any drafts that were waiting on it and are now unblocked.
        try:
            self._drain_dependents()
        except Exception:
            pass  # never let the dependency drain break the loop

