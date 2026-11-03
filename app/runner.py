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

from app import activity, dupes, failures, gitops, liveusage, mailbox, praccept
from app.sleepguard import SleepGuard
from app.backends import Backend, get_backend
from app.config import BASE_DIR, SESSIONS_DIR, Config
from app.gitops import GitBusyError, GitError, GitMergeConflict
from app.tokens import (
    TOKEN_BREAKDOWN_KEYS,
    card_token_breakdown,
    card_tokens,
    combine_live,
)
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
from app.transcripts import DB_FILENAME, HARNESS_DIR, TranscriptStore, project_store
from app.questions import QuestionStore
# Imported for real, not under `if TYPE_CHECKING:`: the module uses
# `from __future__ import annotations`, so `settings: SettingsStore | None` is a
# string that `typing.get_type_hints()` resolves against this module's globals —
# a TYPE_CHECKING-only import would leave it raising NameError.
from app.settings import SettingsStore

# Grace period between SIGTERM and SIGKILL when stopping a session.
STOP_GRACE_SECONDS = 10

# Minimum seconds between live-token journal scans per live card. Matches the
# dashboard's 2s poll, so the UI updates live without extra scan traffic.
LIVE_USAGE_SCAN_INTERVAL = 2.0

# The harness MCP server (app/mcp_server.py), injected into spawned vibe
# sessions via the VIBE_MCP_SERVERS env var so the model can talk to the board.
MCP_SERVER_PATH = Path(__file__).resolve().parent / "mcp_server.py"


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


# Rule block telling spawned models to reuse what is already there. Prevention
# is cheaper than a rejection round: the reviewer rejects a diff that copies a
# block or adds code nothing calls, so the worker is told up front.
def reuse_rules() -> str:
    return (
        "- Before adding a helper, search for an existing one (`grep -rn`). "
        "Reuse or extend it. Do not copy a block — extract it. Do not add code "
        "nothing calls.\n"
    )


# Rule block telling the worker to read the steering mailbox. The check is
# NON-BLOCKING (`timeout_seconds=0`): a silent user must never stall a run, and
# with no message it returns {"none": true} immediately.
def mailbox_rules() -> str:
    return (
        "- The `harness-mailbox` MCP server is attached to this session. Between "
        "major steps — before starting a new file, before running the test "
        "suite, and before you finish — call "
        "`wait_for_message(timeout_seconds=0)`. If the user sent guidance, "
        "follow it and reply with `post_message(to=\"user\", text=...)`.\n"
    )


# Cap on the diff embedded in a review prompt (keeps the fresh reviewer's
# context small — the point of diff-scoped review).
REVIEW_DIFF_MAX_CHARS = 60_000

# Cap on the test-suite output embedded in a review prompt.
REVIEW_TEST_OUTPUT_MAX_CHARS = 4_000

# Cap on the clones listed in a review prompt: the report is a signal, not a
# second workspace to read.
REVIEW_DUPLICATION_MAX_CLONES = 25


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


def worker_rules(mcp_name: str | None, tdd: bool, mailbox: bool = True) -> str:
    """The rule block both worker prompts (first run and fixer) carry."""
    rules = (
        "- If an `agent.md` file exists in the workspace, read it and follow its "
        "rules (it may ask you to keep `route.md` up to date).\n"
    )
    if tdd:
        rules += tdd_rules()
    rules += reuse_rules()
    if mailbox:
        rules += mailbox_rules()
    if mcp_name:
        rules += mcp_rules(mcp_name)
    return rules


def prompt_template(body: str, mcp_name: str | None = "harness",
                    *, tdd: bool = True, mailbox: bool = True) -> str:
    return (
        "You are a Mistral Vibe agent. A user wrote the following spec as a "
        "post-it card. Implement it completely in the current working directory.\n\n"
        "Rules:\n"
        + worker_rules(mcp_name, tdd, mailbox) +
        "- If something is ambiguous, make a reasonable choice and record it in "
        "a NOTES.md file in the workspace.\n"
        "- Implement, then verify (run tests/build if applicable).\n"
        "- Work autonomously; do not ask questions.\n\n"
        "# Spec\n\n" + body
    )


def fix_prompt_template(body: str, reason: str, mcp_name: str | None = "harness",
                        resumed: bool = False, *, tdd: bool = True,
                        mailbox: bool = True) -> str:
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
        + worker_rules(mcp_name, tdd, mailbox) +
        "- If something is ambiguous, make a reasonable choice and record it in "
        "a NOTES.md file in the workspace.\n"
        "- If the rejection reason contains test output, fix the code so the "
        "test command passes.\n"
        "- Implement, then verify (run tests/build if applicable).\n"
        "- Work autonomously; do not ask questions.\n\n"
        "# Spec\n\n" + body
    )


