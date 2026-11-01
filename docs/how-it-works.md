# How a run works

The pipeline a card goes through: implement, test gate, review, accept.

1. **Run** spawns a headless `vibe` session (`--prompt-file`) in the card's work
   folder (project folder, card override, or the isolated sandbox). The card moves
   to **Running**. The worker prompt tells the model to follow **test-driven
   development** — write a failing test first (red), make it pass with the
   smallest change (green), refactor only while green, and leave the full test
   suite green before finishing (disable with `VIBE_HARNESS_TDD=0`).
2. When the implementation exits 0, the **test gate** runs `VIBE_HARNESS_TEST_CMD`
   in the workspace (if configured; timeout `VIBE_HARNESS_TEST_TIMEOUT`, default
   600 s). A failing test command is an automatic rejection — the card goes back
   to **Running** and a fixer session receives the test output as the reason.
   The card then moves to **Reviewing**: a second, read-only `vibe --agent ask`
   session inspects the workspace, runs the test suite itself, and answers
   `VERDICT: APPROVED` or `VERDICT: REJECTED: <reason>`.
3. **Approved** → the card is **Done** and can be accepted. **Rejected** → the card
   goes back to **Running** and a fixer session receives the rejection reason.
   The loop (test gate + review) repeats until approved, capped at
   `VIBE_HARNESS_MAX_REVIEW_ROUNDS` (default 5).
4. **Cap hit** → the card lands in **Done** *without* an approved review
   (`review.exhausted`) and must be accepted manually. The same gate applies to
   any done card without an approval: the **Accept** button only appears after an
   approved review (or after exhaustion, as a manual accept).
5. **Accept is ship-only-green:** pressing **Accept** runs the test command once
   more in the workspace right before merging; while it fails, accept returns 409
   and the card stays in **Done**. Only a green run is merged/accepted.
6. With the card's **auto-accept** checkbox set (it remembers your last choice),
   an approved card is accepted automatically — but never without an approval,
   and never with red tests.

Prompts, logs, and `export.json` for every round are kept under
`sessions/<id>/run-N/` and `sessions/<id>/review-N/`.

## Git workflow

Runs in a real work folder (a project's folder or a card's work folder override —
not the isolated sandbox) are **git-backed**:

- If the folder is not a git repository yet, it is `git init`-ed for you (with a
  minimal `.gitignore` — committed in the initial commit so test artifacts never
  dirty the tree — an initial commit, and a repo-local identity when none is
  configured). Repositories that already exist are never modified.
- The card runs on its own branch, `harness/<card-id>`, created from the branch
  that was checked out when the run started (the *base branch*). The branch is
  checked out in a per-card **linked git worktree** at `sessions/<card-id>/worktree`
  (`git worktree add … -B <branch> <base>`), and vibe works there — so the
  project folder itself stays on your base branch while the card runs, and
  **several cards in the same repository can run concurrently**, each on its
  own branch in its own worktree.
