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

## Session transcripts (compressed)

Each round's vibe session directory (`export/session/`: journal, chunks, tool
results) is the only record of what the model actually did, so it is kept — but
compressed, not as loose files. When a round finalizes (`_record_outcome`, and
`_record_resolver_outcome` for a merge resolver), the harness tars and gzips
`export/session/` into the store of the card's **project** (SQLite, one row per
`(card, round)`) and then removes the directory. `export.json` stays where it
is: it is what the harness reads for tokens, model and stop reason, so the
token accounting is unchanged.

- **One store per project.** A card's transcripts live in
  `<project>/.vibe-harness/transcripts.db`, so they belong to the project they
  came from and leave with it. A card with no project (a sandbox run) — or one
  whose project no longer exists — uses the harness store,
  `<harness root>/.vibe-harness/transcripts.db`. `VIBE_HARNESS_TRANSCRIPTS_DB`
  still wins: one explicit store for everything.
- **Never dirties the project.** Creating a project's store adds
  `.vibe-harness/` to that repo's `.git/info/exclude` — local-only, so the
  tracked `.gitignore` is never modified and the user's repo gains no committed
  change. A project folder that is not a git repository is fine (nothing to
  exclude).
- **A failed save never fails the round.** The capture is best effort: on any
  error it logs a warning and leaves the directory in place, so nothing is
  lost.
- **Backfill** imports the directories written before the store existed:
  `POST /api/transcripts/backfill` walks `sessions/*/*/export/session/`, looks
  up each card's project, stores the round in THAT project's store and removes
  the directory. It is idempotent — a round already in the store is skipped,
  never duplicated.
- **Migration.** The old harness-root `transcripts.db` (where every project's
  rounds used to land) is drained on first use: each row moves to the store of
  its card's project, a row whose card has no project stays in the harness
  store, and the old file is removed once it is empty. Idempotent — a second
  run finds nothing to move.
- **Reading them back:** `GET /api/transcripts/stats` (one row per store —
  project + harness — plus the totals, raw/gz bytes and compression ratio),
  `GET /api/cards/<id>/transcripts` (metadata rows),
  `GET /api/cards/<id>/transcripts/<round>` (the tar.gz as a download) and
  `GET /api/cards/<id>/transcripts/<round>/events` (the journal parsed into the
  same events the live activity feed shows). The MCP exposes the last two as
  `harness_list_transcripts` and `harness_get_transcript` — a model reads the
  parsed text, never the blob.