def duplication_section(clones: list[dict], *, whole_app: bool) -> str:
    """The "Duplication and redundancy" block of the review prompt.

    The report is mechanical (`app/dupes.py`), so the reviewer does not have to
    guess whether a block already exists — and the criteria turn the report
    into a verdict instead of a statistic.
    """
    scope = ("the whole app (no diff was available for this run)" if whole_app
             else "the files this diff touches")
    shown = clones[:REVIEW_DUPLICATION_MAX_CLONES]
    if shown:
        report = "\n".join(
            f"- {clone['length']} lines: "
            + ", ".join(f"{occ['file']}:{occ['start']}"
                        for occ in clone["occurrences"])
            for clone in shown)
        if len(clones) > len(shown):
            report += (f"\n- ... and {len(clones) - len(shown)} more (see "
                       "`GET /api/duplication`)")
    else:
        report = "- no duplicated blocks were found in " + scope + "."
    return (
        "## Duplication and redundancy\n\n"
        "A mechanical clone detector ran over the workspace: blocks of at least "
        "6 normalized lines (whitespace stripped, blank and comment-only lines "
        "dropped) that appear in two or more places. This is the report for "
        + scope + ":\n\n"
        + report + "\n\n"
        "Judge the diff against it:\n"
        "- Does the diff re-implement something that already exists in the "
        "workspace? Name the existing thing.\n"
        "- Does it copy a block instead of extracting or sharing it?\n"
        "- Does it add code nothing calls, a config flag nothing reads, an "
        "abstraction with exactly one caller, or a file that duplicates "
        "another?\n"
        "- Is the change proportionate to the spec? A spec that needs 50 lines "
        "and a diff of 500 is a rejection.\n\n"
        "REJECT when the diff increases the clone count without a stated "
        "justification, and your rejection reason must name the duplicate.\n\n"
    )


