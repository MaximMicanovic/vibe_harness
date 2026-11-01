# route.md — Routing guide: spend tokens where they matter

How vibe-harness routes work through the pipeline, and how to keep token usage
low. This is a Vibe-CLI-only tool: every session is a headless `vibe` run, and
the harness reads each run's `export.json` for token usage. There is no
per-session `--model` flag on the CLI — model routing happens through Vibe
agents (see §3).

## 0. Project folders: git init + agent.md + route.md

This file (in the harness root) is the **harness's** routing guide. Each
registered project gets its **own** `route.md`, which starts **empty** and is
maintained by the agents working in that project — the harness only creates it
if missing and never modifies an existing one.

Opening a new project (＋ Project → `POST /api/projects`) always sets up its
folder (`app/routemd.py::setup_project`):

- **`git init`** the folder (if it isn't a repo yet) with an initial commit that
  folds in `.gitignore`, `agent.md` and `route.md` — the work tree starts clean,
  so the first card run doesn't hit the dirty-tree guard. If the folder is an
  existing repo, the new files are committed immediately as their own commit.
- **`agent.md`** — a *managed* rules file for every vibe session working in the
  folder (marker comment on top). Its rules include: **always keep `route.md`
  up to date when a change is routing/token-relevant**, run the test suite
  before finishing, and record ambiguous choices in `NOTES.md`. The session
  prompts (implement / fixer / merge-resolver) explicitly tell the agent to read
  and follow `agent.md`. On every server start, managed copies are re-synced so
  rule updates propagate; remove the marker line in a project's `agent.md` to
  make it user-owned (never overwritten). The API response reports
  `agent_md: created|updated|unchanged|custom` and `route_md: created|exists`.
- **`route.md`** — created empty per project; agents fill it in per the
  agent.md rules.

Deleting a project only unregisters it — the files stay on disk. A setup error
for one project is reported in the response (`setup_error`) and never blocks
registration of the others.

## 1. The pipeline and where tokens go

```
you write a card (spec)
  → run-N      vibe session implements the spec in the work folder   ← big spend
               (run-1 spawns fresh; fixer rounds run-N>1 RESUME the same
                session via `vibe --resume <session_id>` — cached context,
                no workspace re-read)
  → test gate  VIBE_HARNESS_TEST_CMD runs in the workspace           ← free (no LLM)
  → review-N   FRESH vibe --agent ask process every round, but its   ← small spend
               prompt only carries the spec + `git diff
               <last-reviewed-commit>..HEAD` (round 1: from the
               MERGE-BASE of the card branch and its base, so a branch
               that is merely behind its base is not read as a revert)
               + the test output, and it
               can ask the persistent worker via the per-card MCP mailbox
               (`harness-mailbox`: post_message / wait_for_message /
               submit_verdict — the verdict is read from submit_verdict,
               VERDICT: text is only a fallback)
  → (reject → fixer run-N+1 (resumed) … capped at VIBE_HARNESS_MAX_REVIEW_ROUNDS)
  → accept     test gate once more, then merge the card branch       ← free
               with --no-ff (one merge commit per card); the branch
               and its worktree are KEPT so the diff stays readable
  → inspect    GET /api/cards/<id>/diff?range=card|round|worktree    ← free
               (the board's Diff panel: D over a card, or the Diff
                button) — no LLM tokens, it only reads git
  → revert     POST /api/cards/<id>/revert (or the MCP revert_card)  ← free
               `git revert` the card's merge commit in the project
               folder, then accepted -> draft with the session kept;
               a conflict aborts (409 + paths), no resolver is spawned
```

Tokens are spent almost entirely in **run** sessions; **review** sessions are
cheap (fresh read-only `ask` agent, diff-scoped prompt, no edits); the **test
gates and git steps cost no LLM tokens at all**. Resuming the worker session
across fix rounds is the main token saver: a fresh round no longer re-reads
the whole workspace (input tokens went ~200k → 2–3M per session before this).

## 2. Routing rules the harness applies automatically

- **Fail fast before review.** With `VIBE_HARNESS_TEST_CMD` set, a failing test
  suite rejects the round *before* any reviewer session is spawned — you never
  pay for a review of code that is already known to be broken. The fixer gets
  the test output as the rejection reason, so it fixes the right thing.
- **Workers are told to work test-driven.** Every worker prompt (run-1 and each
  fixer round) carries a TDD rule block: failing test first (red), smallest fix
  (green), refactor while green, full suite green before finishing. It costs a
  few dozen prompt tokens per round and pairs with the test gate above; disable
  with `VIBE_HARNESS_TDD=0`.
- **Reviewer is always the cheap read-only agent.** The review pass always runs
  with `--agent <VIBE_HARNESS_REVIEW_AGENT>` (default `ask`) — never the
  configured worker agent — and the prompt forbids modifying files, so the
  reviewer makes no edit tool calls. The reviewer also runs the test suite and
  must REJECT on failure, which keeps broken code from reaching Done.
- **Worker persists across fix rounds; reviewer is fresh but diff-scoped.**
  run-1 spawns a fresh vibe session; every rejection spawns the fixer with
  `vibe --resume <session_id> --prompt-file <round>/prompt.md` (session id from
  the previous round's `export.json`, kept in `session.worker_session_id`), so
  the worker keeps its cached context. Every review round is a NEW process, but
  its prompt only carries the spec, the test-suite output, and
  `git diff <last-reviewed-commit>..HEAD` in the card's worktree — small context,
  independent judgment, no anchoring. Disable with `VIBE_HARNESS_WORKER_RESUME=0`
  / `VIBE_HARNESS_REVIEW_DIFF=0`.
- **Per-card MCP mailbox.** Each run attaches `app/mailbox_server.py` (tiny stdio
  MCP server) to the card's worktree via `.vibe/config.toml` (git-excluded there);
  state lives in `sessions/<id>/mailbox/`. The fresh reviewer can
  `post_message`/`wait_for_message` with the persistent worker mid-review, and
  its verdict arrives as the structured `submit_verdict` call. Verdicts are
  scoped per review round: every session gets `VIBE_HARNESS_MAILBOX_ROUND=<kind>-<n>`,
  `submit_verdict` is reviewer-role-only and writes `verdicts/<round>.json`
  (mirrored to the top-level `verdict.json` as "latest"); the harness clears the
  round's file before spawning the review and reads only that file afterwards —
  a round that never calls `submit_verdict` falls back to `VERDICT:` text parsing
  (`via="log"`) instead of inheriting the previous round's verdict. The harness
  only supervises: round cap, accept/merge, token accounting, mailbox lifecycle.
  Disable with `VIBE_HARNESS_MAILBOX=0`.
- **Loop cap.** Rejections (review or test) loop through a fixer, capped at
  `VIBE_HARNESS_MAX_REVIEW_ROUNDS` (default 5). After the cap the card lands in
  Done unapproved (`review.exhausted`) and *you* decide — no more paid rounds.
- **Dependency links order the pipeline.** A card with `depends_on` (drag a
  draft card onto another card, or `harness_add_dependency`) does not start while
  any dependency is unfinished: `run` marks it waiting (`session.waiting_for`)
  instead of spawning a session, and `_drain_dependents` starts it automatically
  once every dependency reaches done/accepted (failed/stopped do not unblock).
  So a chain of linked drafts costs the same tokens as running them by hand, in
  order, with no idle time between them — but do not link cards that should run
  independently, they would serialize.
- **Hard guardrails on every session** (passed to `vibe`):
  - `VIBE_HARNESS_MAX_PRICE` → `--max-price <usd>`: absolute per-session cost
    cap; the session is interrupted when exceeded.
  - `VIBE_HARNESS_TIME_LIMIT` → `--time-limit <s>` (default 1800): wall-clock
    cap; vibe writes the export and exits.
  - `VIBE_HARNESS_ENABLED_TOOLS` → repeated `--enabled-tools`: in headless mode
    this *disables all other tools*, which removes whole tool schemas from the
    prompt and cuts tool-call tokens. Allowlist only what the task needs
    (e.g. `read,write,edit,bash`).
- **Small workspaces.** Git-backed runs happen in a per-card linked worktree
  (`sessions/<id>/worktree`, a full checkout of the card's project repo on the
  card's own branch — the project folder itself stays on its base branch), not
  in a giant monorepo root; project-less cards run in an isolated
  `sessions/<id>/workspace`. Smaller working directory + smaller allowlist =
  smaller prompts.

## 3. Model routing (Vibe agents, not CLI flags)

The `vibe` CLI has no `--model` flag; the model comes from Vibe's config
(`~/.vibe/config.toml` → `active_model`, currently
`deepseek-v4-1-flash-1017` = Mistral-hosted DeepSeek V4.1 Flash 1017 on the
`mistral` provider, with `mistral-large-4-0-3` / `zai-glm-5-3-flash` /
`glm-5-3` also registered in `models`) or from a custom agent TOML in
`~/.vibe/agents/<name>.toml`, which can pin a cheaper model. Card: a
Mistral-hosted third-party model needs a full `name` + `provider = "mistral"`
entry — an alias-only entry is dropped unless the server's routed-model
registry knows it.

**Give every hand-written model entry its real token window.** Vibe only knows
the window of models in its built-in table (`mistral-large-4`, `zai-glm-5-3`,
Claude, GPT…); for anything else it assumes 262,144 tokens and warns
`window_guessed`. The DeepSeek entry therefore carries
`max_context_length = 1048576` and `max_output_tokens = 131072` (the model
card's 1M context / 128k output).

**The window alone does not raise the cap you see.** Vibe's status bar shows
context use against the *compaction threshold*, not the window, and the default
rule caps that at `min(256_000, window - 60_000)` — so a 1M model still
displayed and compacted at 256k. The DeepSeek entry sets
`auto_compact_threshold = 800000` explicitly, matching the threshold Mistral's
own routed models ship (800k on a 1M window, ~76%). Check a new entry with
`vibe -p ok --output-dir DIR` and read `config.models.<alias>` in
`export.json`; a running TUI keeps the value it started with, so restart it
(or `/reload`) after editing.

**Name the entry after the model.** The status bar and the model picker show
`display_name`, falling back to the alias, so an entry aliased `deepseek` reads
`deepseek[max]`. The DeepSeek entry uses the full model id as its alias plus
`display_name = "DeepSeek V4.1 Flash 1017"`; `active_model` must then name that
alias.

Route sessions to agents via the harness env vars:

| Route | Env var | Default | Tip |
|---|---|---|---|
| Worker (implement/fix) | `VIBE_HARNESS_AGENT` | Vibe's `default_agent` | Use a custom agent pinned to a cheaper model for small/mechanical cards |
| Reviewer | `VIBE_HARNESS_REVIEW_AGENT` | `ask` | Keep it read-only; a small model is fine — it only inspects and answers a verdict |

Example `~/.vibe/agents/cheap.toml` pinned to a small model, then
`VIBE_HARNESS_AGENT=cheap` for routine cards and the large model for hard ones.

### Per-role models (the sidebar's Models panel)

Each kind of session can run on its own model, chosen in the board's sidebar
(🧠 Models) and stored in `models.json` at the harness root:

| Role | Session | What it does |
|---|---|---|
| Worker | `run-N` | Implements the card and its fixer rounds |
| Review | `review-N` | Reviews the diff and the test output |
| Merge conflict | `resolve-N` | Resolves a conflicted merge on accept |
| Orchestrator | — | No session yet: the worker splits a spec into sub-cards itself |

A role left on **inherit** runs on the user's own `active_model`. A chosen
model is pinned for that one spawned session with `VIBE_ACTIVE_MODEL` — Vibe's
config layers put `VIBE_*` env vars *above* `~/.vibe/config.toml`, so the
global config is never rewritten and two concurrent sessions cannot race each
other's model. `GET/PUT /api/models` is the API; `app/models.py` owns the
store. The orchestrator role is offered but disabled: nothing would use it.

## 4. Write cards so the first run succeeds

The cheapest token is the one never spent:

- One card = one small, unambiguous task. Split big specs into separate cards
  (they run on separate branches and merge independently).
- Be concrete: name files, expected behavior, and the test command to satisfy.
  Ambiguity burns fixer rounds.
- Set `VIBE_HARNESS_TEST_CMD` (e.g. `pytest -q`, `npm test`) so the test gate
  catches breakage before the reviewer — and so accept is ship-only-green.
- Don't re-run a Done card casually; each re-run clears the session and starts
  a fresh paid run. Edit → run only when the spec actually changed.
- Use the card `template` field for recurring card shapes so the spec is
  already structured.

## 5. Measure what you spend

- **Stats bar** (`GET /api/stats`, optionally `?project=<name>`): counts by
  status, Σ tokens, Σ runtime — scope it per project to compare. Hovering the
  **Tokens** counter shows the token-type breakdown (`token_breakdown`:
  cached · output · input, summed over the visible cards; a type nobody
  reported stays hidden).
- **Per-card chips:** each card shows the last session's token count and model;
  hovering a token chip shows that session's type breakdown
  (`session.token_breakdown` from `export.json` usage, or
  `session.live_token_breakdown` while live).
- **Live while running:** while a session is live, the card chip and the
  stats-bar total update in place (`session.live_tokens` on the card,
  `live_tokens` in `/api/stats`; the top-bar pill pulses while > 0). Source: the
  spawned vibe's own session journal — `$VIBE_HOME/logs/session/unified/<id>/journal/*.jsonl`
  carries the cumulative `contextUsage` (`totalTokens` plus `inputTokens` /
  `outputTokens` / `cachedInputTokens`, which feed the live hover breakdown),
  and `logs/session/active/<id>.lock.json` maps the spawned pid to the session
  (`app/liveusage.py`). Cost: incremental tail reads only, ≤ 1 scan per 2 s
  per live card, zero I/O when nothing is running, no extra requests from the
  browser (it rides the existing 2 s poll) and no LLM spend. Best effort: if
  the journal is missing or changes schema, the card falls back to the last
  recorded `export.json` count.
- **Per-round artifacts:** `sessions/<id>/run-N/export/export.json` and
  `review-N/export/export.json` hold `usage.total_tokens` (plus
  `input_tokens` / `output_tokens` / `cached_input_tokens` when the model
  reports them), the model, and the stop reason — diff these across
  cards/agents to find your expensive routes.
- **Per-round verdicts:** `session.reviews` keeps every round's record
  (`{approved, reason, round, tokens, model, log, export_dir, via, commit}`);
  `session.review` is the last one. The Diff panel's `round` range reads them,
  so you can see what each review round actually judged — and what it cost —
  without re-running anything. Round 1 has no previous round, so it starts at
  the merge-base of the card branch and its base (never the base tip, which
  would show the base's newer commits as deletions).
- USD cost is intentionally not shown in the UI, but `--max-price` still caps
  real spend per session.

## 6. Low-token preset (copy-paste)

```bash
export VIBE_HARNESS_MAX_PRICE=1.00          # hard per-session cost cap
export VIBE_HARNESS_TIME_LIMIT=600          # 10 min per session
export VIBE_HARNESS_MAX_REVIEW_ROUNDS=3     # stop paying for fixer loops sooner
export VIBE_HARNESS_TEST_CMD="pytest -q"    # fail fast, review only green code
export VIBE_HARNESS_TEST_TIMEOUT=300
export VIBE_HARNESS_ENABLED_TOOLS="read,write,edit,bash,grep,glob"
export VIBE_HARNESS_REVIEW_AGENT=ask        # read-only reviewer (default)
export VIBE_HARNESS_WORKER_RESUME=1        # resume the worker across fix rounds (default)
export VIBE_HARNESS_REVIEW_DIFF=1          # reviewer sees only diff-since-last-review + tests (default)
export VIBE_HARNESS_MAILBOX=1              # per-card MCP mailbox for worker<->reviewer (default)
# export VIBE_HARNESS_AGENT=cheap           # custom agent pinned to a small model
```

## 7. The harness MCP (models talking back to the board)

Every **run** session (implement/fix — not review, not the merge resolver)
gets a `harness` MCP server injected through vibe's `VIBE_MCP_SERVERS` env var
(`app/runner.py::SessionRunner._session_env`), so no global vibe config is
touched. The server (`app/mcp_server.py`, stdlib-only stdio JSON-RPC) forwards
to the dashboard HTTP API — the dashboard stays the single writer of cards
and sessions. Tools are published as `harness_<tool>` in the `mcp_harness`
group: `current_card`, `list_cards`, `get_card`, `create_card`, `update_card`,
`delete_card`, `run_card`, `stop_card`, `accept_card`, `revert_card`,
`card_log`,
`list_templates`, `stats`, a **read-only** `list_projects`, `ask_user`, the
dependency-link tools `add_dependency` / `remove_dependency` /
`list_dependencies` (chain cards so one runs automatically when another
completes — done/accepted; the dashboard draws a red string between the
linked cards, and dragging a draft card onto another card creates the same
link), the **archive** tools `create_archive` / `list_archives` /
`get_archive` / `archive_card` / `archive_cards` / `archive_stack` /
`restore_card` (see below),
and the **stack** tools `list_stacks` / `create_stack` / `update_stack` /
`delete_stack` / `stack_cards` / `unstack_card` (see below).

- **Stacks (piles of cards).** A stack is a named pile of cards that sit in one
  board column, with an optional header. Stacks are **server state**
  (`stacks.json`, `app/stacks.py`, `/api/stacks`), not browser state: the board
  and the MCP tools must see the same piles, and an LLM cannot reach a
  browser's localStorage. Models can build a pile (`create_stack` with a
  `title`), rename it (`update_stack`), move cards in (`stack_cards`, which
  detaches them from whatever pile they were in) and out (`unstack_card`), and
  dissolve it (`delete_stack` — the cards stay on the board). A pile needs at
  least two cards and only holds cards from one column (400 otherwise); a pile
  that falls below two cards is dissolved. Stacking costs no tokens (pure
  state edits).

- **Archives (cards & decks).** The archive is a folder structure:
  `archives/<slug>-<id>/{archive.md, cards/}` per archive, each with a
  free-form type label (`cards`/`deck`/`project`/…). Archives created before
  the notes → cards rename keep their items in `notes/` instead; every read
  (`list`/`get`/`restore`/move) looks in both folders, new items always land
  in `cards/`. Models can create typed
  archives and move **accepted cards** into them (`archive_card`, or
  `archive_cards` for a batch) and back (`restore_card`). `archive_stack`
  retires a whole deck in one call: its cards move, the deck is recorded
  in the manifest (`archived_stacks`: title + card order) and dissolved,
  and without an `archive_id` the archive is created (or reused) under
  the deck's title — one archive per retired deck, which is the board's
  structure. Archiving a whole *project* deck
  (`POST /api/archives/{id}/projects/{name}`) stays a user-side action —
  there is deliberately no MCP tool for it, same as no project
  create/delete/switch. Archiving costs no tokens (pure file moves) and keeps
  the card's full history; archived cards leave the board. `GET
  /api/archives/{id}` also groups the archive's cards under project headers
  (`projects`: one group per card `project`, unfiled cards last under `name:
  null`, decks archived wholesale keep a header even with no cards left) —
  the sidebar's archive tree and the Archives modal render that as one more
  level, so an archive reads as its projects.

- **`ask_user` (model → user, blocking).** When the spec is genuinely
  ambiguous, the model calls `harness_ask_user(question, options?,
  timeout_seconds?)`: the question is posted to the card
  (`sessions/<id>/questions.json` via the dashboard API) and the tool call
  long-polls until the user answers on the card, dismisses it, or the timeout
  hits (default `VIBE_HARNESS_ASK_USER_TIMEOUT` = 600 s, capped at 3600 s).
  The result carries the answer, or `timeout`/`cancelled` (then the model
  makes a reasonable choice and records it in NOTES.md). While blocked, the
  session spends no LLM tokens — the wait is a plain HTTP long-poll
  (`GET /api/cards/<id>/questions/<qid>/wait`), so asking is cheap; the cost
  risk is only the *pause*, bounded by the timeout. Pending questions are
  cancelled when the session ends (runner `_finalize`/`recover_stale`), and
  the run/fix prompts tell the model to use it sparingly (minor ambiguity →
  pick a default + NOTES.md).

- **Split big specs:** the run/fix prompts tell the model to *first* judge
  whether the spec is too big for one session and, if so, create one card per
  subtask via `harness_create_card` — drafts by default, `auto_run=true` to
  run immediately, `harness_run_card` later. This is the "mission
  decomposition" idea from §7 below, now implemented at the model's
  discretion: **watch token spend on cards that spawn sub-cards** — each
  sub-card run is another paid session (capped per card by
  `--max-price`/`--time-limit` and the review-loop cap).
- **No project swapping:** the MCP has no create/delete/switch-project tool,
  and `create_card`/`update_card` reject `project`/`workdir` args — sub-cards
  inherit the current card's project/workdir (the MCP server learns the
  current card from `VIBE_HARNESS_NOTE_ID` in its env, falling back to the
  `sessions/<id>/…` workdir path).
- **Allowlists:** when `VIBE_HARNESS_ENABLED_TOOLS` is set, the runner appends
  the `harness_*` glob so the MCP tools survive the allowlist
  (`app/runner.py::_build_cmd`).
- **Knobs:** `VIBE_HARNESS_MCP_ENABLED=0` disables the injection entirely;
  `VIBE_HARNESS_MCP_SERVER_NAME` renames the alias (tools become
  `<name>_<tool>`).

## 8. Future routing ideas (not yet implemented)

- Per-card agent/model choice in the editor.
- `--max-turns` (supported by `vibe` headless mode, not yet passed by the
  harness) as an extra per-session turn cap.
- Mission decomposition: one orchestrator card spawning sub-sessions per
  subtask. *(Partially landed: the harness MCP lets a run split its spec into
  sub-cards, and those sub-cards are now CHILDREN of the card that created them
  — `parent_id`, shown on the board as `⤷ N sub-cards` / `↳ <parent>` chips.
  There is still no orchestrator session: the worker decomposes itself. The
  sidebar's ⤷ Split panel reports tokens per accepted outcome for split vs
  unsplit cards, which is the measurement that decides whether a coordinator
  session is worth building.)*

### Why there is no orchestrator session yet

Planning up front is the cheap, easy part and the most likely to be wrong: a
planner writes the plan before any code exists, from the spec alone, and the
plan is stale by the third sub-task. The value of decomposition is **context
isolation** (five focused sessions beat one 3M-token session), not the plan
document. So the order is: make decomposition *visible* (done — `parent_id` +
the tree chips), *measure* it (done — `/api/split-report`), and only then build
a coordinator. When it comes, it should be a small state machine with no
write access (it routes work, it does not do it), woken by child-completion
events rather than polling, with a token budget for the whole subtree.
