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

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from app import gitops, liveusage, mailbox
from app.backends import Backend, get_backend
from app.config import BASE_DIR, SESSIONS_DIR, Config, resolve_path
from app.gitops import GitBusyError, GitError, GitMergeConflict
from app.cards import (
    COMPLETED_STATUSES,
    FINISHED_STATUSES,
    LIVE_STATUSES,
    CardNotFound,
    CardRunning,
    CardStore,
    InvalidTransition,
)
from app.projects import ProjectStore
from app.models import ModelSettings
from app.questions import QuestionStore

# Grace period between SIGTERM and SIGKILL when stopping a session.
STOP_GRACE_SECONDS = 10

# Minimum seconds between live-token journal scans per live card. Matches the
# dashboard's 2s poll, so the UI updates live without extra scan traffic.
LIVE_USAGE_SCAN_INTERVAL = 2.0

# The harness MCP server (app/mcp_server.py), injected into spawned vibe
# sessions via the VIBE_MCP_SERVERS env var so the model can talk to the board.
MCP_SERVER_PATH = Path(__file__).resolve().parent / "mcp_server.py"

# Token-type keys of a breakdown dict, in display order (cached, output, input).
TOKEN_BREAKDOWN_KEYS = ("cached", "output", "input")


def _num(value) -> int | None:
    """A finite int for numeric values (bool excluded), else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def export_token_breakdown(usage: dict) -> dict | None:
    """Per-type token breakdown from an export.json `usage` dict, or None.

    Real vibe 3.x export.json usage: {"total_tokens": N, "input_tokens": …,
    "output_tokens": …, "cached_input_tokens": …} (snake_case). Alternate key
    names are probed defensively; unknown types stay None. Returns None when
    no type is known at all. Keys: {"cached", "output", "input"}.
    """
    def first(*keys: str) -> int | None:
        for k in keys:
            v = _num(usage.get(k))
            if v is not None:
                return v
        return None

    breakdown = {
        "cached": first("cached_input_tokens", "cached_input",
                        "cache_read_input_tokens"),
        "output": first("output_tokens", "output"),
        "input": first("input_tokens", "input"),
    }
    return breakdown if any(v is not None for v in breakdown.values()) else None


def merge_token_breakdown(recorded: dict | None, live: dict | None,
                          cumulative: bool) -> dict | None:
    """Combine the recorded (export.json) and live (journal) breakdowns the
    same way `annotate_card` combines the totals: per-type max when the live
    counter is cumulative (resumed session), per-type sum for a fresh round.
    Types unknown on both sides stay None. Returns None when neither side
    knows any type.
    """
    def has_types(bd: dict | None) -> bool:
        return bool(bd) and any(v is not None for v in bd.values())

    if not has_types(recorded) and not has_types(live):
        return None
    out = {}
    for key in TOKEN_BREAKDOWN_KEYS:
        r = (recorded or {}).get(key)
        l = (live or {}).get(key)
        if r is None and l is None:
            out[key] = None
        elif cumulative:
            out[key] = max(v for v in (r, l) if v is not None)
        else:
            out[key] = (r or 0) + (l or 0)
    return out

# Rule block telling spawned models about the harness MCP: judge first whether
# the spec is too big for one session and, if so, split it into cards.
def mcp_rules(mcp_name: str) -> str:
    return (
        f"- You are connected to this harness dashboard through the `{mcp_name}` MCP "
        f"server (tool group `mcp_{mcp_name}`, tools like `harness_list_cards`, "
        "`harness_create_card`, `harness_run_card`, ...). Use it to talk to the board.\n"
        "- FIRST judge whether this spec is too big for one session. If it is, split "
        "it: create one card per subtask with `harness_create_card` (new cards start "
        "as drafts and inherit this card's project; set auto_run=true to run one "
        "immediately, or leave it in draft and run it later with `harness_run_card`). "
        "The MCP has no way to create, delete, or switch projects.\n"
        "- To chain sub-cards so one runs after another completes (done/accepted), "
        "link them with `harness_add_dependency(card_id=<dependent>, "
        "depends_on=<this card>)` — the dependent starts automatically when this "
        "card completes; see `harness_list_dependencies`.\n"
        "- To archive finished work, use the archive tools: `harness_create_archive` "
        "(a typed folder under archives/), `harness_archive_card` / "
        "`harness_archive_cards` (move accepted cards — or a whole deck of them — "
        "into an archive; they leave the board), `harness_list_archives` / "
        "`harness_get_archive` (see what is archived), and `harness_restore_card` "
        "(bring a card back to the board).\n"
        f"- When the spec is genuinely ambiguous and a wrong guess would be costly, "
        f"ask the user with `harness_ask_user(question=..., options=[...])`: the "
        "question appears ON THIS CARD'S CARD on the dashboard and the tool call "
        "BLOCKS until the user answers on the card (or cancels, or the timeout hits). "
        "The result carries the answer (status \"answered\"), or status "
        "\"timeout\"/\"cancelled\" — then make a reasonable choice and record it in "
        "NOTES.md. Use it sparingly: for minor ambiguity, just pick a sensible "
        "default and card the choice in NOTES.md.\n"
    )

# Rule block telling spawned models to work test-driven: when the model takes
# a card and starts working on it, it writes a failing test first (red),
# implements the smallest change that makes it pass (green), refactors only
# while green, and leaves the full suite green before finishing.
def tdd_rules() -> str:
    return (
        "- Follow test-driven development (TDD) while working on this card:\n"
        "  1. RED — write a failing test first that captures the spec (or the "
        "bug), run it, and confirm it fails for the right reason.\n"
        "  2. GREEN — implement the smallest change that makes the test pass.\n"
        "  3. REFACTOR — clean up only while the tests stay green.\n"
        "  4. Before finishing, run the FULL test suite and keep it green; add "
        "tests for every behavior you add or change. If the project has no "
        "tests yet, create a minimal test file first — do not ship untested "
        "code.\n"
    )


# Cap on the diff embedded in a review prompt (keeps the fresh reviewer's
# context small — the point of diff-scoped review).
REVIEW_DIFF_MAX_CHARS = 60_000

# Cap on the test-suite output embedded in a review prompt.
REVIEW_TEST_OUTPUT_MAX_CHARS = 4_000


class WorkdirNotAllowed(Exception):
    """Raised by run() when a card's workdir is outside the configured allowlist."""


