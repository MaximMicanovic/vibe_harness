"""SessionRunner: spawn, track, stop, and finalize headless vibe CLI sessions.

One vibe subprocess per card at a time, working in an isolated workspace under
``sessions/<card_id>/workspace`` (or the card's project folder). Every
implementation run is followed by a review pass — a second, read-only vibe
session (the ``ask`` agent) that inspects the work. If the reviewer approves,
the card becomes ``done`` (acceptable); if it rejects, the card goes back to
``running`` and a fixer session receives the rejection reason. The loop repeats
until approved, capped at ``config.max_review_rounds``; when the cap is hit the
card lands in ``done`` WITHOUT an approved review (``review.exhausted``) and
must be accepted manually. Runs in a real work folder (project folder or
workdir override — not the sandbox) are git-backed: the folder is ``git init``-ed
if needed, the card runs on its own branch ``<prefix>/<card_id>``, each
successful round is committed, and accepting the card merges the branch into
the base branch. Run metadata
(tokens/model from vibe's export.json) is persisted into the card's
frontmatter via CardStore.

This module is the CORE of the runner; the cohesive pieces it used to hold
inline live in their own modules under ``app/runner/`` now, composed here as mixins (a pure
refactor — the public ``SessionRunner`` API is unchanged):

- ``app/runner/queue.py`` — ``QueueMixin``: the run queue, dependency gating, and the
  concurrency slots (``run_if_ready``, ``_drain_queued_runs``, ...).
- ``app/runner/review.py`` — ``ReviewMixin``: the review loop (verdict reading,
  diff/test gates, fixer rounds).
- ``app/runner/accept.py`` — ``AcceptMixin``: accept/revert and git orchestration
  (local merge, PR, conflict resolver, per-repo accept queue).
- ``app/runner/supervise.py`` — ``SuperviseMixin``: process spawn/stop/timeout,
  the session registry, outcome recording, restart recovery, and the
  backend/model/env resolution for spawned sessions.
- ``app/runner/prompts.py`` — the prompt/rule templates, verdict parsing, and small
  shared helpers.

Every name this module exported before the split is re-exported below (see
``__all__``), so ``app/main.py`` and the tests keep importing from
``app.runner`` unchanged.

Review loop v2 — token-efficient routing:

- **Persistent worker**: the first run spawns a fresh vibe session; every
  fixer round after a rejection RESUMES that session (``vibe --resume
  <session_id> --prompt-file <round>/prompt.md``), so the worker keeps its
  cached context instead of re-reading the whole workspace. The session id is
  read from each round's ``export.json`` and stored as
  ``session.worker_session_id``.
- **Fresh reviewer, diff-scoped**: every review round is a NEW read-only vibe
  process (no resume), but its prompt only carries the spec, the
  ``git diff <last-reviewed-commit>..HEAD`` in the card's worktree, and the
  test-suite output — small context, independent judgment.
- **MCP mailbox per card**: the harness writes a ``.vibe/config.toml`` into
  the card's worktree attaching ``app/mailbox_server.py`` (stdio MCP) with
  ``post_message`` / ``wait_for_message`` / ``submit_verdict``. The fresh
  reviewer can question the persistent worker mid-review; the verdict arrives
  as the structured ``submit_verdict`` call. Verdicts are scoped PER REVIEW
  ROUND: each session gets ``VIBE_HARNESS_MAILBOX_ROUND=<kind>-<n>`` and
  ``submit_verdict`` (reviewer role only) writes ``verdicts/<round>.json``;
  the harness clears that file before spawning the round and reads only that
  file after it, so a round that never calls submit_verdict falls back to
  ``VERDICT:`` text instead of inheriting the previous round's verdict.
  The harness itself only supervises: round cap, accept/merge, token
  accounting, and the mailbox lifecycle (setup/teardown).
"""
from __future__ import annotations

import subprocess  # noqa: F401  # kept so tests can monkeypatch app.runner.subprocess.Popen

import shutil
import threading
import time
from pathlib import Path
from app import failures, gitops, liveusage, mailbox
from app.cards import CardNotFound, CardRunning, CardStore, FINISHED_STATUSES, LIVE_STATUSES
from app.config import Config, SESSIONS_DIR
from app.models import ModelSettings
from app.projects import ProjectStore
from app.questions import QuestionStore
from app.sleepguard import SleepGuard
from app.tokens import TOKEN_BREAKDOWN_KEYS, card_cost_usd, card_token_breakdown, card_tokens, combine_live
from app.transcripts import DB_FILENAME, HARNESS_DIR, TranscriptStore, project_store
from app.settings import SettingsStore