- Each successful implementation round is committed on the card branch (in the
  card's worktree).
- **Accept merges** the card branch into the base branch in the project folder
  (fast-forward, with a real merge as fallback), then removes the card's
  linked worktree and deletes the branch, leaving the folder on the base
  branch. `auto_accept` merges the same way.
- **Merge conflict → a resolver model spins up.** The conflict does not fail the
  accept: the conflicted merge is left in place in the project folder, the card
  stays in **Done** (the card shows a spinner and a "⚠ merge conflict —
  resolving…" chip), and a vibe session is spawned to fix the conflict there
  (it concludes the merge with a commit). When it succeeds, the card is
  accepted and merged automatically. If the
  resolver fails, the card stays done with the conflict open — clicking Accept
  again spawns a fresh resolver (clicking it while one is already running
  returns 409).
- A dirty tree never blocks a run: leftover uncommitted changes (e.g. from an
  interrupted previous run) are stashed automatically — on a first run or a
  re-run — and kept in `git stash list` for manual recovery. The card's git
  record flags this with `stashed: true` and, when it happened on a first run,
  `auto_stashed: true`. (Merging on accept still requires a clean tree.)
- Deleting a card removes its linked worktree too (its unmerged branch is
  kept for manual recovery).
- **Restart / crash recovery** (startup, incl. after `kill -9`): fault-tolerant
  per card — one broken card (e.g. deleted repo) is skipped and never crashes
  startup. Live cards whose vibe process is still alive are **re-adopted** —
  they finalize when the process exits and Stop works by pid — only dead-pid
  cards are marked failed. A Done card stuck in "resolving…" whose merge
  already landed is finished automatically (merged + accepted); an open
  conflict is left for a resolver. Orphaned worktrees of non-live cards are
  pruned, and their `harness/<id>` branch is deleted only when already merged
  (safe `-d` — unmerged work is never force-deleted). Pending questions whose
  waiter died with the server are cancelled (failed, adopted and
  resolver-adopted cards alike).
- Cards show a `⎇ harness/<id> → <base>` chip while the work is unmerged, and
  `✓ merged into <base>` after accept.

Cards without a project or work folder run in the isolated sandbox
(`sessions/<id>/workspace`) and do not use git — there is no repo to branch
from or merge into.

## Git workflow

Runs in a real work folder are git-backed: one branch and one linked
worktree per card, merged on accept.

Runs in a real work folder (a project's folder or a card's work folder override —
not the isolated sandbox) are **git-backed**:

- If the folder is not a git repository yet, it is `git init`-ed for you (with a
  minimal `.gitignore` — committed in the initial commit so test artifacts never
  dirty the tree — an initial commit, and a repo-local identity when none is
  configured). Repositories that already exist are never modified.
- The card runs on its own branch, `harness/<card-id>`, created from the branch
  that was checked out when the run started (the *base branch*). The branch is
  checked out in a per-card **linked git worktree** at `sessions/<card-id>/worktree`
  (`git worktree add … -B <branch> <base>`), and vibe works there — so the
  project folder itself stays on your base branch while the card runs, and
  **several cards in the same repository can run concurrently**, each on its
  own branch in its own worktree.
- Each successful implementation round is committed on the card branch (in the
  card's worktree).
- **Accept merges** the card branch into the base branch in the project folder
  (fast-forward, with a real merge as fallback), then removes the card's
  linked worktree and deletes the branch, leaving the folder on the base
  branch. `auto_accept` merges the same way.
- **Merge conflict → a resolver model spins up.** The conflict does not fail the
  accept: the conflicted merge is left in place in the project folder, the card
  stays in **Done** (the card shows a spinner and a "⚠ merge conflict —
  resolving…" chip), and a vibe session is spawned to fix the conflict there
  (it concludes the merge with a commit). When it succeeds, the card is
  accepted and merged automatically. If the
  resolver fails, the card stays done with the conflict open — clicking Accept
  again spawns a fresh resolver (clicking it while one is already running
  returns 409).
- A dirty tree never blocks a run: leftover uncommitted changes (e.g. from an
  interrupted previous run) are stashed automatically — on a first run or a
  re-run — and kept in `git stash list` for manual recovery. The card's git
  record flags this with `stashed: true` and, when it happened on a first run,
  `auto_stashed: true`. (Merging on accept still requires a clean tree.)
- Deleting a card removes its linked worktree too (its unmerged branch is
  kept for manual recovery).
- **Restart / crash recovery** (startup, incl. after `kill -9`): fault-tolerant
  per card — one broken card (e.g. deleted repo) is skipped and never crashes
  startup. Live cards whose vibe process is still alive are **re-adopted** —
  they finalize when the process exits and Stop works by pid — only dead-pid
  cards are marked failed. A Done card stuck in "resolving…" whose merge
  already landed is finished automatically (merged + accepted); an open
  conflict is left for a resolver. Orphaned worktrees of non-live cards are
  pruned, and their `harness/<id>` branch is deleted only when already merged
  (safe `-d` — unmerged work is never force-deleted). Pending questions whose
  waiter died with the server are cancelled (failed, adopted and
  resolver-adopted cards alike).
- Cards show a `⎇ harness/<id> → <base>` chip while the work is unmerged, and
  `✓ merged into <base>` after accept.

Cards without a project or work folder run in the isolated sandbox
(`sessions/<id>/workspace`) and do not use git — there is no repo to branch
from or merge into.

## Backends: Vibe or Claude

The harness is a board, not an agent — it drives a headless CLI and reads back
what that CLI reports. Two are supported, chosen in the sidebar's **Models**
panel (or `PUT /api/settings {"backend": "claude"}`):

| | `vibe` (default) | `claude` |
|---|---|---|
| command | `vibe --prompt-file … --output-dir …` | `claude -p --output-format json` |
| prompt | `--prompt-file` | stdin |
| output | `export/export.json` | one JSON result object on stdout |
| time limit | its own `--time-limit` | the harness watches the clock and terminates |
| review pass | `--agent ask` (read-only) | `--tools Read,Grep,Glob,Bash` (no Edit/Write) |
| budget cap | `--max-price` | `--max-budget-usd` |
| MCP server | `VIBE_MCP_SERVERS` env | `--mcp-config` file |

A backend owns three things: the command line, where its output lands, and how
to normalize that output into the shape the rest of the harness speaks
(`usage`, `session_id`, `config.active_model`, `stop_reason`). Everything
downstream — the review loop, git, archives, the board — is backend-agnostic.

Two caveats for `claude`:

- **It runs on your Claude subscription.** `claude -p` uses the same OAuth
  login as the interactive CLI, so headless runs consume the same rolling
  5-hour window. When the window is exhausted the session fails with a
  rate-limit error, which the harness reports as a failed card. Use
  `--bare` + `ANTHROPIC_API_KEY` (per-token billing) if you want no window.
- **`total_cost_usd` is notional on a subscription.** It is the API-equivalent
  figure, not money charged, so the board's cost column overstates what you
  actually paid.