- **Never published:** `.vibe-harness/` is in each repo's `.git/info/exclude`
  (and `transcripts.db*` is in the harness root's `.gitignore`), like
  `sessions/`.

## Duplication and redundancy

Correctness is not the only thing a review checks. Every card adds lines, so
the pipeline also pushes back on *growth*:

- **The worker is told up front.** Both worker prompts (run-1 and every fixer
  round) carry the reuse rule: *before adding a helper, search for an existing
  one (`grep -rn`); reuse or extend it; do not copy a block — extract it; do not
  add code nothing calls.* Prevention is cheaper than a rejection round.
- **The reviewer gets a mechanical report.** `app/dupes.py` is a pure,
  dependency-free clone detector: it normalizes every source line (strip the
  whitespace, drop blank and comment-only lines) and reports runs of at least
  six consecutive identical normalized lines that appear in two or more places,
  as `{"length": N, "occurrences": [{"file": ..., "start": ...}]}`. It covers
  `.py`, `.js`, `.css` and `.html`, skipping `tests/`, `sessions/`, `archives/`,
  `.venv` and `node_modules`. The review prompt carries the report for the files
  the diff touches (the whole app when there is no diff) plus four criteria:
  does the diff re-implement something that already exists (name it)? does it
  copy a block instead of extracting or sharing it? does it add code nothing
  calls, a config flag nothing reads, an abstraction with exactly one caller, or
  a file that duplicates another? is the change proportionate to the spec (a
  spec that needs 50 lines and a diff of 500 is a rejection)? The reviewer must
  **reject** when the diff increases the clone count without a stated
  justification, and the rejection reason must name the duplicate.
- **The board shows the number.** `GET /api/duplication` returns
  `{"count": N, "clones": [...]}` for the project, and every finalized run
  records `session.diff_stat` — `{files, added, removed}` from
  `git diff --stat <base>...<branch>` — which the card renders as a `+412 −38`
  chip (file count on hover). A card that adds 900 lines for a small spec is
  visible at a glance instead of only in review. Sandbox runs (no git) record
  nothing and show no chip.

## What a card cost

A card's total is what it actually cost, not just what its worker reported:

```
card_total = worker export total
           + every review round
           + every resolver round
```

- **Worker** — `session.tokens`, the worker's `export.json` total. It is
  already cumulative across resumed fixer rounds (`vibe --resume`), so it is
  added once, never per round.
- **Review rounds** — `session.reviews[]` keeps every round's record
  (`{approved, reason, round, tokens, model, log, export_dir, via, commit}`);
  cards written before the list existed fall back to `session.review`.
- **Resolver rounds** — a merge-conflict resolver writes its own
  `resolve-N/export/export.json`; `_record_resolver_outcome` reads it and
  stores `tokens` (and `token_breakdown`) on `session.resolver`.

`app/tokens.py::card_tokens` is the one definition: the per-card chip,
`/api/stats` and `/api/split-report` all read it, so the board total is exactly
the sum of the chips. `session.tokens` keeps its old meaning (the worker's
export total), so cards written before this existed still report a sensible
total.

**The live counter only climbs.** While a session runs, the card shows
`card_total + live` (a fresh round) or `max(live, card_total)` (a resumed,
cumulative round). When a round ends its live counter disappears for a moment;
the last value shown is persisted as `session.live_high_water` and floors the
total until the next round's export lands, so the number never dips at a round
boundary (review → fixer included).

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

## Why a card failed

Logs are raw text: "tests failed" vs "the reviewer rejected it" vs "it timed
out" vs "it crashed" is something you would otherwise work out by hand. The
harness names it instead.

`app/failures.py::classify(card)` is a **pure function** over the session
record the runner already keeps (`exit_code`, `stop_reason`, `verified`,
`test_output`, `git_error`, `review.approved` / `review.reason` /
`review.source` / `review.exhausted`, `review_rounds`, `resolver`, `git.*`).
It returns `{kind, detail}` — `detail` being one human sentence — or `None`
for a card that did not fail. No store, no I/O, no clock: the same card always
classifies the same way.

Kinds, in priority order (the first match wins):

| kind | when |
|---|---|
| `stopped_by_user` | the card is `stopped` and the harness watchdog did not kill it |
| `git_conflict` | `git.merge_conflict` / `git.resolving` / a failed resolver |
| `timeout` | `session.timed_out`, or a limit `stop_reason` (`limit`, `max_turns`, …) |
| `tests_failed` | `verified` is false, the last rejection came from the test gate, or the post-merge gate failed |
| `reviewer_rejected` | the loop ran out (`review.exhausted`) with an actual rejection |
| `no_verdict` | the loop ran out and the reviewer never answered |
| `crashed` | a non-zero exit (or a failed commit) with no other explanation |

`annotate_card` attaches the result as `session.failure`, so it rides along on
`GET /api/cards`, `GET /api/cards/<id>` and therefore on the MCP's `list_cards`
/ `get_card`. The board shows the kind as a badge on the card (the detail is
the tooltip) and the top bar's **Failures** filter narrows the board to one
kind — the choice is remembered in the browser like the project filter.

Two fields exist only for this: `session.timed_out` (the harness killed a
session that outlived `VIBE_HARNESS_TIME_LIMIT` — a backend without a native
time limit, i.e. Claude) and `review.no_verdict` (no verdict channel answered),
so a timeout is not mistaken for a Stop and a silent reviewer is not mistaken
for a rejection.

## Git workflow

Runs in a real work folder (a project's folder or a card's work folder override —
not the isolated sandbox) are **git-backed**: one branch and one linked worktree
per card, shipped on accept — merged locally, or opened as a pull request.

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
- **Accept ships the change**, in one of two modes (`accept_mode`, the
  sidebar's **Accept mode** control or `VIBE_HARNESS_ACCEPT_MODE`):
  - **`local`** (the default) **merges with `--no-ff`**: the card branch goes
    into the base branch in the project folder as exactly **one merge commit**,
    whose second parent is the card branch. That commit is what "this card"
    means — the diff view shows `merge_commit^..merge_commit`, and a future
    revert has one commit to revert. A fast-forwarded card would have no such
    commit. `auto_accept` merges the same way. The result is recorded on the
    card's session git record: `merge_commit` (sha), `merge_was_ff` (true only
    when the branch was already contained in the base, so no commit was
    created) and `merged_at` (ISO).
  - **`pr`** never merges: it pushes the card branch (`git push -u origin
    harness/<id>`) and opens a pull request (`gh pr create --head
    harness/<id>`) whose body carries the audit trail — the card spec, the
    review verdict (approved + reason + round + model), the test output tail
    and the diff stat. The card records `session.pr_url`, `pr_number` and
    `pr_opened_at`, and the branch **and its worktree are kept** because the PR
    may need follow-up commits. The accept button reads **Open PR**, and an
    accepted card links to its PR. A preflight runs first and fails with a 409
    naming the missing piece — `gh` not installed, `gh auth status` not green,
    no `origin` remote, or the card branch gone — changing nothing. The body is
    built by `app/praccept.py::build_pr_body`, a pure function unit-tested
    without git or network.
- **One accept per repository at a time.** Every accept merges in the repo's
  main worktree (the project folder), and git allows exactly one merge in
  progress there. `SessionRunner` keeps a per-repository accept lock
  (`_accepting_repos`): a second accept for the same repo does not merge — the
  card stays in **Done** flagged `session.accept_queued` with a `queue_reason`
  ("waiting for the merge in <repo> to finish") and the accept drain merges it
  automatically when the repo frees, so no second click is needed. Accepts for
  *different* repos still run concurrently. The repo stays locked while a
  conflict resolver owns the merge, and is released when the resolver
  concludes or fails.
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
  the card's `merge_commit`. That conclusion is guarded: the commit is only
  recorded when the card branch is an ancestor of HEAD
  (`git merge-base --is-ancestor`), so a merge that belongs to another card
  leaves this card done with a `merge_error` instead of accepting it with
  someone else's commit. If the resolver fails, the card stays done with
  the conflict open and **leaves the accept queue** — it is not retried in a
  loop (that would spawn resolver after resolver); clicking Accept again
  spawns a fresh resolver (clicking it while one is already running returns
  409). A card that was queued behind a busy repo and is then accepted by its
  resolver is cleared of `accept_queued` / `queue_reason` like any other
  accept.
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
  skipped**: their branch and worktree are what the diff view reads. That
  includes a card accepted as a pull request — its branch and worktree are
  kept across restarts (the PR may need follow-up commits), which the safe
  `-d` guard alone would not achieve: the `-u` push points the branch at
  `origin/<branch>`, so git considers it merged and `-d` would succeed.
  Pending questions whose waiter died with the server are cancelled (failed,
  adopted and resolver-adopted cards alike).
- Cards show a `⎇ harness/<id> → <base>` chip while the work is unmerged, and
  `✓ merged into <base>` after a local accept — or a `↗ PR #<n>` link after a
  PR accept.

Cards without a project or work folder run in the isolated sandbox
(`sessions/<id>/workspace`) and do not use git — there is no repo to branch
from or merge into.

## Undoing an accepted card (revert)

Accept merges; a revert un-merges. `POST /api/cards/<id>/revert` (and the
`revert_card` MCP tool) undoes an accepted card and puts it back on the board
as a **draft**, so the loop closes without a hand-run `git revert`:

- **It reverts the card's merge commit** in the project folder (the repo root,
  where the base branch is checked out): `git revert --no-edit -m 1 <sha>` for
  the `--no-ff` merge commit an accept records, `git revert --no-edit <sha>`
  for a plain commit. The new commit is recorded on the session as
  `revert_commit`, with `reverted_at` (ISO) next to it.