from .queue import QueueMixin
from .review import ReviewMixin
from .accept import AcceptMixin
from .supervise import SuperviseMixin
from .prompts import (
    REVIEW_DIFF_MAX_CHARS,
    REVIEW_DUPLICATION_MAX_CLONES,
    REVIEW_TEST_OUTPUT_MAX_CHARS,
    WorkdirNotAllowed,
    _VERDICT_RE,
    _num,
    duplication_section,
    export_token_breakdown,
    fix_prompt_template,
    mailbox_rules,
    mcp_rules,
    merge_token_breakdown,
    now_iso,
    parse_verdict,
    prompt_template,
    resolve_prompt_template,
    resolve_workdir,
    reuse_rules,
    review_prompt_template,
    tdd_rules,
    worker_rules,
)
from .supervise import (
    MCP_SERVER_PATH,
    NotRunningError,
    STOP_GRACE_SECONDS,
    TestFailedError,
    _AdoptedProc,
    _UNSET,
)

__all__ = [
    "LIVE_USAGE_SCAN_INTERVAL",
    "MCP_SERVER_PATH",
    "NotRunningError",
    "REVIEW_DIFF_MAX_CHARS",
    "REVIEW_DUPLICATION_MAX_CLONES",
    "REVIEW_TEST_OUTPUT_MAX_CHARS",
    "STOP_GRACE_SECONDS",
    "SessionRunner",
    "TOKEN_BREAKDOWN_KEYS",
    "TestFailedError",
    "WorkdirNotAllowed",
    "_AdoptedProc",
    "_UNSET",
    "_VERDICT_RE",
    "_num",
    "duplication_section",
    "export_token_breakdown",
    "fix_prompt_template",
    "mailbox_rules",
    "mcp_rules",
    "merge_token_breakdown",
    "now_iso",
    "parse_verdict",
    "prompt_template",
    "resolve_prompt_template",
    "resolve_workdir",
    "reuse_rules",
    "review_prompt_template",
    "tdd_rules",
    "worker_rules",
]

# Minimum seconds between live-token journal scans per live card. Matches the
# dashboard's 2s poll, so the UI updates live without extra scan traffic.
LIVE_USAGE_SCAN_INTERVAL = 2.0