def resolve_workdir(raw: str, base_dir: Path = BASE_DIR) -> Path:
    """Resolve a workdir setting: `~` expanded, relative -> base_dir (harness root)."""
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = base_dir / p
    return p.resolve()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prompt_template(body: str, mcp_name: str | None = "harness",
                    *, tdd: bool = True) -> str:
    rules = (
        "- If an `agent.md` file exists in the workspace, read it and follow its "
        "rules (it may ask you to keep `route.md` up to date).\n"
    )
    if tdd:
        rules += tdd_rules()
    if mcp_name:
        rules += mcp_rules(mcp_name)
    return (
        "You are a Mistral Vibe agent. A user wrote the following spec as a "
        "post-it card. Implement it completely in the current working directory.\n\n"
        "Rules:\n"
        + rules +
        "- If something is ambiguous, make a reasonable choice and record it in "
        "a NOTES.md file in the workspace.\n"
        "- Implement, then verify (run tests/build if applicable).\n"
        "- Work autonomously; do not ask questions.\n\n"
        "# Spec\n\n" + body
    )


def fix_prompt_template(body: str, reason: str, mcp_name: str | None = "harness",
                        resumed: bool = False, *, tdd: bool = True) -> str:
    rules = (
        "- If an `agent.md` file exists in the workspace, read it and follow its "
        "rules (it may ask you to keep `route.md` up to date).\n"
    )
    if tdd:
        rules += tdd_rules()
    if mcp_name:
        rules += mcp_rules(mcp_name)
    resume_card = (
        "\n\nYou are RESUMING your previous session (`vibe --resume <session_id>`): "
        "all earlier context (the workspace you already read, the spec, the "
        "previous review) is retained — do NOT re-read the whole workspace; focus "
        "on the rejection reason below.\n"
        if resumed else ""
    )
    return (
        "You are a Mistral Vibe agent. A user wrote the following spec as a "
        "post-it card. A previous attempt was REJECTED by the automated reviewer.\n"
        + resume_card + "\n"
        "Reviewer's rejection reason:\n" + reason + "\n\n"
        "Fix the issues raised by the reviewer and complete the spec. Implement "
        "completely in the current working directory.\n\n"
        "Rules:\n"
        + rules +
        "- If something is ambiguous, make a reasonable choice and record it in "
        "a NOTES.md file in the workspace.\n"
        "- If the rejection reason contains test output, fix the code so the "
        "test command passes.\n"
        "- Implement, then verify (run tests/build if applicable).\n"
        "- Work autonomously; do not ask questions.\n\n"
        "# Spec\n\n" + body
    )


def review_prompt_template(body: str, diff: str | None = None,
                           diff_range: str | None = None,
                           test_output: str | None = None) -> str:
    """Fresh-reviewer prompt, scoped small on purpose: the
    spec + the diff since the last reviewed commit + the harness-run test
    output. The reviewer may ask the persistent worker questions through the
    harness-mailbox MCP tools and must submit a structured verdict via
    `submit_verdict` (VERDICT: text is only a fallback)."""
    parts = [
        "You are the automated reviewer in a code-production pipeline. A worker "
        "implemented the spec below in the current working directory.\n\n"
        "You are a FRESH reviewer with deliberately small context: review ONLY "
        "what is provided below (spec + diff + test output), plus targeted reads "
        "of the workspace when the diff is unclear. Do NOT re-read the whole "
        "workspace.\n\n"
        "# Spec\n\n" + body + "\n\n",
    ]
    if diff is not None:
        parts.append(
            "## Changes under review"
            + (f" (`git diff {diff_range}` in the worktree)\n\n" if diff_range else "\n\n")
            + "```diff\n" + diff + "\n```\n\n"
        )
    else:
        parts.append(
            "## Changes under review\n\n"
            "No git diff is available for this run (sandbox workspace, no git). "
            "Inspect the workspace directly, but keep your reads targeted.\n\n"
        )
    if test_output is not None:
        parts.append(
            "## Test-suite output (run by the harness in the worktree)\n\n"
            "```\n" + test_output + "\n```\n\n"
        )
    parts.append(
        "Review the changes for bugs, correctness, edge cases, and completeness "
        "against the spec. You may also run the project's test suite in the "
        "workspace yourself; a failing test suite means rejection. Do NOT modify "
        "any source files; this is a read-only review.\n\n"
        "## Mailbox (MCP)\n\n"
        "The `harness-mailbox` MCP server is attached to this session with tools:\n"
        "- `post_message(to, text)` — message the other party (`to` is `worker` "
        "or `reviewer`).\n"
        "- `wait_for_message()` — block until a message addressed to you arrives.\n"
        "- `submit_verdict(approved, reason)` — submit your final verdict.\n\n"
        "The worker session persists across fix rounds and remembers the earlier "
        "context: if anything in the diff is unclear, ask it with "
        "`post_message(to=\"worker\", ...)` and then `wait_for_message()` instead "
        "of guessing.\n\n"
        "When you are done, you MUST call "
        "`submit_verdict(approved=<true|false>, reason=<one sentence>)` — the "
        "harness reads that structured call as your verdict. As a fallback, also "
        "end your final answer with exactly one line, either:\n"
        "VERDICT: APPROVED\n"
        "or:\n"
        "VERDICT: REJECTED: <one-sentence reason>\n"
    )
    return "".join(parts)


def resolve_prompt_template(title: str, body: str, branch: str, base: str) -> str:
    return (
        "You are a Mistral Vibe agent acting as a merge-conflict resolver in a "
        "code-production pipeline.\n\n"
        f'A post-it card titled "{title}" was implemented on the git branch '
        f"`{branch}` and is being merged into `{base}` as part of accepting it. "
        "The merge CONFLICTED and is currently in progress in the working "
        "directory (conflict markers `<<<<<<<` / `=======` / `>>>>>>>`).\n\n"
        "The card's spec — the work that must survive the merge:\n\n"
        "# Spec\n\n" + body + "\n\n"
        "Your job:\n"
        "- Resolve every conflict so the merged result keeps BOTH sides' intent: "
        "the base branch's changes AND the card's implementation of the spec.\n"
        "- When the two are truly incompatible, prefer the card's implementation, "
        "but keep unrelated base-branch changes intact.\n"
        "- Verify the result (run tests/build if applicable).\n"
        "- Conclude the merge: `git add` the resolved files, then `git commit` "
        "(this finishes the in-progress merge). Do NOT run `git merge --abort` "
        "unless resolution is impossible.\n"
        "- If an `agent.md` file exists in the workspace, read it and follow its "
        "rules (it may ask you to keep `route.md` up to date).\n"
        "- Work autonomously; do not ask questions.\n"
    )


_VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVED|REJECTED)\b[:\-]?\s*(.*)")


def parse_verdict(text: str) -> tuple[bool, str | None] | None:
    """Extract the reviewer's verdict; the last VERDICT: line wins.

    Returns (approved, reason) or None when the output has no verdict line.
    """
    matches = _VERDICT_RE.findall(text)
    if not matches:
        return None
    verdict, rest = matches[-1]
    reason = rest.strip() or None
    return (verdict == "APPROVED", reason)


class _AdoptedProc:
    """Shim for a session process re-adopted by pid after a server restart.

    The process is not our child (start_new_session detached it), so waitpid
    is impossible; poll() checks liveness with os.kill(pid, 0) and the exit
    code is read from the round's export.json, which a clean vibe exit (or a
    SIGTERM/time-limit stop, exit 3) always writes.
    """

    def __init__(self, pid: int, export_path: Path):
        self.pid = pid
        self._export_path = export_path
        self._exit: int | None = None

    def _alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
            return True
        except OSError:
            return False

    def _read_exit_code(self) -> int | None:
        try:
            export = json.loads(self._export_path.read_text(encoding="utf-8"))
            code = export.get("exit_code")
            return code if isinstance(code, int) else None
        except Exception:
            return None

    def poll(self) -> int | None:
        if self._alive():
            return None
        if self._exit is None:
            self._exit = self._read_exit_code()
        return self._exit

    def wait(self) -> int | None:
        while self._alive():
            time.sleep(0.5)
        return self.poll()


class NotRunningError(Exception):
    """Raised by stop() when the card has no live session."""


class TestFailedError(Exception):
    """The configured test command failed at a gate point (maps to HTTP 409)."""