- **It needs a recorded merge commit.** A card accepted before merge commits
  were recorded has none, so the revert refuses with a clear 409 instead of
  guessing which commit to undo.
- **It refuses a dirty tree**, and refuses while a merge or a revert is already
  in progress in that repo (409) — those belong to a resolver or to you.
- **A conflict aborts.** Unlike an accept-time merge conflict (left in place
  for a resolver session), a conflicting revert runs `git revert --abort` and
  returns 409 with the conflicting paths: the repository is left exactly as it
  was, and no resolver is spawned.
- **The card is reopened, not reset.** `accepted -> draft` is a real
  transition; `accepted` is cleared but the **session history is kept**
  (tokens, logs, reviews, git record), so the cost record of the accepted run
  survives. The card shows a `↩ reverted` chip and the editor a note naming the
  revert commit.
- **An archived card comes back first.** If the card sits in an archive, the
  revert restores it to the board (the existing restore path) and then reverts
  it, so the reopened draft is visible.
- The card's own branch and linked worktree are left alone (they are what the
  diff view reads); a re-run resets the branch from the base as usual.

## Seeing what a card changed (the diff view)

A reviewer session approves a change you never look at; the diff view is how you
inspect it. Hold **D** and click a card (or use the **Diff** button in the card
panel) to open it. The diff is a floating popup, not a full-screen modal: about
720x640 (floor 420x320), opened next to the clicked card and clamped to the
viewport, draggable by its header and resizable by the grip in its bottom-right
corner, closed with **Esc** or the ×. The board stays visible and usable
behind it, and only one popup is open at a time — opening the diff closes the
live activity popup (**C** + click) and vice versa. Like the activity popup it
remembers where you put it (see below).