class SessionRunner(QueueMixin, ReviewMixin, AcceptMixin, SuperviseMixin):
    def __init__(self, store: CardStore, config: Config, sessions_dir: Path = SESSIONS_DIR,
                 projects: ProjectStore | None = None,
                 questions: QuestionStore | None = None,
                 models: ModelSettings | None = None,
                 settings: "SettingsStore | None" = None,
                 transcripts: TranscriptStore | None = None):
        self.store = store
        self.config = config
        self.sessions_dir = Path(sessions_dir)
        self.projects = projects
        # Per-role model choice (worker / review / resolver). None = every
        # session runs on the user's own active_model.
        self.models = models
        # Which agent CLI to drive (vibe / claude). None = the default backend.
        self.settings = settings
        # Compressed session transcripts: every finalized round's
        # export/session/ directory moves into the store of the card's PROJECT
        # (see transcripts_for), so a project's transcripts live with it. This
        # is the harness store: the rounds of cards with no project (sandbox
        # runs) and the fallback when a card's project is gone. It lives beside
        # the sessions it indexes (BASE_DIR in production, the temp dir in
        # tests); VIBE_HARNESS_TRANSCRIPTS_DB still wins.
        self.transcripts = transcripts or TranscriptStore(
            base_dir=self.sessions_dir.parent / HARNESS_DIR)
        # project path -> its store, so one project does not open a new
        # connection per call. Guarded by _transcripts_lock (reentrant: the
        # migration below resolves stores while holding it).
        self._transcript_stores: dict[str, TranscriptStore] = {}
        self._transcripts_lock = threading.RLock()
        # The one-time drain of the old harness-root store runs on first use.
        self._transcripts_migrated = False
        # Per-card Q&A (model asks the user on the card via the MCP ask_user
        # tool); shared with the app's HTTP endpoints when the app built us.
        self.questions = questions or QuestionStore(self.sessions_dir)
        # card_id -> {"proc": Popen, "stop_requested": bool}; guarded by _lock.
        self._registry: dict[str, dict] = {}
        self._lock = threading.Lock()
        # Serializes queue drains: every session end and every stop() drains,
        # and two drains running at once would both see the same queued card
        # and start it twice (a duplicate session leaves a stale registry
        # entry, which silently shrinks the concurrency cap). Also serializes
        # the dependency drain, which starts sessions the same way.
        self._drain_lock = threading.Lock()
        # Serializes the cap check + slot registration in run(): the check and
        # the registry insert must be atomic, or two starts both pass the check
        # and the cap is exceeded. Lock order is _drain_lock -> _start_lock ->
        # _lock; nothing takes them in the other direction.
        self._start_lock = threading.Lock()
        # Serializes live-token journal scans (API handlers may race).
        self._usage_lock = threading.Lock()
        # "Accept all": one drain thread merges the queued Done cards one at a
        # time (they all merge in the same main worktree, so they cannot run
        # concurrently). Guarded by _accept_lock.
        self._accept_lock = threading.Lock()
        self._accept_draining = False
        # Set when a drain is asked to run while one is already running: the
        # running drain re-checks the queue before it exits, so a card queued
        # behind a repo that just freed is never left waiting.
        self._accept_drain_wakeup = False
        # Serializes accepts PER REPOSITORY: every accept merges in the repo's
        # MAIN worktree, and git allows exactly one merge in progress there.
        # A repo stays in the set while a resolver owns its merge and is
        # released when the resolver concludes or fails. Lock order is
        # _accept_repo_lock -> _accept_lock; nothing takes them the other way.
        self._accept_repo_lock = threading.Lock()
        self._accepting_repos: set[Path] = set()
        # Keeps the machine awake while cards run or wait: a sleeping machine
        # suspends the session process and the CLI reports `interrupted`.
        self.sleep_guard = SleepGuard(enabled=config.prevent_sleep)
    # ------------------------------------------------------------------ run

    def run(self, card_id: str) -> dict:
        card = self.store.get(card_id)
        if card["status"] in LIVE_STATUSES:
            raise CardRunning(f"card {card_id} is already running")
        if card["status"] in FINISHED_STATUSES:
            # A re-run starts fresh: clear the previous session/acceptance.
            card = self.store.reset_to_draft(card_id)
        unmet = self.unmet_dependencies(card)
        if self._merge_in_progress_for(card):
            # A merge conflict is being resolved in the target repo right now
            # (a conflicted accept merge left in place for a resolver): queue
            # the run instead of failing with 409 — it starts automatically
            # once the merge concludes (see _drain_queued_runs).
            patch: dict = {
                "queued": True,
                "queue_reason": "waiting for the in-progress merge conflict to resolve",
            }
            if unmet:
                patch["waiting_for"] = unmet
            self.store.patch_session(card_id, patch)
            return self.store.get(card_id)
        if unmet:
            # The card depends on other cards that are not merged yet: do not
            # start it now — mark it as waiting; _drain_dependents starts it
            # automatically once every dependency is accepted (merged).
            self.store.patch_session(card_id, {
                "waiting_for": unmet,
                "queue_reason": "waiting for " + ", ".join(unmet) + " to be merged",
            })
            return self.store.get(card_id)
        # The cap check and the slot registration must be atomic: two Run
        # clicks (or a drain racing a click) that both pass the check would
        # start two sessions at the cap. `_start_lock` is held across the whole
        # start path — the git prep (worktree add) is exactly the window the
        # check protects, and it is wide enough to lose the race in practice.
        with self._start_lock:
            # Re-read: another start may have won while we waited for the lock.
            card = self.store.get(card_id)
            if card["status"] in LIVE_STATUSES:
                raise CardRunning(f"card {card_id} is already running")
            limit = self._max_concurrent()
            running = self.running_count()
            if limit > 0 and running >= limit:
                # No free session slot: queue the run instead of spawning
                # another session (and worktree, and test suite).
                # _drain_queued_runs starts it, oldest queue time first, as
                # soon as a session ends.
                session = card.get("session") or {}
                self.store.patch_session(card_id, {
                    "queued": True,
                    # Keep the original queue time: pressing Run again must not
                    # jump the card ahead of the ones queued before it.
                    "queued_at": session.get("queued_at") or now_iso(),
                    "queue_reason": f"waiting for a free slot ({running}/{limit} running)",
                })
                return self.store.get(card_id)
            git_patch = self._prepare_git(card)
            workdir = Path(git_patch["worktree"]) if git_patch else None
            extra_patch: dict = {"queued": False, "queued_at": None,
                                 "waiting_for": None}
            if git_patch:
                extra_patch["git"] = git_patch
            if self.config.mailbox:
                # Per-card MCP mailbox: state under sessions/<id>/mailbox + the
                # stdio server attached via .vibe/config.toml in the work folder
                # (the card's linked worktree for git-backed runs, else sandbox).
                ws0 = workdir if workdir is not None else self._resolve_ws(card)[0]
                extra_patch["mailbox"] = mailbox.setup_mailbox(
                    ws0, self.sessions_dir / card_id)
            self._spawn(card_id, kind="run",
                        prompt=prompt_template(card["body"], self._mcp_name(),
                                               tdd=self.config.tdd,
                                               mailbox=self._prompt_mailbox("run", card)),
                        extra_patch=extra_patch or None,
                        workdir=workdir)
        return self.store.get(card_id)
    def _mcp_name(self) -> str | None:
        """MCP server alias for prompts/env, or None when the MCP is disabled."""
        return self.config.mcp_server_name if self.config.mcp_enabled else None
    def _prompt_mailbox(self, kind: str, card: dict | None = None,
                        resume: str | None = None) -> bool:
        """Whether this spawn's prompt may mention the mailbox MCP tools.

        The mailbox server is attached via the worktree's `.vibe/config.toml`,
        which only Vibe reads — a session on another backend has no mailbox
        tools, so its prompts must not instruct it to call them. Gated on the
        backend actually used for that spawn (the same resolution `_spawn`
        performs), plus the mailbox kill-switch.
        """
        return (self.config.mailbox
                and self._session_backend(kind, card, resume).key == "vibe")
    def _resolve_ws(self, card: dict, *, use_worktree: bool = False) -> tuple[Path, bool, list[str]]:
        """Effective work folder: (ws, is_sandbox, trust_flags).

        The card's `workdir` override, else its project's folder, else the
        isolated sandbox under sessions/<id>/workspace. Custom folders are
        trusted for this invocation only (--trust) and must pass the
        allowlist.

        With use_worktree=True, a git-backed card's per-card linked worktree
        (sessions/<id>/worktree, recorded in session.git.worktree) replaces
        the project folder: the vibe run/review subprocesses work there (on
        the card branch), not in the project folder itself.
        """
        raw_workdir = (card.get("workdir") or "").strip()
        if not raw_workdir and card.get("project") and self.projects is not None:
            project = self.projects.get(card["project"])
            if project:
                raw_workdir = project["path"]
        if raw_workdir:
            ws = resolve_workdir(raw_workdir)
            if not self.config.workdir_allowed(ws):
                raise WorkdirNotAllowed(
                    f"workdir {ws} is not in VIBE_HARNESS_ALLOWED_WORKDIRS")
            if use_worktree:
                git_info = (card.get("session") or {}).get("git") or {}
                worktree = git_info.get("worktree")
                if worktree:
                    wt = Path(worktree)
                    wt.mkdir(parents=True, exist_ok=True)
                    return wt, False, ["--trust"]
            ws.mkdir(parents=True, exist_ok=True)
            return ws, False, ["--trust"]
        ws = self.sessions_dir / card["id"] / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        return ws, True, []
    def _prepare_git(self, card: dict) -> dict | None:
        """Git setup for runs in a real work folder (not the sandbox).

        The card runs on its own branch (<prefix>/<card_id>) checked out in a
        per-card linked worktree at sessions/<id>/worktree; the project folder
        (the repo's main worktree) stays on its base branch, so several cards
        in the same repository can run concurrently. The folder is `git
        init`-ed if it is not a repository yet. Leftover uncommitted changes
        in the main worktree (e.g. from an interrupted previous run) are
        stashed automatically — kept in the stash for manual recovery — and
        flagged `stashed`/`auto_stashed` in the returned git record, so a
        dirty tree never blocks a run. Returns the session patch (including
        the `worktree` path), or None for sandbox runs (no git). Raises
        GitError subclasses (-> HTTP 409) on repo-state problems (e.g. an
        unresolved merge in the repo).
        """
        ws, is_sandbox, _ = self._resolve_ws(card)
        if is_sandbox:
            return None
        branch = f"{self.config.git_branch_prefix}/{card['id']}"
        worktree = self.sessions_dir / card["id"] / "worktree"
        return gitops.prepare_card_branch(ws, branch, worktree)
    # ------------------------------------------------------- transcripts

    def transcripts_for(self, card: dict | None) -> TranscriptStore:
        """The store a card's transcripts belong to, cached per project path.

        `<project>/.vibe-harness/transcripts.db`, so a project's transcripts
        live with the project they belong to. A card with no project (a
        sandbox run) — or one whose project no longer exists — uses the
        harness store. VIBE_HARNESS_TRANSCRIPTS_DB still wins: one explicit
        store for everything.
        """
        self.migrate_transcripts()
        path = self._project_path(card)
        if path is None:
            return self.transcripts
        key = str(path)
        with self._transcripts_lock:
            store = self._transcript_stores.get(key)
            if store is None:
                store = project_store(path)
                self._transcript_stores[key] = store
            return store
    def _project_path(self, card: dict | None) -> Path | None:
        """The card's project folder, or None (no project / project gone)."""
        name = (card or {}).get("project")
        if not name or self.projects is None:
            return None
        path = (self.projects.get(name) or {}).get("path")
        return Path(path) if path else None
    def _card_or_none(self, card_id: str) -> dict | None:
        """The card, or None when it was deleted (its rounds are still stored)."""
        try:
            return self.store.get(card_id)
        except CardNotFound:
            return None
    def migrate_transcripts(self) -> dict:
        """Drain the old harness-root store into the per-project stores.

        One-time (on first use) and idempotent: `<harness root>/transcripts.db`
        used to hold every project's rounds. Each row moves to the store of its
        card's project; a row whose card has no project — or whose project is
        gone — stays in the harness store, never dropped. The old file is
        removed once it is empty. Never raises: a broken migration must not
        fail a round.
        """
        with self._transcripts_lock:
            if self._transcripts_migrated:
                return {"moved": 0, "kept": 0}
            self._transcripts_migrated = True
            # The harness root is a repository too: keep its own store out of
            # git the same way (a no-op when it is not one).
            gitops.exclude_from_git(self.sessions_dir.parent, f"{HARNESS_DIR}/")
            try:
                return self._migrate_transcripts()
            except Exception as exc:
                print(f"WARNING: transcript migration failed: {exc!r}")
                return {"moved": 0, "kept": 0}
    def _migrate_transcripts(self) -> dict:
        legacy = TranscriptStore(db_path=self.sessions_dir.parent / DB_FILENAME)
        if not legacy.db_path.exists():
            return {"moved": 0, "kept": 0}
        rows_by_card: dict[str, list[dict]] = {}
        for row in legacy.list():
            rows_by_card.setdefault(row["card_id"], []).append(row)
        moved = kept = 0
        for card_id in sorted(rows_by_card):
            rows = rows_by_card[card_id]
            target = self.transcripts_for(self._card_or_none(card_id))
            if target.db_path == legacy.db_path:
                # One explicit store (VIBE_HARNESS_TRANSCRIPTS_DB): the rows
                # are already where they belong.
                kept += len(rows)
                continue
            try:
                moved += target.copy_from(legacy, {card_id})
            except Exception as exc:
                print(f"WARNING: could not migrate transcripts for {card_id}: {exc!r}")
                kept += len(rows)
                continue
            legacy.delete(card_id)
        if legacy.stats()["rows"] == 0:
            legacy.remove()
        return {"moved": moved, "kept": kept}
    def transcript_stats(self) -> dict:
        """Per-store rows plus the totals: every project store + the harness one.

        One row per database file, so it is obvious where the bytes are. The
        harness's own project resolves to the harness store (the same file), so
        it is reported once.
        """
        self.migrate_transcripts()
        stores: list[tuple[str | None, TranscriptStore]] = [(None, self.transcripts)]
        for project in (self.projects.list() if self.projects is not None else []):
            stores.append((project["name"],
                           self.transcripts_for({"project": project["name"]})))
        rows: list[dict] = []
        seen: set[str] = set()
        for name, store in stores:
            key = str(store.db_path)
            if key in seen:
                continue
            seen.add(key)
            rows.append({"project": name, "path": key, **store.stats()})
        raw_bytes = sum(row["raw_bytes"] for row in rows)
        gz_bytes = sum(row["gz_bytes"] for row in rows)
        return {"rows": sum(row["rows"] for row in rows),
                "raw_bytes": raw_bytes, "gz_bytes": gz_bytes,
                "ratio": round(raw_bytes / gz_bytes, 2) if gz_bytes else 0.0,
                "stores": rows}
    def backfill_transcripts(self) -> dict:
        """Import every on-disk round into the store of its card's project."""
        self.migrate_transcripts()
        return self.transcripts.backfill(
            self.sessions_dir,
            store_for=lambda card_id: self.transcripts_for(
                self._card_or_none(card_id)))
    def _capture_transcript(self, card_id: str, round_name: str, rdir: Path) -> None:
        """Move one round's session transcript into the card's project store.

        `export/session/` is the vibe journal + chunks: worth keeping for
        debugging, but ~1 GB of loose files. It is stored as one tar.gz blob
        and then removed; `export.json` stays, it is what the harness reads.

        Never raises: a failed save leaves the directory in place (nothing is
        lost) and must not fail the round.
        """
        session_dir = rdir / "export" / "session"
        if not session_dir.is_dir():
            return
        try:
            self.transcripts_for(self._card_or_none(card_id)).save(
                card_id, round_name, session_dir)
        except Exception as exc:
            print(f"WARNING: could not store transcript {card_id} {round_name}: {exc!r}")
            return
        shutil.rmtree(session_dir, ignore_errors=True)
    # ------------------------------------------------------- live token usage

    def live_usage(self, card_id: str) -> dict | None:
        """Best-effort live token usage of the card's current vibe session.

        Returns {"tokens": int, "cumulative": bool, "breakdown": dict | None}
        or None. `cumulative` is True when the round RESUMED the worker session
        (`vibe --resume`), so the live counter already includes earlier rounds;
        False for a fresh session (run-1, every review, resolvers), where it
        must be added to the card's recorded total (`card_tokens`). `breakdown`
        is the per-type split ({"cached", "output", "input"}) of the same live
        counter, for the hover tooltip on the token counters.

        Low impact by construction: the scan runs at most once per
        LIVE_USAGE_SCAN_INTERVAL per live card, reads only the bytes appended
        to the session journal since the last scan, and does zero I/O when the
        card has no live registry entry (the common case). All errors are
        swallowed — live display is optional, export.json stays authoritative.
        """
        with self._lock:
            entry = self._registry.get(card_id)
        if entry is None:
            return None
        proc = entry.get("proc")
        if proc is not None and proc.poll() is not None:
            # Process exited; _finalize records the authoritative export.json
            # numbers. Keep showing the last live value in the meantime.
            tokens = entry.get("live_tokens")
            if tokens is None:
                return None
            return {"tokens": tokens, "cumulative": bool(entry.get("resumed")),
                    "breakdown": entry.get("live_breakdown")}
        now = time.monotonic()
        if now - entry.get("usage_scanned_at", 0.0) >= LIVE_USAGE_SCAN_INTERVAL:
            with self._usage_lock:
                # Re-check under the scan lock: another request may have just
                # scanned this card.
                if now - entry.get("usage_scanned_at", 0.0) >= LIVE_USAGE_SCAN_INTERVAL:
                    entry["usage_scanned_at"] = now
                    try:
                        home = liveusage.vibe_home()
                        sid = entry.get("vibe_session_id")
                        if not sid and proc is not None:
                            sid = liveusage.find_session_id_by_pid(home, proc.pid)
                        if sid:
                            entry["vibe_session_id"] = sid
                            found = liveusage.scan_session_journal(
                                home, sid, entry.setdefault("usage_offsets", {}))
                            if found is not None:
                                prev = entry.get("live_tokens")
                                # Keep the total and its breakdown in sync: the
                                # breakdown always describes the max-total record.
                                if prev is None or found["total"] >= prev:
                                    entry["live_tokens"] = found["total"]
                                    bd = {k: found.get(k) for k in TOKEN_BREAKDOWN_KEYS}
                                    entry["live_breakdown"] = (
                                        bd if any(v is not None for v in bd.values())
                                        else None)
                                else:
                                    entry["live_tokens"] = prev
                    except Exception:
                        pass  # best effort — never break the API over live stats
        tokens = entry.get("live_tokens")
        if tokens is None:
            return None
        return {"tokens": tokens, "cumulative": bool(entry.get("resumed")),
                "breakdown": entry.get("live_breakdown")}
    def _live_high_water(self, card: dict) -> int | None:
        """The card's last displayed total, or None when no live value was seen.

        Called when a round ends: its process has exited, so `live_usage`
        returns the last scanned value without touching the journal. The value
        is persisted as `session.live_high_water` so the card keeps showing it
        until the next round's export lands — the number never dips at a round
        boundary.
        """
        live = self.live_usage(card["id"])
        if live is None:
            return None
        return combine_live(card_tokens(card.get("session") or {}), live)
    def annotate_card(self, card: dict) -> dict:
        """Return the card for API responses, with the derived fields attached.

        `session.failure` is the classifier's `{kind, detail}` for a card that
        failed (None/absent for one that did not) — the board's badge and the
        MCP's get_card/list_cards both read it from here, so the reason is
        computed once, in one place.

        `session.card_tokens` is what the card actually cost in tokens: the
        worker's export total plus every review and resolver round
        (`app/tokens.py`). The chip, /api/stats and /api/split-report all read
        this one number, so they cannot disagree. `session.card_cost_usd` is
        the same aggregate in USD (None when no round exported a cost).

        While a session is live, `session.live_tokens` is the per-card total
        shown on the card and summed into /api/stats: the live counter itself
        for resumed (cumulative) rounds, the card total plus the fresh round's
        live counter otherwise. `session.live_token_breakdown` carries the same
        combination for the per-type split (cached/output/input) shown on
        hover. The store's card is never mutated — the session dict is copied.

        A card in 'done' also gets a top-level `warnings` list when the accept
        gate has something loud to say: no test command configured (the green
        gate is off) and/or an exhausted review that needs the force override.
        """
        out = dict(card)
        session = card.get("session")
        if isinstance(session, dict):
            new_session = dict(session)
            failure = failures.classify(card)
            if failure is None:
                new_session.pop("failure", None)
            else:
                new_session["failure"] = failure
            total = card_tokens(session)
            # A card that never recorded a cost (no export, no rounds) keeps
            # showing no token chip, as before.
            if total or session.get("tokens") is not None:
                new_session["card_tokens"] = total
            card_breakdown = card_token_breakdown(session)
            if card_breakdown is not None:
                new_session["card_token_breakdown"] = card_breakdown
            card_cost = card_cost_usd(session)
            # A card whose rounds never exported a cost keeps showing no cost
            # chip, as before (None = unknown, not $0).
            if card_cost is not None:
                new_session["card_cost_usd"] = card_cost
            out["session"] = new_session
        live = self.live_usage(card["id"])
        if live is not None:
            session = out.get("session") or {}
            recorded = session.get("card_tokens") or 0
            value = combine_live(recorded, live)
            new_session = {**session, "live_tokens": value}
            breakdown = merge_token_breakdown(card_token_breakdown(session),
                                              live.get("breakdown"), live["cumulative"])
            if breakdown is not None:
                new_session["live_token_breakdown"] = breakdown
            out["session"] = new_session
        # Accept-gate warnings for a card waiting in Done: attached here (not
        # persisted) so the board AND the MCP see them from /api/cards.
        if card.get("status") == "done":
            warnings: list[str] = []
            if not self.config.test_cmd_configured:
                warnings.append(
                    "no test command configured (test_cmd unset) — accepting "
                    "this card is NOT gated by tests")
            review = (card.get("session") or {}).get("review") or {}
            if review.get("approved") is not True and review.get("exhausted") is True:
                warnings.append(
                    "review rounds exhausted without an approval — accepting "
                    "this card requires the explicit force override")
            if warnings:
                out["warnings"] = warnings
        return out