class SessionRunner:
    def __init__(self, store: CardStore, config: Config, sessions_dir: Path = SESSIONS_DIR,
                 projects: ProjectStore | None = None,
                 questions: QuestionStore | None = None,
                 models: ModelSettings | None = None,
                 settings: "SettingsStore | None" = None):
        self.store = store
        self.config = config
        self.sessions_dir = Path(sessions_dir)
        self.projects = projects
        # Per-role model choice (worker / review / resolver). None = every
        # session runs on the user's own active_model.
        self.models = models
        # Which agent CLI to drive (vibe / claude). None = the default backend.
        self.settings = settings
        # Per-card Q&A (model asks the user on the card via the MCP ask_user
        # tool); shared with the app's HTTP endpoints when the app built us.
        self.questions = questions or QuestionStore(self.sessions_dir)
        # card_id -> {"proc": Popen, "stop_requested": bool}; guarded by _lock.
        self._registry: dict[str, dict] = {}
        self._lock = threading.Lock()
        # Serializes live-token journal scans (API handlers may race).
        self._usage_lock = threading.Lock()

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
            # The card depends on other cards that have not completed yet:
            # do not start it now — mark it as waiting; _drain_dependents
            # starts it automatically once every dependency completes.
            self.store.patch_session(card_id, {
                "waiting_for": unmet,
                "queue_reason": "waiting for " + ", ".join(unmet) + " to complete",
            })
            return self.store.get(card_id)
        git_patch = self._prepare_git(card)
        workdir = Path(git_patch["worktree"]) if git_patch else None
        extra_patch: dict = {"queued": False, "waiting_for": None}
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
                                           tdd=self.config.tdd),
                    extra_patch=extra_patch or None,
                    workdir=workdir)
        return self.store.get(card_id)

    def _mcp_name(self) -> str | None:
        """MCP server alias for prompts/env, or None when the MCP is disabled."""
        return self.config.mcp_server_name if self.config.mcp_enabled else None

    def _model_env(self, kind: str) -> dict:
        """VIBE_ACTIVE_MODEL for one session kind, or {} when the role inherits.

        Vibe's config layers put VIBE_* env vars above the user's config.toml,
        so this pins the model for this one spawned session — the global config
        is never rewritten, and two sessions cannot race each other's model.
        """
        model = self.models.model_for_session(kind) if self.models else None
        return {"VIBE_ACTIVE_MODEL": model} if model else {}

    def _session_env(self, card_id: str) -> dict:
        """Environment for a spawned vibe session.

        Injects the harness MCP server into VIBE_MCP_SERVERS (vibe reads it as
        JSON) so the model gets the mcp_<name> tool group. Any servers already
        present in the inherited VIBE_MCP_SERVERS are kept; an entry with our
        name is replaced. The MCP server process gets VIBE_HARNESS_URL (which
        dashboard to talk to) and VIBE_HARNESS_NOTE_ID (which card this session
        implements) through its env map.
        """
        env = dict(os.environ)
        name = self.config.mcp_server_name
        servers: list = []
        raw = env.get("VIBE_MCP_SERVERS")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    servers = [s for s in parsed
                               if isinstance(s, dict) and s.get("name") != name]
            except ValueError:
                servers = []
        servers.append({
            "name": name,
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(MCP_SERVER_PATH)],
            "env": {
                "VIBE_HARNESS_URL": self.config.api_base_url(),
                "VIBE_HARNESS_NOTE_ID": card_id,
                "VIBE_HARNESS_ASK_USER_TIMEOUT": str(self.config.ask_user_timeout_seconds),
            },
        })
        env["VIBE_MCP_SERVERS"] = json.dumps(servers)
        # Never let an ambient per-card mailbox (e.g. the harness itself
        # running inside a vibe session) leak into a spawned session — the
        # card's own mailbox vars are set by _spawn when configured.
        for key in [k for k in env if k.startswith("VIBE_HARNESS_MAILBOX")]:
            del env[key]
        return env

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

    def _merge_in_progress_for(self, card: dict) -> bool:
        """True when the card's target repo currently has a merge in progress
        (MERGE_HEAD set) — e.g. a conflicted accept merge left in place while
        a resolver session applies it. Sandbox runs have no repo."""
        ws, is_sandbox, _ = self._resolve_ws(card)
        if is_sandbox:
            return False
        root = gitops.repo_root(ws)
        return root is not None and gitops.merge_in_progress(root)

    def _adopt_session(self, card: dict, pid: int,
                       export_dir: str | None = None) -> None:
        """Re-register a session process that survived a server restart/reload
        (start_new_session detaches it, so it is not our child and we cannot
        waitpid it). The _AdoptedProc shim polls pid liveness; the exit code
        comes from the round's export.json, which a clean vibe exit writes.

        `export_dir` overrides the round dir used to derive the session kind
        (run/review/resolve) and to read the exit code — pass the resolver's
        export_dir to re-adopt a conflict resolver (kind parses as "resolve").
        """
        card_id = card["id"]
        session = card.get("session") or {}
        if export_dir is None:
            export_dir = session.get("export_dir") or ""
        rdir = Path(export_dir).parent if export_dir else self.sessions_dir / card_id
        parts = rdir.name.split("-")
        kind = parts[0] if parts and parts[0] else "run"
        try:
            round_no = int(parts[-1]) if len(parts) > 1 else 1
        except ValueError:
            round_no = 1
        with self._lock:
            if card_id in self._registry:
                return
            entry = {"proc": _AdoptedProc(pid, rdir / "export" / "export.json"),
                     "kind": kind, "round": round_no,
                     "stop_requested": False, "adopted": True}
            self._registry[card_id] = entry
        target = self._finalize_resolver if kind == "resolve" else self._finalize
        threading.Thread(target=target, args=(card_id,), daemon=True).start()

    def _drain_queued_runs(self) -> None:
        """Start the runs that were queued behind an in-progress merge
        conflict, for cards whose target repo no longer has a merge in
        progress. Called when a resolver concludes a merge (and at startup),
        so queued drafts do not wait for another manual Run click."""
        for card in self.store.list():
            session = card.get("session") or {}
            if not session.get("queued") or card["status"] in LIVE_STATUSES:
                continue
            try:
                if self._merge_in_progress_for(card):
                    continue  # still waiting for the merge conflict to resolve
                self.store.patch_session(card["id"], {"queued": False,
                                                      "queue_reason": None})
                self.run(card["id"])
            except Exception:
                pass  # raced with another action; a later drain or manual Run retries

    # ---------------------------------------------------------- dependencies

    def unmet_dependencies(self, card: dict) -> list[str]:
        """Ids from the card's depends_on that have not completed yet
        (status done/accepted). A dependency whose card was deleted counts
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
        starts it automatically once they complete. Non-drafts are returned
        unchanged, and so are drafts that neither depend on anything nor were
        waiting. Used when a dependency is added or removed (a draft linked
        onto an already-completed card runs immediately; unlinking the last
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
                "queue_reason": "waiting for " + ", ".join(unmet) + " to complete",
            })
        return self.run(card_id)

    def _drain_dependents(self) -> None:
        """Start the drafts that were waiting on dependencies (session
        waiting_for set) and whose dependencies have now all completed
        (done/accepted). Called whenever a card reaches a terminal status,
        after an accept, and at startup."""
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
                    "waiting_for": None, "queued": False, "queue_reason": None,
                })
                self.run(card["id"])
            except Exception:
                pass  # raced with another action; a later drain or manual Run retries

    def accept_card(self, card_id: str) -> dict:
        """Accept a done card: gate first, then merge its git branch (if any).

        A merge conflict does NOT fail the accept: the conflicted merge is left
        in place, the card stays 'done' (Done column) flagged `resolving`, and
        a resolver vibe session is spawned. When the resolver concludes the
        merge, the accept finishes automatically (merge recorded -> accepted).
        A second accept while a resolver is live raises GitBusyError (-> 409).

        The merge result is recorded on the session git record
        (``merge_commit`` / ``merge_was_ff`` / ``merged_at``) and the card
        branch + linked worktree are KEPT, so the card's diff stays
        inspectable after accept (``GET /api/cards/<id>/diff``).
        """
        card = self.store.check_accept(card_id)
        git_info = (card.get("session") or {}).get("git")
        if git_info and gitops.merge_in_progress(Path(git_info["repo"])):
            # A conflicted merge is waiting for a resolver (retry accept).
            if self.is_running(card_id):
                raise GitBusyError(
                    f"a conflict resolver is already running for card {card_id}")
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
                accepted = self.store.accept(card_id)
                try:
                    self._drain_dependents()
                except Exception:
                    pass
                return accepted
        # Ship only green: the test command must pass in the workspace right
        # before merging/accepting (raises TestFailedError -> 409 otherwise).
        self._green_gate(card)
        if not git_info:
            accepted = self.store.accept(card_id)
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
            accepted = self.store.accept(card_id)
        # Accepted counts as completed: start drafts waiting on this card.
        try:
            self._drain_dependents()
        except Exception:
            pass
        return accepted

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
        backend = self.backend()
        log_path = rdir / "session.log"
        log_file = open(log_path, "ab")
        try:
            cmd = self._build_cmd(prompt_path, ws, rdir, kind="run",
                                  backend=backend) + trust_flags
            # The resolver is its own role: it can run on its own model.
            env = {**os.environ, **self._model_env("resolve")}
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
        with the conflict still open and `resolving` cleared."""
        with self._lock:
            entry = self._registry.get(card_id)
        if entry is None:
            return
        proc = entry["proc"]
        exit_code = proc.wait()

        card = self.store.get(card_id)
        session = card.get("session") or {}
        git_info = session.get("git") or {}
        resolver = session.get("resolver") or {}
        resolver_patch = {**resolver, "finished_at": now_iso(), "exit_code": exit_code}
        root = Path(git_info["repo"]) if git_info.get("repo") else None
        concluded = (exit_code == 0 and root is not None
                     and not gitops.merge_in_progress(root)
                     and not gitops.is_dirty(root))
        if concluded:
            self._conclude_resolved_merge(card, resolver_patch)
        else:
            git_patch = {**git_info, "resolving": False}
            if exit_code != 0:
                git_patch["resolver_error"] = (
                    f"resolver session exited with code {exit_code}")
            self.store.patch_session(card_id, {"git": git_patch,
                                               "resolver": resolver_patch})

        with self._lock:
            if self._registry.get(card_id) is entry:
                self._registry.pop(card_id, None)

        # The merge state may have changed: start any runs that were queued
        # behind this conflict (no-op while a merge is still in progress).
        try:
            self._drain_queued_runs()
        except Exception:
            pass  # never let the queue drain break the resolver finalize
        # The accept may have completed the card: start waiting dependents.
        try:
            self._drain_dependents()
        except Exception:
            pass

    def _conclude_resolved_merge(self, card: dict,
                                 resolver_patch: dict | None = None) -> None:
        """The in-progress merge was concluded (MERGE_HEAD gone, tree clean):
        finish the accept — post-merge green gate, record the resolver's
        commit as the card's merge commit, mark merged + resolving:false, and
        accept the card. The card branch and its linked worktree are kept (the
        diff view reads them). Used by _finalize_resolver and by startup
        recovery (a resolver that died after concluding the merge but before
        its finalize ran)."""
        card_id = card["id"]
        session = card.get("session") or {}
        git_info = session.get("git") or {}
        base_git = {**git_info, "resolving": False}

        def patch(extra_git: dict) -> None:
            p = {"git": {**base_git, **extra_git}}
            if resolver_patch is not None:
                p["resolver"] = resolver_patch
            self.store.patch_session(card_id, p)

        try:
            # Post-merge green gate: tests must pass on the merged result
            # before the accept finishes.
            self._green_gate(card)
            # The resolver concluded the merge in the main worktree: its
            # commit IS the card's merge commit. The card branch and its
            # linked worktree are kept (the diff view reads them).
            head = gitops.run_git(["rev-parse", "HEAD"],
                                  Path(git_info["repo"])).stdout.strip()
            patch({"merged": True, "merge_conflict": False, "merge_error": None,
                   "merge_commit": head, "merge_was_ff": False,
                   "merged_at": now_iso()})
            self.store.accept(card_id)
        except GitError as exc:
            patch({"merge_error": str(exc)})
        except TestFailedError as exc:
            # Merge concluded but tests fail post-merge: stay done, keep the
            # branch, record the failure; a retry accept re-runs the gate
            # (the merge itself is already up to date).
            patch({"test_error": str(exc)})

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

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
        # else: no repo / MERGE_HEAD still set — leave the conflict open for
        # a resolver (a retry accept spawns a fresh one).

    def backend(self) -> Backend:
        """The agent CLI this session will drive (vibe / claude)."""
        key = self.settings.backend() if self.settings else None
        return get_backend(key)

    def _build_cmd(self, prompt_path: Path, ws: Path, rdir: Path,
                   kind: str, resume: str | None = None,
                   backend: Backend | None = None,
                   mcp_config: Path | None = None) -> list[str]:
        """The command line for one session. kind: 'run' or 'review'.

        `resume` (a session id from a previous round's export) resumes that
        session instead of starting a new one — used for fixer rounds so the
        persistent worker keeps its cached context. Reviewers are always fresh
        (no resume). The flags themselves belong to the backend.
        """
        backend = backend or self.backend()
        return backend.build_cmd(config=self.config, prompt_path=prompt_path,
                                 ws=ws, rdir=rdir, kind=kind, resume=resume,
                                 mcp_config=mcp_config)

    def _claude_mcp_config(self, rdir: Path, card_id: str) -> Path:
        """Write the `--mcp-config` file that gives a Claude run the board tools.

        Vibe reads the same server from VIBE_MCP_SERVERS; Claude wants a JSON
        file with an `mcpServers` map. Same command, same env, so the tools
        behave identically whichever backend spawned the session.
        """
        config = {
            "mcpServers": {
                self.config.mcp_server_name: {
                    "command": sys.executable,
                    "args": [str(MCP_SERVER_PATH)],
                    "env": {
                        "VIBE_HARNESS_URL": self.config.api_base_url(),
                        "VIBE_HARNESS_NOTE_ID": card_id,
                        "VIBE_HARNESS_ASK_USER_TIMEOUT": str(self.config.ask_user_timeout_seconds),
                    },
                }
            }
        }
        path = rdir / "mcp.json"
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return path

    def _watch_time_limit(self, card_id: str, proc) -> None:
        """Terminate a session that outlives `time_limit_seconds`.

        Only used for backends without a native time limit (Claude). Vibe
        enforces its own `--time-limit` and writes the export on the way out;
        here the harness has to stop the process, so the round ends as a
        timeout rather than a clean finish.
        """
        limit = max(1, int(self.config.time_limit_seconds))

        def watch() -> None:
            try:
                proc.wait(timeout=limit)
                return
            except subprocess.TimeoutExpired:
                pass
            with self._lock:
                entry = self._registry.get(card_id)
                if entry is not None and entry.get("proc") is proc:
                    entry["stop_requested"] = True
                    entry["timed_out"] = True
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    proc.terminate()
                except OSError:
                    pass

        threading.Thread(target=watch, daemon=True).start()

    def _spawn(self, card_id: str, *, kind: str, prompt: str,
               extra_patch: dict | None = None,
               workdir: Path | None = None,
               resume: str | None = None) -> None:
        """Start one vibe session. kind: 'run' (implement/fix) or 'review'.

        The card transitions to 'running' or 'reviewing'; artifacts for this
        round live in sessions/<id>/<kind>-<n>/. extra_patch is merged into the
        session record (e.g. the previous run's outcome, or a review record).
        `workdir` overrides the effective work folder (the caller passes the
        card's linked git worktree for the first run of a git-backed card);
        otherwise it resolves via _resolve_ws, which prefers the linked
        worktree recorded in session.git for git-backed cards.

        `resume` is a vibe session id (from the previous run round's
        export.json): the session is resumed with `vibe --resume <id>` so the
        persistent worker keeps its context across fix rounds. Only worker
        ('run') rounds are ever resumed; reviewers are always fresh.
        """
        card = self.store.get(card_id)
        sdir = self.sessions_dir / card_id
        sdir.mkdir(parents=True, exist_ok=True)
        round_no = self._next_round(card_id, kind)
        rdir = sdir / f"{kind}-{round_no}"
        rdir.mkdir(parents=True, exist_ok=True)

        prompt_path = rdir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        if workdir is not None:
            # Explicit work folder (the card's linked worktree): a real work
            # folder, trusted for this invocation only.
            ws, trust_flags = Path(workdir), ["--trust"]
        else:
            ws, _is_sandbox, trust_flags = self._resolve_ws(card, use_worktree=True)

        # Per-card MCP mailbox: hand the spawned vibe process the mailbox dir,
        # this side's role, and this round's key; the stdio server (attached
        # via the worktree's .vibe/config.toml) inherits them.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("VIBE_HARNESS_MAILBOX")}
        if self.config.mailbox:
            mdir = mailbox.mailbox_dir(self.sessions_dir, card_id)
            if mdir.is_dir():
                round_key = f"{kind}-{round_no}"
                if kind == "review":
                    # A fresh review round must not inherit an earlier round's
                    # structured verdict: clear THIS round's verdict file
                    # before the reviewer process starts.
                    mailbox.clear_round_verdict(mdir, round_key)
                env.update({
                    "VIBE_HARNESS_MAILBOX_DIR": str(mdir),
                    "VIBE_HARNESS_MAILBOX_ROLE": ("worker" if kind == "run"
                                                  else "reviewer"),
                    "VIBE_HARNESS_MAILBOX_ROUND": round_key,
                    "VIBE_HARNESS_MAILBOX_SERVER": str(mailbox.SERVER_SCRIPT)})

        backend = self.backend()
        log_path = rdir / "session.log"
        log_file = open(log_path, "ab")
        try:
            # Run sessions (implement/fix) get the harness MCP server; review
            # and resolver sessions stay MCP-free. Vibe takes it through
            # VIBE_MCP_SERVERS, Claude through a --mcp-config file.
            mcp_config = None
            if kind == "run" and self.config.mcp_enabled:
                if backend.key == "claude":
                    mcp_config = self._claude_mcp_config(rdir, card_id)
                else:
                    run_env = self._session_env(card_id)
                    if env:
                        run_env.update(env)
                    env = run_env
            cmd = self._build_cmd(prompt_path, ws, rdir, kind, resume=resume,
                                  backend=backend, mcp_config=mcp_config) + trust_flags
            env.update(self._model_env(kind))
            stdin_file = open(prompt_path, "rb") if backend.prompt_on_stdin else None
            try:
                proc = subprocess.Popen(
                    cmd, stdin=stdin_file, stdout=log_file,
                    stderr=subprocess.STDOUT, cwd=ws, start_new_session=True, env=env)
            finally:
                if stdin_file is not None:
                    stdin_file.close()
            if not backend.native_time_limit:
                # Claude has no --time-limit: enforce it here, the same way the
                # Stop button does, so a stuck session cannot run forever.
                self._watch_time_limit(card_id, proc)
        finally:
            # The child owns its own dup of the fd; drop the parent's copy.
            log_file.close()

        with self._lock:
            self._registry[card_id] = {"proc": proc, "stop_requested": False,
                                      "kind": kind, "round": round_no,
                                      "resumed": resume is not None,
                                      "live_tokens": None, "live_breakdown": None,
                                      "vibe_session_id": None,
                                      "usage_offsets": {}, "usage_scanned_at": 0.0}

        patch = {
            "pid": proc.pid,
            "started_at": now_iso(),
            "finished_at": None,
            "exit_code": None,
            "workdir": str(ws),
            "log": str(log_path),
            "export_dir": str(rdir / "export"),
        }
        if kind == "review":
            rounds = (card.get("session") or {}).get("review_rounds") or 0
            patch["review_rounds"] = rounds + 1
        if extra_patch:
            patch.update(extra_patch)
        status = "reviewing" if kind == "review" else "running"
        self.store.set_status(card_id, status, session_patch=patch)

        threading.Thread(target=self._finalize, args=(card_id,), daemon=True).start()

    def _next_round(self, card_id: str, kind: str) -> int:
        """Per-kind session counter: run-1, review-1, run-2, review-2, ..."""
        sdir = self.sessions_dir / card_id
        highest = 0
        if sdir.is_dir():
            for child in sdir.iterdir():
                m = re.fullmatch(rf"{kind}-(\d+)", child.name)
                if m and child.is_dir():
                    highest = max(highest, int(m.group(1)))
        return highest + 1

    def _read_export(self, rdir: Path, backend: Backend | None = None) -> dict:
        """One round's output, normalized to the harness's export shape.

        Each backend knows where its own output lands (vibe: export/export.json,
        claude: the JSON result on stdout, teed into session.log) and how to map
        it onto `usage` / `session_id` / `config.active_model` / `stop_reason`.
        """
        return (backend or self.backend()).read_export(rdir)

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

    def _review_diff(self, session: dict, ws: Path) -> tuple[str | None, str | None]:
        """The diff a fresh reviewer should look at: `git diff
        <last-reviewed-commit>..HEAD` in the card's worktree.

        The base is the commit the previous review round covered
        (``session.last_reviewed_commit``), else the MERGE-BASE of the card
        branch and its base branch. The merge-base matters: diffing from the
        base TIP makes a branch that is merely behind its base look like it
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
            base = session.get("last_reviewed_commit")
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

    def _green_gate(self, card: dict) -> None:
        """Ship-only-green gate at accept: the test command must pass in the
        workspace right before the card is accepted/merged. Raises
        TestFailedError (-> 409) on failure; records verified=True on pass.
        """
        if not self.config.test_cmd:
            return
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
                                                   tdd=self.config.tdd),
                        extra_patch=extra, resume=resume)

    # -------------------------------------------------------------- finalize

    def _finalize(self, card_id: str) -> None:
        """Runs in a daemon thread: wait for exit, record the outcome, and
        drive the review loop."""
        with self._lock:
            entry = self._registry.get(card_id)
        if entry is None:
            return
        proc = entry["proc"]
        kind = entry["kind"]

        exit_code = proc.wait()
        card = self.store.get(card_id)
        sdir = self.sessions_dir / card_id
        rdir = sdir / f"{kind}-{entry['round']}"
        session = card.get("session") or {}
        ws = Path(session.get("workdir") or (sdir / "workspace"))

        export = self._read_export(rdir)
        usage = export.get("usage") or export.get("token_usage") or {}
        tokens = usage.get("total_tokens", usage.get("total"))
        # Per-type breakdown (cached/output/input) for the token-counter hover
        # tooltip; None when the export carries only a total.
        token_breakdown = export_token_breakdown(usage) if isinstance(usage, dict) else None
        export_config = export.get("config") or export.get("effective_config") or {}
        model = export_config.get("active_model") or export_config.get("model")
        stop_reason = export.get("stop_reason")
        # The vibe session id — the resume key for the next fixer round.
        worker_session_id = export.get("session_id")

        if kind == "run":
            # --- test gate: run the test command in the workspace ---
            # None when no test command is configured, else (passed, output).
            tested = None
            if exit_code == 0 and self.config.test_cmd:
                tested = self._run_tests(ws)
            verified = None if tested is None else tested[0]

            outcome = {
                "finished_at": now_iso(),
                "exit_code": exit_code,
                "verified": verified,
                "model": model,
                "tokens": tokens,
                "stop_reason": stop_reason,
            }
            if token_breakdown is not None:
                outcome["token_breakdown"] = token_breakdown
            if worker_session_id:
                outcome["worker_session_id"] = worker_session_id
            if entry["stop_requested"]:
                self.store.set_status(card_id, "stopped", session_patch=outcome)
            elif exit_code != 0:
                self.store.set_status(card_id, "failed", session_patch=outcome)
            else:
                # Success -> commit the result on the card branch (when the
                # run is git-backed), run the test gate, then hand over to
                # the reviewer — or loop back to a fixer when tests fail.
                git_info = session.get("git")
                commit_error = None
                if git_info:
                    title = " ".join((card.get("title") or "card").split())[:80]
                    try:
                        gitops.commit_workspace(
                            Path(session.get("workdir") or ws),
                            f"vibe-harness: {title} (card {card_id})")
                    except GitError as exc:
                        commit_error = str(exc)
                if commit_error:
                    self.store.set_status(card_id, "failed", session_patch={
                        **outcome, "git_error": commit_error})
                elif tested is not None and not tested[0]:
                    # Test-gate rejection: back to running, the fixer gets
                    # the test output as the rejection reason; the run
                    # outcome (verified=False, tokens, ...) is recorded too.
                    tail = tested[1][-4000:]
                    self._loop_reject(card_id, card,
                                      f"the test command failed:\n{tail}",
                                      source="test",
                                      session_patch={
                                          "test_output": tail,
                                          **{k: v for k, v in outcome.items()
                                             if k != "finished_at"},
                                      })
                else:
                    # Loop step 1: hand over to a FRESH reviewer, scoped to
                    # the diff since the last reviewed commit + the test
                    # output (small context, independent judgment).
                    handover = {k: v for k, v in outcome.items() if k != "finished_at"}
                    diff_text = diff_range = None
                    if self.config.review_diff:
                        diff_text, diff_range = self._review_diff(session, ws)
                    test_tail = None
                    if tested is not None:
                        test_tail = tested[1][-REVIEW_TEST_OUTPUT_MAX_CHARS:]
                    self._spawn(card_id, kind="review",
                                prompt=review_prompt_template(card["body"],
                                                              diff_text, diff_range,
                                                              test_tail),
                                extra_patch=handover)
        else:  # review
            # Verdict channel 1 (preferred): the structured `submit_verdict`
            # MCP tool call, recorded by the mailbox server as this round's
            # verdicts/review-<n>.json (reviewer role only). Reading ONLY this
            # round's file means a review round that never called
            # submit_verdict cannot inherit the previous round's verdict.
            verdict = None
            via = None
            mdir = mailbox.mailbox_dir(self.sessions_dir, card_id)
            if self.config.mailbox:
                structured = mailbox.read_verdict(
                    mdir, round=f"review-{entry['round']}")
                if structured is not None:
                    verdict, via = structured, "submit_verdict"
            # Fallback channel 2: VERDICT: text in the log / REVIEW.md.
            if verdict is None:
                logged = self._read_verdict(rdir, ws)
                if logged is not None:
                    verdict, via = logged, "log"
            if verdict is None:
                approved, reason = False, "reviewer did not produce a verdict"
            else:
                approved, reason = verdict
            review_record = {
                "approved": approved,
                "reason": reason,
                "round": entry["round"],
                "tokens": tokens,
                # The reviewer's model, shown next to the diff it judged.
                "model": model,
                "log": str(rdir / "session.log"),
                "export_dir": str(rdir / "export"),
                "via": via,
            }
            if token_breakdown is not None:
                review_record["token_breakdown"] = token_breakdown
            # Supervision bookkeeping: which commit this review covered (the
            # next review diffs from it) and how many mailbox messages were
            # exchanged so far.
            review_patch: dict = {}
            head = self._reviewed_commit(session)
            if head:
                review_record["commit"] = head
                review_patch["last_reviewed_commit"] = head
            # Review history: every round's record is kept (session.review
            # stays the LAST one for backward compatibility), so the diff
            # panel can show what each round judged.
            review_patch["reviews"] = [*(session.get("reviews") or []),
                                       review_record]
            if self.config.mailbox and mdir.is_dir():
                review_record["messages"] = len(mailbox.list_messages(mdir))
            if entry["stop_requested"]:
                self.store.set_status(card_id, "stopped", session_patch={
                    "finished_at": now_iso(),
                    "exit_code": exit_code,
                })
            elif approved:
                # Loop step 2a: approved -> done, then auto-accept if asked.
                done = self.store.set_status(card_id, "done", session_patch={
                    "review": review_record,
                    "finished_at": now_iso(),
                    **review_patch,
                })
                if done.get("auto_accept"):
                    try:
                        self.accept_card(card_id)
                    except Exception:
                        # Best effort: the user can still accept manually.
                        pass
            else:
                # Loop step 2b: rejected -> back to running; the fixer
                # session (RESUMED worker) receives the rejection reason (or
                # the loop is exhausted and the card lands in 'done' for
                # manual accept).
                self._loop_reject(card_id, card, reason or "",
                                  source="review", review_record=review_record,
                                  session_patch=review_patch)

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
        # The card may have reached a terminal status (done/failed/stopped):
        # start any drafts that were waiting on it and are now unblocked.
        try:
            self._drain_dependents()
        except Exception:
            pass  # never let the dependency drain break the loop

    # ----------------------------------------------------------------- stop

    @staticmethod
    def _signal_session(pid: int, sig: int) -> None:
        """Signal a session process: its process group when it was started
        detached (start_new_session => pid == pgid), else the pid itself — a
        re-adopted process after a server restart may not be a group leader."""
        try:
            os.killpg(pid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(pid, sig)
        except OSError:
            pass

    def stop(self, card_id: str) -> None:
        with self._lock:
            entry = self._registry.get(card_id)
        if entry is None or entry["proc"].poll() is not None:
            raise NotRunningError(f"card {card_id} has no live session")
        entry["stop_requested"] = True
        proc = entry["proc"]
        # start_new_session=True makes proc.pid the process-group id for
        # sessions we spawned ourselves; an adopted session (registered by
        # pid after a restart) is signalled by pid when it leads no group.
        self._signal_session(proc.pid, signal.SIGTERM)

        def _escalate() -> None:
            time.sleep(STOP_GRACE_SECONDS)
            if proc.poll() is None:
                self._signal_session(proc.pid, signal.SIGKILL)

        threading.Thread(target=_escalate, daemon=True).start()
        # _finalize records status "stopped" once the process exits.

    # ---------------------------------------------------------------- status

    def delete_card(self, card_id: str) -> None:
        """Delete a card: remove its linked git worktree (if any), then the
        card file and its sessions dir. Raises CardNotFound (404) / CardRunning
        (409) like the store. The card's branch (if still unmerged) is kept —
        recover unmerged work manually."""
        card = self.store.get(card_id)
        self.store.delete(card_id)
        git_info = (card.get("session") or {}).get("git") or {}
        if git_info.get("worktree"):
            try:
                gitops.remove_card_worktree(git_info)
            except GitError:
                pass  # best effort; "git worktree prune" cleans up later
        shutil.rmtree(self.sessions_dir / card_id, ignore_errors=True)
        self.questions.drop(card_id)

    def is_running(self, card_id: str) -> bool:
        with self._lock:
            entry = self._registry.get(card_id)
        return entry is not None and entry["proc"].poll() is None

    # ------------------------------------------------------- live token usage

    def live_usage(self, card_id: str) -> dict | None:
        """Best-effort live token usage of the card's current vibe session.

        Returns {"tokens": int, "cumulative": bool, "breakdown": dict | None}
        or None. `cumulative` is True when the round RESUMED the worker session
        (`vibe --resume`), so the live counter already includes earlier rounds;
        False for a fresh session (run-1, every review, resolvers), where it
        must be added to the previously recorded `session.tokens`. `breakdown`
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

    def annotate_card(self, card: dict) -> dict:
        """Return the card for API responses, with live token usage attached.

        While a session is live, `session.live_tokens` is the per-card token
        total shown on the card and summed into /api/stats: the live counter
        itself for resumed (cumulative) rounds, previous recorded tokens plus
        the fresh round's live counter otherwise. `session.live_token_breakdown`
        carries the same combination for the per-type split (cached/output/
        input) shown on hover. The store's card is never mutated — the session
        dict is copied.
        """
        live = self.live_usage(card["id"])
        if live is None:
            return card
        session = card.get("session") or {}
        recorded = session.get("tokens") or 0
        if live["cumulative"]:
            value = max(live["tokens"], recorded)
        else:
            value = recorded + live["tokens"]
        new_session = {**session, "live_tokens": value}
        breakdown = merge_token_breakdown(session.get("token_breakdown"),
                                          live.get("breakdown"), live["cumulative"])
        if breakdown is not None:
            new_session["live_token_breakdown"] = breakdown
        out = dict(card)
        out["session"] = new_session
        return out

    def recover_stale(self) -> None:
        """Startup: reconcile cards after a dashboard restart/reload (kill -9
        included) — no stuck cards, no orphaned worktrees.

        - A live card (running/reviewing) whose session process is still
          alive is RE-ADOPTED: the in-memory registry is gone after a reload,
          so we register a pid shim and a finalize thread — otherwise the
          card would be orphaned (dead pid, stuck 'running') when the process
          eventually exits. stop() works for adopted sessions by pid. Only
          cards with a dead pid are failed. Pending questions are cancelled in
          both cases (their long-poll waiter died with the old server).
        - A `done` card still flagged git.resolving (resolver died with the
          server): a live resolver pid is re-adopted; a dead one whose merge
          already concluded (MERGE_HEAD gone, tree clean) finishes the accept
          automatically; an open conflict (MERGE_HEAD set) is left for a
          resolver.
        - Orphaned per-card worktrees/branches of NON-live cards are pruned:
          the linked worktree is removed (``git worktree remove --force`` +
          prune) and the card branch is deleted only when already merged
          (safe ``git branch -d``) — unmerged work is never force-deleted.
          ACCEPTED cards are skipped: their branch and worktree are what the
          diff view reads, and they go away only on delete/archive.
        """
        for card in self.store.list():
            status = card["status"]
            session = card.get("session") or {}
            try:
                if status in LIVE_STATUSES:
                    pid = session.get("pid")
                    if pid and self._pid_alive(pid):
                        self._adopt_session(card, pid)
                        self._cancel_questions(card["id"])
                        continue
                    git_info = session.get("git") or {}
                    if git_info.get("base_branch"):
                        # Best effort: leave the main worktree on its base branch
                        # (runs from before the worktree-per-card refactor checked
                        # the card branch out there; now the main worktree never
                        # moves and the card branch lives in its linked worktree).
                        try:
                            gitops.run_git(["checkout", "-q", git_info["base_branch"]],
                                           Path(git_info["repo"]), check=False)
                        except OSError:
                            pass
                    self.store.set_status(card["id"], "failed", session_patch={
                        "finished_at": now_iso(),
                        "exit_code": None,
                    })
                    self._cancel_questions(card["id"])
                    continue
                if status == "done" and (session.get("git") or {}).get("resolving"):
                    self._recover_resolving(card)
            except Exception:
                # One broken card (e.g. its repo folder was deleted while the
                # server was down) must never crash startup recovery.
                pass
        # Prune orphaned per-card worktrees/branches of cards that are no
        # longer live (nothing manages them after a restart). Cards still
        # resolving keep their worktree/branch — a resolver owns them.
        for card in self.store.list():
            if card["status"] in LIVE_STATUSES:
                continue
            session = card.get("session") or {}
            git_info = session.get("git") or {}
            if not git_info.get("worktree") or not git_info.get("repo"):
                continue
            if card["status"] == "done" and git_info.get("resolving"):
                continue
            if card["status"] == "accepted" or git_info.get("merged"):
                # An accepted card keeps its branch and worktree: they are
                # what the diff view reads. They go away on delete/archive.
                continue
            try:
                gitops.remove_card_worktree(git_info)
                branch = git_info.get("branch")
                root = Path(git_info["repo"])
                if branch and gitops.branch_exists(root, branch):
                    # Safe delete only: fails (and keeps the branch) when the
                    # card's work is not merged yet.
                    gitops.run_git(["branch", "-q", "-d", branch], root,
                                   check=False)
            except Exception:
                pass  # best effort: one broken card/repo must not break startup
        # A merge conflict may have been resolved while the dashboard was
        # down: start any runs that were queued behind it.
        try:
            self._drain_queued_runs()
        except Exception:
            pass  # never let the queue drain break startup recovery
        # Dependencies completed while the dashboard was down: start the
        # drafts that were waiting on them.
        try:
            self._drain_dependents()
        except Exception:
            pass  # never let the dependency drain break startup recovery

    def log_tail(self, card_id: str, n: int = 200) -> str:
        card = self.store.get(card_id)
        session = card.get("session") or {}
        log_path = session.get("log")
        if not log_path:
            return ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                last = deque((line.rstrip("\r\n") for line in f), maxlen=n)
        except OSError:
            return ""
        return "\n".join(last)
