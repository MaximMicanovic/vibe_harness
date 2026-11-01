# Vibe Harness

A local kanban board for AI coding agents. Write a spec on a card, press **Run**,
and the harness spawns a headless [Mistral Vibe](https://mistral.ai) CLI session
that implements it on its own git branch in its own worktree — with live status,
logs and token usage back on the board. Every implementation is then reviewed by
a second, read-only Vibe session before it can be accepted and merged.

It is a **Vibe-only** tool: it shells out to the `vibe` CLI in headless mode and
reads Vibe's `export.json` for token usage and run metadata. No other agent CLIs
are supported.

**What you get**

- **Cards, not chat.** Each spec is a card on a board with six columns
  (Draft → Running → Reviewing → Done → Accepted / Failed). Runs survive a
  server restart; a card's whole history stays on the card.
- **A review gate you can trust.** A fresh, read-only reviewer session inspects
  the diff and runs the tests itself. Nothing is accepted without an approval,
  and nothing is merged with red tests.
- **Real git, per card.** Each card runs on `harness/<card-id>` in a linked
  worktree, so several cards in one repo run concurrently. Accept merges;
  a merge conflict spawns a resolver session instead of failing the card.
- **Models talk to the board.** Run sessions get an MCP server with ~30 tools:
  create and run sub-cards, link dependencies, stack cards into decks, archive
  finished work, and ask *you* a question on the card when the spec is ambiguous.
- **Cost is visible.** Tokens, cost and per-round `export.json` for every
  session, plus per-role model choice (worker / reviewer / merge resolver).

## Contents

- [Quickstart](#quickstart)
- [Requirements](#requirements)
- [How a run works](#how-a-run-works)
- [Git workflow](#git-workflow)
- [Configuration](#configuration)
- [The harness MCP (models talking to the board)](#the-harness-mcp-models-talking-to-the-board)
- [Projects](#projects)
- [Layout](#layout)
- [Tests](#tests)

## Quickstart

```bash
uv venv
uv pip install -e ".[dev]"
uvicorn app.main:app --reload --reload-dir app --port 8734
```

Then open http://127.0.0.1:8734, create a card, and press **Run with Vibe**.
Always pass `--reload-dir app`: card worktrees live under `sessions/`, and
watching those makes every agent edit restart the server — startup recovery
then re-adopts live cards whose vibe process is still alive (instead of
failing them). The top-bar **⟳ Restart** button
(`POST /api/server/restart`) does a hard restart when needed.

## Requirements

- **Python 3.12+** and [uv](https://docs.astral.sh/uv/) (or plain `pip`).
- The **`vibe` CLI** on `PATH` — `vibe --version` should work.
- A **Vibe API key** configured for the CLI (`vibe --setup`), needed for real runs.

The dashboard itself needs no key: it only shells out to `vibe`.

## How a run works

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

## Configuration

All optional, via environment variables:

| Env var | Default | Purpose |
|---|---|---|
| `VIBE_HARNESS_HOST` | `127.0.0.1` | Bind host |
| `VIBE_HARNESS_PORT` | `8734` | Bind port |
| `VIBE_HARNESS_TIME_LIMIT` | `1800` | Per-session time limit (s), passed to `vibe --time-limit` |
| `VIBE_HARNESS_MAX_PRICE` | unset | Per-session price cap (USD), passed to `vibe --max-price` |
| `VIBE_HARNESS_AGENT` | unset | Vibe agent (builtin or `~/.vibe/agents/NAME.toml`) |
| `VIBE_HARNESS_APPROVE` | `auto` | `auto` → `--auto-approve`, `smart` → `--smart-approve` |
| `VIBE_HARNESS_ENABLED_TOOLS` | unset | Comma-separated tool allowlist → `--enabled-tools` |
| `VIBE_HARNESS_TEST_CMD` | unset | Test command run in the workspace after each successful run and once more at accept; a failure rejects the round into the fixer loop / blocks accept with 409 |
| `VIBE_HARNESS_TEST_TIMEOUT` | `600` | Timeout (s) for the test command, separate from vibe's `--time-limit` |
| `VIBE_HARNESS_ACCEPT_CMD` | unset | Deprecated alias for `VIBE_HARNESS_TEST_CMD` |
| `VIBE_HARNESS_REVIEW_AGENT` | `ask` | Read-only builtin agent for the review pass |
| `VIBE_HARNESS_MAX_REVIEW_ROUNDS` | `5` | Review/fix loop cap per card; when hit, the card is done-but-unapproved and must be accepted manually |
| `VIBE_HARNESS_GIT_BRANCH_PREFIX` | `harness` | Per-card git branch prefix (`<prefix>/<card-id>`) for git-backed runs |
| `VIBE_HARNESS_VIBE_BIN` | `vibe` | Path/name of the Vibe CLI |
| `VIBE_HARNESS_ALLOWED_WORKDIRS` | unset (allow all) | Comma-separated absolute path prefixes; a card's work folder must be inside one of them, else runs return 403 |
| `VIBE_HARNESS_MCP_ENABLED` | `1` | Inject the harness MCP server into spawned run sessions (models then get the `mcp_harness` tool group); `0` disables |
| `VIBE_HARNESS_MCP_SERVER_NAME` | `harness` | MCP server alias — tools are published as `<name>_<tool>` (e.g. `harness_create_card`) |
| `VIBE_HARNESS_ASK_USER_TIMEOUT` | `600` | Default seconds `harness_ask_user` waits for your answer on the card (per question; the tool caps it at 3600) |
| `VIBE_HARNESS_TDD` | `1` | Worker prompts (run + fixer rounds) instruct the model to work test-driven: failing test first (red), smallest fix (green), refactor while green, full suite green before finishing; `0` omits the rule |

> **Reducing token usage:** see [`route.md`](route.md) — how work is routed
> through run → test gate → review → accept, which knobs cap spend
> (`--max-price`, `--time-limit`, tool allowlist, loop cap), how to route
> sessions to cheaper Vibe agents, and a copy-paste low-token preset.

## The harness MCP (models talking to the board)

Every **run** session the harness spawns gets a `harness` MCP server injected
(via vibe's `VIBE_MCP_SERVERS` env var, so no global config is touched; review
and resolver sessions stay MCP-free). Inside a run, the model sees an
`mcp_harness` tool group with `harness_*` tools backed by
`app/mcp_server.py` (a stdlib-only stdio JSON-RPC server that forwards to the
dashboard's HTTP API, so the dashboard stays the single writer):

- `harness_current_card` — which card this session implements (id, project, …)
- `harness_list_cards` / `harness_get_card` / `harness_create_card` /
  `harness_update_card` / `harness_delete_card`
- `harness_run_card` / `harness_stop_card` / `harness_accept_card` / `harness_card_log`
- `harness_add_dependency` / `harness_remove_dependency` / `harness_list_dependencies` —
  link cards so one runs automatically after another completes (done/accepted):
  `add_dependency(card_id=<dependent>, depends_on=<dependency>)`. The board draws
  a red string between linked cards; the same link can be made by dragging a draft
  card onto another card.
- `harness_create_archive` / `harness_list_archives` / `harness_get_archive` /
  `harness_archive_card` / `harness_archive_cards` / `harness_archive_stack` /
  `harness_restore_card` — shelve finished work into typed archive folders
  under `archives/`. `archive_stack` retires a whole deck in one call and
  records it in the archive's manifest, so the board's decks are the
  structure the archive keeps.
- `harness_list_stacks` / `harness_create_stack` / `harness_update_stack` /
  `harness_delete_stack` / `harness_stack_cards` / `harness_unstack_card` —
  the board's decks (named piles of cards in one column).
- `harness_list_templates` / `harness_stats` / `harness_list_projects` (read-only)
- `harness_ask_user` — **ask you a question on the card**: the model
  calls it when the spec is ambiguous and a wrong guess would be costly. The
  question appears on the card (with optional multiple-choice buttons and a
  text input) and the tool call **blocks** until you answer on the card,
  dismiss it, or the timeout hits; the result carries your answer (or
  `timeout`/`cancelled`, in which case the model falls back to a reasonable
  choice recorded in `NOTES.md`). Default wait 600 s
  (`VIBE_HARNESS_ASK_USER_TIMEOUT`, max 3600 s per call). Questions are stored
  per card in `sessions/<id>/questions.json`, shown as a Q→A history on the
  card, and cancelled automatically when the session ends. Cards returned by
  the API carry their `questions` list.

That is the same functionality the dashboard offers — **except project
management**: there is deliberately no tool to create, delete, or switch
projects, and `create_card`/`update_card` reject `project`/`workdir`
arguments. Cards a model creates inherit the current card's project/workdir.

The run/fix prompts tell the model to **first judge whether the spec is too
big for one session**; if so it splits it with `harness_create_card` — new
cards start as drafts, and the model can run them immediately
(`auto_run=true`) or later via `harness_run_card`. When a tool allowlist is
configured (`VIBE_HARNESS_ENABLED_TOOLS`), the harness automatically appends
the `harness_*` glob so the MCP stays available. Disable the whole MCP with
`VIBE_HARNESS_MCP_ENABLED=0`.

## Projects

The dashboard is **project-centric**. Register a project with the **＋ Project**
button (name + folder — on macOS, **Browse…** opens the native folder picker and
fills in the path; the folder is created if missing), then pick it in
the **Project** dropdown in the top bar. The board and the stats bar then show
only that project's cards, and the top-bar selection is remembered per browser,
so a reload lands back on the project you were working in. The **last used
project** is likewise remembered per browser — updated when you save a card
with a project (or without one) and when you pick a concrete project in the
top bar — and when you press **+ New card**, the editor's **Project**
dropdown lets you pick the project and automatically defaults to it
(the top-bar selection is the fallback when nothing was used yet).

When you run a card, Vibe works in the project's folder (runs are auto-approved,
so it edits that project directly). A card's **Work folder override** field
redirects a single card elsewhere; a card with no project runs in an isolated
sandbox (`sessions/<id>/workspace`).

Registering a project also **sets up its folder**: `git init` (with an initial
commit, or an immediate commit of the new files if it's already a repo), a
managed **`agent.md`** with rules for agents working there — including "always
keep `route.md` up to date when relevant" — and an **empty `route.md`** that
agents fill in. Every server start re-syncs the managed `agent.md` copies
(remove its marker comment to keep your own version); `route.md` is agent-owned
and never touched by the harness once it exists. Session prompts tell each Vibe
agent to read and follow `agent.md`.

Safety: optionally restrict which folders runs may use:

```bash
export VIBE_HARNESS_ALLOWED_WORKDIRS=/Users/you/programming,/Users/you/work
```

(comma-separated absolute path prefixes; empty = allow all; violations return 403).

## Layout

```
app/
  config.py    # paths, defaults, env overrides
  cards.py     # CardStore: CRUD over cards/*.md (YAML frontmatter)
  runner.py    # SessionRunner: spawn/stop/track headless vibe sessions
  mcp_server.py# stdio MCP server: lets spawned models talk to the board (harness_* tools)
  main.py      # FastAPI app: API routes + static file serving
  templates/   # starter card templates
  static/      # dashboard UI (vanilla HTML/CSS/JS)
cards/         # post-it cards (markdown + frontmatter)
sessions/      # per-card dirs: workspace/ (or the project folder) + per-round
               #   run-N/ and review-N/ (prompt.md, session.log, export/)
tests/         # pytest suite
```

## Tests

```bash
.venv/bin/python -m pytest -v
```

The suite is self-contained: it uses a fake `vibe` binary and temp directories,
so it never spends tokens and never touches your real board. A few checks boot
the served `app.js` under a small DOM stub in Node (`tests/*.js`, `tests/*.cjs`)
to cover the board's stacking and archive-tree behaviour.

## Data on disk

Everything the board owns lives beside the code and is **gitignored** — the
repository holds the harness, not your work:

| Path | What it is |
|---|---|
| `cards/` | the cards (markdown + YAML frontmatter) |
| `sessions/` | per-card run/review rounds: `prompt.md`, `session.log`, `export/`, worktrees |
| `archives/` | archive folders (`archives/<name>-<id>/`) |
| `projects.json` | the registered projects |
| `stacks.json` | the board's decks |
| `models.json` | the per-role model choice |

## License

No license has been chosen yet, so all rights are reserved. If you want this to
be reusable, add a `LICENSE` file (MIT and Apache-2.0 are the usual picks for a
project like this).