def review_prompt_template(body: str, diff: str | None = None,
                           diff_range: str | None = None,
                           test_output: str | None = None,
                           duplication: list[dict] | None = None,
                           duplication_root: Path | None = None) -> str:
    """Fresh-reviewer prompt, scoped small on purpose: the
    spec + the diff since the last reviewed commit + the harness-run test
    output + the mechanical duplication report. The reviewer may ask the
    persistent worker questions through the harness-mailbox MCP tools and must
    submit a structured verdict via `submit_verdict` (VERDICT: text is only a
    fallback).

    `duplication` is the precomputed clone report (tests pass it directly);
    when it is None the report is computed here — for the files the diff
    touches, or for the whole app when there is no diff — from
    `duplication_root` (the card's worktree; the harness repo by default).
    """
    if duplication is None:
        root = Path(duplication_root) if duplication_root is not None else BASE_DIR
        files = dupes.diff_files(diff) if diff is not None else None
        duplication = dupes.report(root, files=files)
    parts = [
        "You are the automated reviewer in a code-production pipeline. A worker "
        "implemented the spec below in the current working directory.\n\n"
        "You are a FRESH reviewer with deliberately small context: review ONLY "
        "what is provided below (spec + diff + test output + duplication "
        "report), plus targeted reads of the workspace when the diff is "
        "unclear. Do NOT re-read the whole workspace.\n\n"
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
    parts.append(duplication_section(duplication, whole_app=diff is None))
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
                                               mailbox=self.config.mailbox),
                        extra_patch=extra_patch or None,
                        workdir=workdir)
        return self.store.get(card_id)

    def _mcp_name(self) -> str | None:
        """MCP server alias for prompts/env, or None when the MCP is disabled."""
        return self.config.mcp_server_name if self.config.mcp_enabled else None

    def _role_model(self, kind: str) -> str | None:
        """The model chosen for one session kind, or None to inherit.

        The backend decides how it is passed: Claude takes `--model`, Vibe has
        no such flag and reads VIBE_ACTIVE_MODEL from the environment.
        """
        return self.models.model_for_session(kind) if self.models else None

    def _model_env(self, kind: str) -> dict:
        """VIBE_ACTIVE_MODEL for one session kind, or {} when the role inherits.

        Vibe's config layers put VIBE_* env vars above the user's config.toml,
        so this pins the model for this one spawned session — the global config
        is never rewritten, and two sessions cannot race each other's model.
        """
        model = self._role_model(kind)
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
                     "stop_requested": False, "adopted": True,
                     "model": self._role_model(kind)}
            self._registry[card_id] = entry
        target = self._finalize_resolver if kind == "resolve" else self._finalize
        threading.Thread(target=target, args=(card_id,), daemon=True).start()

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

    def accept_mode(self) -> str:
        """How accepting a card ships the change: `local` (merge) or `pr`.

        The sidebar choice (settings.json) wins over the config default
        (VIBE_HARNESS_ACCEPT_MODE), so the board can switch without a restart.
        """
        if self.settings is not None:
            return self.settings.accept_mode(self.config.accept_mode)
        return self.config.accept_mode

    def accept_card(self, card_id: str) -> dict:
        """Accept a done card: gate first, then ship it.

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
        card = self.store.check_accept(card_id)
        git_info = (card.get("session") or {}).get("git")
        if self.accept_mode() == "pr":
            result = self._accept_with_pr(card, git_info)
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
                result = self._accept_card_locked(card, git_info)
            finally:
                # A resolver owns the merge now: the repo stays locked until it
                # concludes (_conclude_resolved_merge) or fails.
                if repo is not None and not self._resolver_owns_merge(card_id):
                    self._release_accept_repo(repo)
        if result.get("status") == "accepted":
            # Shipped: the card is no longer waiting for a merge slot.
            result = self._drop_from_accept_queue(card_id) or result
        return result

    def _accept_card_locked(self, card: dict, git_info: dict | None) -> dict:
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
                    self.accept_card(card_id)
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

    def _accept_with_pr(self, card: dict, git_info: dict | None) -> dict:
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
        accepted = self.store.accept(card_id)
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
        backend = self.backend()
        log_path = rdir / "session.log"
        log_file = open(log_path, "ab")
        try:
            # kind="resolve": the resolver is its own role, so it gets its own
            # model (and, for Vibe, the same non-review flags a run gets).
            cmd = self._build_cmd(prompt_path, ws, rdir, kind="resolve",
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
                                      "model": self._role_model("resolve"),
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
        export = self._read_export(rdir)
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
        # A refused model is why the merge is still conflicted: name it rather
        # than reporting a bare exit code.
        model_error = self._model_error(rdir, entry.get("model"), exit_code)
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
            self.store.accept(card_id)
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
        """The command line for one session. kind: 'run', 'review' or 'resolve'.

        `resume` (a session id from a previous round's export) resumes that
        session instead of starting a new one — used for fixer rounds so the
        persistent worker keeps its cached context. Reviewers are always fresh
        (no resume). The flags themselves belong to the backend, including how
        the role's model is passed (`--model` for Claude, nothing for Vibe).
        """
        backend = backend or self.backend()
        return backend.build_cmd(config=self.config, prompt_path=prompt_path,
                                 ws=ws, rdir=rdir, kind=kind, resume=resume,
                                 mcp_config=mcp_config,
                                 model=self._role_model(kind))

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
        # One live session per card. A second spawn would overwrite the
        # registry entry: the first process would go unmanaged and its finalize
        # thread could never release the slot (the cap would silently shrink).
        # A sequential handover (run -> review -> fixer) is fine — the previous
        # process has already exited by then.
        with self._lock:
            existing = self._registry.get(card_id)
        if existing is not None and existing["proc"].poll() is None:
            raise CardRunning(f"card {card_id} already has a live session")
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
                                      # The model this round asked for: the
                                      # export only reports the model that
                                      # actually ran, which is exactly what a
                                      # rejected choice never produces.
                                      "model": self._role_model(kind),
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

    def _model_error(self, rdir: Path, model: str | None,
                     exit_code: int | None) -> str | None:
        """The backend's reason for refusing `model`, or None.

        A rejected model exits non-zero like any other crash, so the cause has
        to be read back out of the round's log: the round then fails naming the
        model instead of looking like a random crash — or, worse, like a
        success on the backend's own default model.
        """
        if not model or not exit_code:
            return None
        try:
            text = (rdir / "session.log").read_text(encoding="utf-8",
                                                    errors="replace")
        except OSError:
            return None
        return self.backend().model_error(text, model)

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

    def _green_gate(self, card: dict) -> str | None:
        """Ship-only-green gate at accept: the test command must pass in the
        workspace right before the card is accepted/merged. Raises
        TestFailedError (-> 409) on failure; records verified=True on pass and
        returns the passing output (the PR body quotes it), or None when no
        test command is configured.
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
                                                   mailbox=self.config.mailbox),
                        extra_patch=extra, resume=resume)

    # -------------------------------------------------------------- finalize

    def _finalize(self, card_id: str) -> None:
        """Runs in a daemon thread: wait for exit, record the outcome, and
        drive the review loop.

        The session slot is released in a `finally`: a failure while recording
        the outcome (e.g. the card moved on and the store refuses the status
        write) must never leave a stale registry entry — that would shrink the
        concurrency cap for the rest of the process.
        """
        with self._lock:
            entry = self._registry.get(card_id)
        if entry is None:
            return
        try:
            self._record_outcome(card_id, entry)
        finally:
            self._release_slot(card_id, entry)

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

    def _record_outcome(self, card_id: str, entry: dict) -> None:
        """Record one finished session and drive the next pipeline step."""
        proc = entry["proc"]
        kind = entry["kind"]

        exit_code = proc.wait()
        card = self.store.get(card_id)
        sdir = self.sessions_dir / card_id
        rdir = sdir / f"{kind}-{entry['round']}"
        session = card.get("session") or {}
        ws = Path(session.get("workdir") or (sdir / "workspace"))
        # The last live total shown for the card, persisted as a floor so the
        # display does not dip between rounds (the registry entry is gone until
        # the next round's export lands).
        high_water = self._live_high_water(card)
        high_water_patch = ({"live_high_water": high_water}
                            if high_water is not None else {})

        export = self._read_export(rdir)
        self._capture_transcript(card_id, f"{kind}-{entry['round']}", rdir)
        usage = export.get("usage") or export.get("token_usage") or {}
        tokens = usage.get("total_tokens", usage.get("total"))
        # Per-type breakdown (cached/output/input) for the token-counter hover
        # tooltip; None when the export carries only a total.
        token_breakdown = export_token_breakdown(usage) if isinstance(usage, dict) else None
        export_config = export.get("config") or export.get("effective_config") or {}
        model = export_config.get("active_model") or export_config.get("model")
        stop_reason = export.get("stop_reason")
        # A model the backend refused: the round failed for that reason, so say
        # so instead of reporting a generic crash (or a silent fallback).
        model_error = self._model_error(rdir, entry.get("model"), exit_code)
        if model_error:
            stop_reason = "model_error"
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
                **high_water_patch,
            }
            if token_breakdown is not None:
                outcome["token_breakdown"] = token_breakdown
            if model_error:
                outcome["model_error"] = model_error
            if worker_session_id:
                outcome["worker_session_id"] = worker_session_id
            if entry.get("timed_out"):
                # The harness watchdog killed it (a backend without a native
                # time limit): a timeout, not the user pressing Stop.
                outcome["timed_out"] = True
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
                    else:
                        # The growth signal: how much this card added and
                        # removed against its base. A 900-line card for a small
                        # spec is then visible on the board, not only in review.
                        counts = praccept.diff_stat_counts(git_info)
                        if counts is not None:
                            outcome["diff_stat"] = counts
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
                                                              test_tail,
                                                              duplication_root=ws),
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
            if verdict is None:
                # No verdict channel answered: recorded explicitly so the
                # failure classifier can tell "the reviewer said no" from "the
                # reviewer said nothing" without parsing the reason text.
                review_record["no_verdict"] = True
            if model_error:
                # The reviewer never ran: its model was refused. Recorded so
                # the round does not read as a reviewer that simply said no.
                review_record["model_error"] = model_error
            if token_breakdown is not None:
                review_record["token_breakdown"] = token_breakdown
            # Supervision bookkeeping: which commit this review covered (the
            # next review diffs from it) and how many mailbox messages were
            # exchanged so far.
            review_patch: dict = {**high_water_patch}
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
                stopped_patch = {
                    "finished_at": now_iso(),
                    "exit_code": exit_code,
                    **high_water_patch,
                }
                if entry.get("timed_out"):
                    stopped_patch["timed_out"] = True
                self.store.set_status(card_id, "stopped", session_patch=stopped_patch)
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
        # _finalize records status "stopped" once the process exits and drains
        # the queue there; drain here too so a stop that races an exit already
        # observed still releases the slot for the next queued card.
        try:
            self._drain_queued_runs()
        except Exception:
            pass  # never let the queue drain break a stop

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

    # ---------------------------------------------------------- live activity

    def activity(self, card_id: str, cursor: int = 0,
                 limit: int = activity.DEFAULT_LIMIT) -> dict:
        """The card's live activity feed: what the model is doing right now.

        `cursor` is the number of journal lines the client already consumed
        (0 = the last `limit` events). The journal is the vibe session's, found
        the same way live token usage finds it: the live registry entry's pid
        maps to the session, and a card with no live session falls back to its
        last worker session. A card with neither, or a journal that is gone,
        is an empty feed — never an error (see app/activity.py).
        """
        card = self.store.get(card_id)      # CardNotFound -> 404
        session_id = self._activity_session_id(card)
        if not session_id:
            return {"cursor": 0, "events": []}
        journal = (liveusage.vibe_home() / "logs" / "session" / "unified"
                   / session_id / "journal")
        return activity.read_activity(journal, cursor=cursor, limit=limit)

    def _activity_session_id(self, card: dict) -> str | None:
        """The vibe session whose journal carries this card's activity.

        The live session wins (its journal is being appended right now); a
        card with no live registry entry falls back to the worker session
        recorded when the last round ended.
        """
        with self._lock:
            entry = self._registry.get(card["id"])
        if entry is not None:
            session_id = entry.get("vibe_session_id")
            proc = entry.get("proc")
            if not session_id and proc is not None:
                try:
                    session_id = liveusage.find_session_id_by_pid(
                        liveusage.vibe_home(), proc.pid)
                except Exception:
                    session_id = None
                if session_id:
                    entry["vibe_session_id"] = session_id
            if session_id:
                return session_id
        return (card.get("session") or {}).get("worker_session_id")

    # ------------------------------------------------- steering (user -> worker)

    def messages(self, card_id: str) -> list[dict]:
        """The card's mailbox conversation, oldest first (both directions).

        The popup shows the user <-> worker exchange; reviewer <-> worker
        traffic is internal and filtered out client-side.
        """
        self.store.get(card_id)      # CardNotFound -> 404
        return mailbox.list_messages(mailbox.mailbox_dir(self.sessions_dir, card_id))

    def post_message(self, card_id: str, text: str) -> dict:
        """Append the user's guidance to the card's mailbox, addressed to the
        worker. The worker's next `wait_for_message(timeout_seconds=0)` returns
        it. Empty text is a ValueError (the API maps it to 400)."""
        self.store.get(card_id)      # CardNotFound -> 404
        return mailbox.post_message(
            mailbox.mailbox_dir(self.sessions_dir, card_id), "user", "worker", text)

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

        `session.card_tokens` is what the card actually cost: the worker's
        export total plus every review and resolver round (`app/tokens.py`).
        The chip, /api/stats and /api/split-report all read this one number, so
        they cannot disagree.

        While a session is live, `session.live_tokens` is the per-card total
        shown on the card and summed into /api/stats: the live counter itself
        for resumed (cumulative) rounds, the card total plus the fresh round's
        live counter otherwise. `session.live_token_breakdown` carries the same
        combination for the per-type split (cached/output/input) shown on
        hover. The store's card is never mutated — the session dict is copied.
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
            out["session"] = new_session
        live = self.live_usage(card["id"])
        if live is None:
            return out
        session = out.get("session") or {}
        recorded = session.get("card_tokens") or 0
        value = combine_live(recorded, live)
        new_session = {**session, "live_tokens": value}
        breakdown = merge_token_breakdown(card_token_breakdown(session),
                                          live.get("breakdown"), live["cumulative"])
        if breakdown is not None:
            new_session["live_token_breakdown"] = breakdown
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
          diff view reads, and they go away only on delete/archive. That
          includes a card accepted as a pull request: its branch and worktree
          are the working copy for follow-up commits on the PR, and the ``-u``
          push sets the branch's upstream to ``origin/<branch>`` — which makes
          ``git branch -d`` consider it merged and succeed, so the safe-delete
          guard alone would not save it.
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
        # resolving keep their worktree/branch — a resolver owns them — and so
        # does a card accepted as a pull request: the PR may need follow-up
        # commits, and its `-u` push makes `git branch -d` succeed (the branch
        # looks "merged" to its upstream), so pruning would delete it.
        for card in self.store.list():
            if card["status"] in LIVE_STATUSES:
                continue
            session = card.get("session") or {}
            git_info = session.get("git") or {}
            if not git_info.get("worktree") or not git_info.get("repo"):
                continue
            if card["status"] == "done" and git_info.get("resolving"):
                continue
            if (card["status"] == "accepted" or git_info.get("merged")
                    or session.get("pr_url") or session.get("pr_opened_at")):
                # An accepted card keeps its branch and worktree: they are
                # what the diff view reads (and, for a PR accept, the working
                # copy for follow-up commits). They go away on delete/archive.
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
        # An "Accept all" batch was interrupted by the restart: the cards are
        # still flagged accept_queued, so pick the batch back up.
        try:
            if any((c.get("session") or {}).get("accept_queued")
                   for c in self.store.list()):
                self._start_accept_drain()
        except Exception:
            pass  # never let the accept drain break startup recovery

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
