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

## Concurrency cap

The harness runs at most `VIBE_HARNESS_MAX_CONCURRENT` sessions at once
(default 3; `0` = unlimited). The sidebar's **Settings** panel (⚙️) sets the
same cap per harness — it is stored in `settings.json`, wins over the env var,
and is read at run time, so a change applies without a restart. The panel also
shows the current occupancy ("2 of 3 running").

Pressing **Run** when every slot is taken does not fail and does not spawn
anything: the card stays in **Draft** flagged `session.queued` with
`queue_reason` "waiting for a free slot (2/3 running)" (a neutral chip, not an
error), and `session.queued_at` records when it joined the queue. As soon as a
session ends — the end of every session, after **Stop**, after a merge-conflict
resolver concludes, and at startup — `_drain_queued_runs()` starts the queued
cards **oldest queue time first**, stopping as soon as the cap is reached, so no
card starves. Drains are serialized (one at a time) and the cap check plus the
slot registration are atomic, so two sessions ending together cannot both start
the same queued card or push the board past the cap; a session's slot is always
released — even when recording its outcome fails — so the cap cannot silently
shrink. A card in the review loop holds one slot at a time (run → review →
fixer are sequential), which is what makes the cap a cap on *concurrent*
sessions, worktrees and test suites.

## Notifications: a card finishing is not silent