The header carries the card title, the range selector, the layout toggle
(Unified / Side by side) and the palette toggle, plus the verdict that judged
that range (approved/rejected, the reviewer's reason, the round, the model and
the token count). The body holds the changed files with their +/- counts
(click one to scroll to it) and the diff itself.

The diff is **parsed**, not just coloured: files -> hunks -> rows, each row
carrying its kind (`ctx` / `add` / `del` / `meta` / `hunk`), its old and new
line numbers, and its text. That is what makes the rest possible:

- **Line numbers** for both sides in a fixed-width, dimmed gutter, counted
  from each hunk header so they stay right across hunks.
- **Unified mode** (the default on a narrow panel): one column, `+`/`-`
  markers, old and new line numbers.
- **Side-by-side mode** (the default when the panel is wide enough,
  toggleable): old on the left, new on the right, aligned row by row within
  each hunk, with a 2px rule between the columns.
- **Word-level highlighting**: each run of `-` rows is paired with the
  following `+` run and a small LCS over tokens (refined to characters inside
  each changed block) marks the changed span with a stronger tint than the
  rest of the line. A one-character change shows the character.
- **Syntax highlighting**: each line is tokenized by the language of its file
  — from the path in the `diff --git` header: Python, JavaScript, CSS, HTML,
  Markdown, JSON, and plain for anything else — into keywords, strings,
  comments, numbers, functions, operators, punctuation, tags, attributes and
  headings. The tokenizer is hand-written (no dependency) and carries
  multi-line state, so a Python docstring or a JS block comment is coloured on
  every line it spans. The token colour is the text colour; the row tint and
  the word-diff tint stay behind it, so both signals survive. A 4000-line diff
  still renders in well under a second.
- **Collapsed context**: a run of more than 6 unchanged lines becomes a
  "⋯ N unchanged lines" row; clicking it expands the rows in place.
- **File headers**: path and `+N -M`, collapsible, sticky while that file
  scrolls.
- **Restrained palette**: the diff hue is on the gutter rule and a faint row
  fill; the code is token-coloured on top of it. The palette toggle switches
  to a colourblind-safe blue/orange palette and is persisted in `localStorage`
  (`vibe.diff.palette`). Every token colour keeps 4.5:1 contrast against the
  base, the add/del fill and the word-diff tint in both palettes.

Every piece of model-written text is still inserted as a **text node** — the
diff never goes through `innerHTML`. The `truncated` flag still shows its
notice.

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

## Watching a card work (hold C and click it)

The session **log** is the CLI's final output: for a headless run it stays
almost empty until the session ends, so it cannot answer "what is it doing
right now". The vibe **journal** can — it is appended *while* the session runs.

Hold **C** and left-click a card to open a small floating panel (~420x520, floor
320x240) next to it. It is not an overlay: the board stays usable behind it, and
only one popup is open at a time. The header shows the card title, its status
pill and the live token count; the body is the activity feed, newest at the
bottom, auto-scrolled unless you scrolled up (then a **↓ jump to latest** button
appears). The model's thinking is collapsed behind a **thinking…** toggle. Esc
or the **×** closes it. A plain click still opens the editor.

Both popups are the same kind of panel, so both behave the same way: drag the
header to move one, drag the grip in its bottom-right corner to resize it (the
activity popup keeps a strip of padding under its compose row so the grip never
covers **Send**). The layout is remembered per panel in `localStorage` under
`vibe-harness:panel:activity` / `vibe-harness:panel:diff` as
`{width, height, left, top}` — written when a drag ends and, debounced ~250 ms,
when a resize settles (a native resize fires no event of its own, so a
`ResizeObserver` watches the box). The next open, and a reload, put the panel
back where you left it, still clamped to the viewport. Opening a panel is not a
layout choice: a panel you never moved or resized keeps opening next to the card
you clicked. A corrupt, missing or unreadable stored value (private mode throws
on `localStorage`) falls back to the defaults, and clearing `localStorage`
returns both panels to them.

`GET /api/cards/<id>/activity?cursor=<int>` returns
`{cursor, events: [{kind, at, …}]}` with `kind` one of:

