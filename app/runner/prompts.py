"""Prompt and rule templates for the spawned sessions, verdict parsing, and the
small shared helpers that grew next to them (workdir resolution, token-type
breakdowns). Moved out of the runner core (`app/runner/__init__.py`) in the module split; `app/runner/__init__.py`
re-exports every name so existing imports keep working.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from app import dupes
from app.config import BASE_DIR
from app.tokens import TOKEN_BREAKDOWN_KEYS


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
# the spec is too big for one session and, if so, split it into cards. Every
# tool name is built from `mcp_name` (VIBE_HARNESS_MCP_SERVER_NAME) so renaming
# the server leaves no stale tool names in the prompt.
def mcp_rules(mcp_name: str) -> str:
    return (
        f"- You are connected to this harness dashboard through the `{mcp_name}` MCP "
        f"server (tool group `mcp_{mcp_name}`, tools like `{mcp_name}_list_cards`, "
        f"`{mcp_name}_create_card`, `{mcp_name}_run_card`, ...). Use it to talk to the board.\n"
        "- FIRST judge whether this spec is too big for one session. If it is, split "
        f"it: create one card per subtask with `{mcp_name}_create_card` (new cards start "
        "as drafts and inherit this card's project; set auto_run=true to run one "
        f"immediately, or leave it in draft and run it later with `{mcp_name}_run_card`). "
        "The MCP has no way to create, delete, or switch projects.\n"
        "- To chain sub-cards so one runs after another completes (done/accepted), "
        f"link them with `{mcp_name}_add_dependency(card_id=<dependent>, "
        f"depends_on=<this card>)` — the dependent starts automatically when this "
        f"card completes; see `{mcp_name}_list_dependencies`.\n"
        f"- To archive finished work, use the archive tools: `{mcp_name}_create_archive` "
        f"(a typed folder under archives/), `{mcp_name}_archive_card` / "
        f"`{mcp_name}_archive_cards` (move accepted cards — or a whole deck of them — "
        f"into an archive; they leave the board), `{mcp_name}_list_archives` / "
        f"`{mcp_name}_get_archive` (see what is archived), and `{mcp_name}_restore_card` "
        "(bring a card back to the board).\n"
        "- When the spec is genuinely ambiguous and a wrong guess would be costly, "
        f"ask the user with `{mcp_name}_ask_user(question=..., options=[...])`: the "
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
                           duplication_root: Path | None = None,
                           *, mailbox: bool = True) -> str:
    """Fresh-reviewer prompt, scoped small on purpose: the
    spec + the diff (since the last reviewed commit, or the whole card diff
    when review_diff is off) + the harness-run test
    output + the mechanical duplication report. When `mailbox` is set, the
    reviewer may also question the persistent worker through the
    harness-mailbox MCP tools and must submit a structured verdict via
    `submit_verdict` (VERDICT: text is only a fallback).

    `mailbox` must be set only for sessions that actually have the mailbox
    (backend vibe — the server is attached via the worktree's .vibe/config.toml,
    which only Vibe reads); on other backends the VERDICT: log line is the only
    verdict channel.

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
    )
    if mailbox:
        parts.append(
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
    else:
        # No mailbox on this session (only Vibe reads the worktree's
        # .vibe/config.toml): the VERDICT: line in the log is the verdict.
        parts.append(
            "When you are done, end your final answer with exactly one line, "
            "either:\n"
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


# Cap on the diff embedded in a review prompt (keeps the fresh reviewer's
# context small — the point of diff-scoped review).
REVIEW_DIFF_MAX_CHARS = 60_000


# Cap on the test-suite output embedded in a review prompt.
REVIEW_TEST_OUTPUT_MAX_CHARS = 4_000


# Cap on the clones listed in a review prompt: the report is a signal, not a
# second workspace to read.
REVIEW_DUPLICATION_MAX_CLONES = 25


_VERDICT_RE = re.compile(r"VERDICT:\s*(APPROVED|REJECTED)\b[:\-]?\s*(.*)")