The board polls `/api/cards` every 2 s while a card is live, so the tab already
sees every status change. `app.js` keeps the previous status map in
`state.prevStatuses` and diffs it on each refresh: a card moving from
**Running**/**Reviewing** to **Done**/**Failed**/**Stopped** is announced.

- **Desktop notification** via the browser Notification API, naming the card and
  its outcome. Permission is requested on the **first click**, never on load.
- **Beep fallback.** If permission is denied (or the browser has no Notification
  API) a short tone is synthesised with WebAudio — there is no audio file.
- **Silent cases.** Nothing fires on the first load (there is no previous state
  to diff) and nothing fires for the card whose panel is open — you are already
  looking at it.
- **Title badge.** While notifications are unread and the tab is hidden the
  document title reads `(N) vibe-harness`; focusing the tab clears it.
- **Toggle.** The sidebar's **Settings** panel (⚙️) — the same panel as the
  concurrency cap — has a *Desktop notifications* switch. The choice is per
  browser, stored in `localStorage` under `vibe-harness:notifications` (`'0'` =
  off, anything else = on), so it survives a reload. Switching it off also stops
  the beep and the title badge.

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
- **Accept merges with `--no-ff`**: the card branch goes into the base branch in
  the project folder as exactly **one merge commit**, whose second parent is the
  card branch. That commit is what "this card" means — the diff view shows
  `merge_commit^..merge_commit`, and a future revert has one commit to revert.
  A fast-forwarded card would have no such commit. `auto_accept` merges the
  same way. The result is recorded on the card's session git record:
  `merge_commit` (sha), `merge_was_ff` (true only when the branch was already
  contained in the base, so no commit was created) and `merged_at` (ISO).
- **The card branch and its linked worktree are KEPT after accept.** They are
  what the card's diff is read from, so `GET /api/cards/<id>/diff` still works
  on an accepted card. They are removed only when the card is **deleted** or
  **archived** (`gitops.remove_card_worktree`).
- **Disk cost.** Every accepted card keeps its branch and a full checkout of the
  repository at `sessions/<card-id>/worktree`. A worktree is a real working
  directory (not a packfile), so the cost is roughly *one copy of the tracked
  files per accepted card* — a 50 MB repo with 20 accepted cards costs about
  1 GB under `sessions/`. The branches themselves are cheap (they share the
  repo's object database). Archiving or deleting a card reclaims its worktree;
  `git worktree prune` cleans up a worktree whose directory was removed by hand.
- **Merge conflict → a resolver model spins up.** The conflict does not fail the
  accept: the conflicted merge is left in place in the project folder, the card
  stays in **Done** (the card shows a spinner and a "⚠ merge conflict —
  resolving…" chip), and a vibe session is spawned to fix the conflict there
  (it concludes the merge with a commit). When it succeeds, the card is
  accepted and merged automatically, and the resolver's commit is recorded as
  the card's `merge_commit`. If the resolver fails, the card stays done with
  the conflict open — clicking Accept again spawns a fresh resolver (clicking
  it while one is already running returns 409).
- A dirty tree never blocks a run: leftover uncommitted changes (e.g. from an
  interrupted previous run) are stashed automatically — on a first run or a
  re-run — and kept in `git stash list` for manual recovery. The card's git
  record flags this with `stashed: true` and, when it happened on a first run,
  `auto_stashed: true`. (Merging on accept still requires a clean tree.)
- Deleting a card removes its linked worktree too (its branch is kept for
  manual recovery).
- **Restart / crash recovery** (startup, incl. after `kill -9`): fault-tolerant
  per card — one broken card (e.g. deleted repo) is skipped and never crashes
  startup. Live cards whose vibe process is still alive are **re-adopted** —
  they finalize when the process exits and Stop works by pid — only dead-pid
  cards are marked failed. A Done card stuck in "resolving…" whose merge
  already landed is finished automatically (merged + accepted); an open
  conflict is left for a resolver. Orphaned worktrees of non-live cards are
  pruned, and their `harness/<id>` branch is deleted only when already merged
  (safe `-d` — unmerged work is never force-deleted). **Accepted cards are
  skipped**: their branch and worktree are what the diff view reads. Pending
  questions whose waiter died with the server are cancelled (failed, adopted
  and resolver-adopted cards alike).
- Cards show a `⎇ harness/<id> → <base>` chip while the work is unmerged, and
  `✓ merged into <base>` after accept.

Cards without a project or work folder run in the isolated sandbox
(`sessions/<id>/workspace`) and do not use git — there is no repo to branch
from or merge into.

## Seeing what a card changed (the diff view)

A reviewer session approves a change you never look at; the diff view is how you
inspect it. Hover a card and press **D** (or use the **Diff** button in the card
panel) to open the panel. It shows the range selector, the verdict that judged
that range (approved/rejected, the reviewer's reason, the round, the model and
the token count) pinned next to the diff, the changed files with their +/-
counts (click one to scroll to it), and the diff itself — rendered line by line
(hunk header / addition / deletion / context), every line inserted as a text
node because the content is model-written.

`GET /api/cards/<id>/diff?range=card|round|worktree` (default `card`):

| range | what it shows |
|---|---|
| `card` | what accept merged: `merge-base(base_branch, harness/<id>)..harness/<id>` before accept, `merge_commit^..merge_commit` after — the same changes either way |
| `round` | what the last review round judged: the commits between the previous round's commit and this round's commit (round 1: from the merge-base of the card branch and its base) |
| `worktree` | uncommitted changes in the card's worktree (`git diff HEAD`) |

The response is `{range, base, head, diff, truncated, files: [{path, additions,
deletions}], review}`. The diff is capped at 400 KB and flagged `truncated`. A
sandbox (non-git) card answers `{diff: null, reason: "not git-backed"}` instead
of failing.

The `card` range, the `round` range (round 1) and the reviewer's own diff all
start at the **merge-base** of the card branch and its base branch, not at the
base branch's tip. A card branch
is cut when the run starts; if the base moves while the card is being reviewed,
`git diff base..branch` compares two tips and shows the card *deleting* whatever
landed on the base since — a false "revert" that gets the round rejected for
someone else's commit. The merge-base is also exactly what accept merges, so the
before-accept and after-accept views agree.

**Review history.** Every round's verdict is appended to `session.reviews` (a
list of `{approved, reason, round, tokens, model, log, export_dir, via,
commit}`); `session.review` stays the LAST record for backward compatibility.
That is what lets the `round` range reconstruct what each round judged — the
harness advances `last_reviewed_commit` to HEAD when a round ends, so the
literal `last_reviewed_commit..HEAD` is empty right after a review.

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