| kind | source in the journal | fields |
|---|---|---|
| `text` | `payload.delta[].entry.content[].text` (an assistant `message`) | `text` |
| `reasoning` | `payload.delta[].entry.text` (id `reasoning-…`) | `text` |
| `tool` | `payload.request` with `kind: "tool"` | `tool`, `summary` |
| `result` | `payload.result` (`tool_succeeded` / `tool_failed`) | `ok`, `summary` |
| `status` | `payload.delta[].entry.detail.display.statusText` | `text` |

`cursor` is the number of journal lines already consumed; the client passes it
back and gets only what was appended since. A first call (no cursor) returns
the last 200 events, so opening the popup on a finished card shows its tail
instead of replaying everything. Each `text`/`summary` is capped at 2000
characters and one call returns at most 200 events.

The journal is found the same way live token usage finds it
(`app/liveusage.py`): the card's live registry entry pid maps to the session
via `logs/session/active/<id>.lock.json`. A card with no live session falls
back to its last worker session's journal; a card with neither, or a journal
that is gone, is an empty feed — never an error. The popup polls once a second
while the card is running/reviewing and stops when it leaves those states.

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

## Two people editing the same card

`PUT /api/cards/{id}` is optimistic-concurrency guarded, so a second save cannot
silently erase the first person's edit:

- `CardUpdate` takes an optional `expected_updated_at`. When it is present and
  does not match the card's current `updated_at`, the update is refused with
  **409** and a detail saying *"this card changed since you opened it"* — nothing
  is written.
- When it is absent the update is unconditional (last write wins), exactly as
  before. The MCP tools (`update_card`) and scripts send no
  `expected_updated_at`, so they keep working unchanged.
- The editor (`app/static/app.js::openEditor`) remembers the `updated_at` it
  loaded (`state.editorUpdatedAt`) and sends it on save. On a 409 it keeps the
  user's text in the fields and shows a conflict box with two ways out:
  **Reload theirs** (re-fetch the card and replace the fields with the version
  on the board) and **Overwrite anyway** (re-send without
  `expected_updated_at`, which wins). The editor stays open until one is chosen;
  **Run with Vibe** takes the same path, so a lost race never runs the other
  person's spec.

Editing a card nobody else touched is unaffected: the version the editor loaded
is still the current one, so the save goes through.

## Finding a card: search

The top bar has a search box. It answers one question — *where is that card?* —
across the board and every archive, and it is deliberately two mechanisms:

- **The board is filtered in the browser.** `GET /api/cards` already returns
  every card *including its body*, so `state.cards` is the whole board. Typing
  filters it on title, body and id, case-insensitive, as you type — no request,
  no backend. Matching cards stay in their columns, the rest are hidden, and a
  `N of M cards` hint next to the box says how much is hidden. The project
  filter and the search compose: both apply.
- **The archives are searched on the server.** Archives load per folder
  (`GET /api/archives/{id}`), so the browser does not hold them. `GET
  /api/search?q=<text>` scans the board's cards and every archived card
  linearly (`app/search.py` — no index, no dependencies) and answers

  ```json
  [{"id": "…", "title": "…", "status": "accepted",
    "where": "Vibe Harness / Old work",
    "snippet": {"text": "…the matching line…", "start": 12, "end": 18}}]
  ```

  `where` is `"board"` or the archive's location (`<project> / <name>`, or just
  the name when the archive is unfiled). `snippet` is the matching body line
  trimmed to ~120 chars around the match, with the match's offsets so the UI
  highlights it without searching again; a card that matched only on its title
  or id gets its title as the snippet. Matching is case-insensitive over title,
  body and id. Unreadable card files are skipped, never fatal.

  The list is capped at 50 results. The response stays a plain list, so the cap
  is reported in headers: `X-Search-Truncated: true|false` and
  `X-Search-Total: <uncapped count>`. The results panel says "Showing the first
  50 of N matches" when it was cut short.

The results list under the input groups hits by location — **Board** first,
then each archive — and every row shows the card's title, status and the
highlighted snippet. Clicking a board hit opens that card's panel; clicking an
archived hit opens the archive browser on the folder holding it and highlights
the card. A query that matches nothing says so explicitly rather than showing a
blank panel.

Keyboard: `/` focuses the box, `Esc` clears it. `Esc` clears the search *before*
it closes an overlay — a live query means "show me the board again", not "close
whatever happens to be open".

Search costs no LLM tokens: it is a linear scan over markdown files plus a
client-side filter, and it never spawns a session.
