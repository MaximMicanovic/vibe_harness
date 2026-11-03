# MCPTOOLS.md — every tool an LLM session can call

A spawned session sees tools from three places. This file documents all of
them, derived from the code (`app/mcp_server.py`, `app/mailbox_server.py`) —
not from memory. If a tool is not here, it does not exist.

| Source | Published as | Count | Who gets it |
|---|---|---|---|
| Harness MCP server (`app/mcp_server.py`) | `harness_<tool>` in the `mcp_harness` group | 35 | `run` sessions (implement + fix rounds) |
| Mailbox MCP server (`app/mailbox_server.py`) | `harness-mailbox_<tool>` in the `mcp_harness_mailbox` group | 3 | worker and reviewer sessions, when `VIBE_HARNESS_MAILBOX` is on |
| Vibe's built-in tools | bare names (`bash`, `read_file`, …) | platform-dependent | every session, filtered by `VIBE_HARNESS_ENABLED_TOOLS` |

The `harness_` prefix is `VIBE_HARNESS_MCP_SERVER_NAME` (default `harness`):
vibe publishes an MCP tool as `<server name>_<tool name>`, so `create_card`
arrives as `harness_create_card` in the `mcp_harness` group. Review and
resolver sessions are deliberately MCP-free — they only get the built-ins.

**Read-only vs mutating:** `list_*`, `get_*`, `stats`, `card_log` and
`current_card` only read the board; every other tool changes it. The full
read-only list is in [section 2](#2-read-only-vs-mutating).

---

## 1. Harness MCP server — `harness_*` (35 tools)

A thin, stdlib-only JSON-RPC stdio server that forwards every call to the
dashboard's HTTP API (`VIBE_HARNESS_URL`, default `http://127.0.0.1:8734`), so
the dashboard stays the single writer of cards and sessions. The runner injects
it into every run session: Vibe reads it from `VIBE_MCP_SERVERS`, Claude from a
`--mcp-config` file (`app/runner.py`).

### Cards

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `current_card` | The card this session is implementing (id, title, status, project, workdir). The "ground yourself first" call. | — | `{card_id, card}`, or `{card_id: null, detail}` outside a card session |
| `list_cards` | Cards on the board, newest first, each with `depends_on` and — for a card that failed — `session.failure` (`{kind, detail}`: why it failed, in one line). | `status` (one of `draft`/`running`/`reviewing`/`done`/`failed`/`stopped`/`accepted`), `project` | array of cards |
| `get_card` | One card in full: body, session record, review, git info, and `session.failure` when it failed. | `card_id` | the card |
| `create_card` | New card, starts as a **draft**. Split oversized work into one card per subtask; the new card inherits the calling card's project/workdir and becomes its child (`parent_id`). | `title`, `body`, `template`, `auto_run` (default false), `auto_accept` (default false) | the new card |
| `update_card` | Edit title/body/auto_run/auto_accept. Editing a finished card resets it to draft. | `card_id`, plus any of `title`, `body`, `auto_run`, `auto_accept` | the updated card |
| `delete_card` | Delete a card with its session dir and linked worktree. 409 while running. | `card_id` | `{deleted: <card_id>}` |
| `run_card` | Run a card now — spawns a headless session. 409 while already running. | `card_id` | the card |
| `stop_card` | Stop the live session (SIGTERM, escalating to SIGKILL). | `card_id` | `{stopped: <card_id>}` |
| `accept_card` | Accept a done card: test gate, then ship it — `local` mode merges the branch into the base branch, `pr` mode pushes it and opens a pull request whose body carries the review verdict. Needs an approved review (or an exhausted review loop). A second accept for the same repo is queued (`session.accept_queued` + `queue_reason`) and merged when the repo frees. | `card_id` | the card |
| `accept_all_cards` | Accept every card **currently** in Done, merged one at a time. The batch is a snapshot: a card that reaches Done later is not included. | — | `{queued: [card ids], running: bool}` |
| `revert_card` | Undo an accepted card: `git revert` the merge commit its accept recorded, then reopen the card as a draft (session history kept). 409 when the card has no recorded merge commit, when the repo is dirty or mid-merge/revert, and on a conflicting revert — which is aborted, leaving the repo untouched. | `card_id` | the card |
| `card_log` | Tail of the card's current session log (the CLI's output). | `card_id`, `tail` (1–5000, default 200) | `{lines: [...]}` |

`create_card` and `update_card` **reject `project` and `workdir`**: a card
created through the MCP inherits the calling card's project, and there is no
tool to create, delete, or switch projects.

### Dependencies (pipeline order)

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `add_dependency` | `card_id` runs automatically once `depends_on` reaches done/accepted. Starts immediately if the dependency is already complete, otherwise waits (`session.waiting_for`). 409 on self-link, cycle, unknown card, or a card with a live session. | `card_id` (the dependent), `depends_on` (the trigger) | the card |
| `remove_dependency` | Drop the link. If it was the last unmet dependency of a draft, that draft starts immediately. | `card_id`, `depends_on` | the card |
| `list_dependencies` | With `card_id`: `{depends_on, blocks, unmet}` for that card (`blocks` = cards waiting on it, `unmet` = dependencies not yet done/accepted). Without: every card's `depends_on`. | `card_id` (optional) | `{depends_on, blocks, unmet}` or `[{id, title, status, depends_on}]` |

### Stacks

A stack is a named pile of cards that sits in one column, with a header. A card
lives in one stack at a time.

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `list_stacks` | The board's stacks: id, col, title, `ids` (bottom → top). | — | array of stacks |
| `create_stack` | New stack from at least two cards, all in one column. Cards already stacked elsewhere are moved here. | `title`, `card_ids` (≥ 2), `col` (default: the first card's column) | the stack |
| `update_stack` | Rename a stack and/or replace its members (keeps id and header). | `stack_id`, `title`, `card_ids` (≥ 2) | the stack |
| `delete_stack` | Dissolve a stack; its cards stay on the board. | `stack_id` | `{deleted: <stack_id>, cards: [card ids]}` |
| `stack_cards` | The way to move cards: add them to `stack_id`, or create a stack when it is omitted (then pass `title`). Cards must be in the stack's column. | `card_ids`, `stack_id` (optional), `title` (for a new stack) | the stack |
| `unstack_card` | Pull one card out of its stack; a stack left with a single card is dissolved. | `card_id` | `{stack, dissolved, card_id}` — `stack` is null when the pile dissolved |

### Archives

An archive is a folder under `archives/` with a manifest and a `cards/`
subfolder. Archives carry a type label (`cards`, `deck`, `project`, or any
slug) so different kinds can live side by side.

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `create_archive` | Create an archive folder with a manifest. | `name` (unique; becomes the folder slug), `type` (default `cards`), `description` | the archive |
| `list_archives` | Archives newest first: id, name, type, item count, archived stacks. | `type` (optional filter) | array of archives |
| `get_archive` | One archive: manifest plus its archived cards (`items`). | `archive_id` | the archive |
| `archive_card` | Move a card into an archive; it leaves the board. Only **accepted** cards can leave the board (409 otherwise); an archived card can move between archives. | `card_id`, `archive_id` | `{archive, cards: [moved cards]}` |
| `archive_cards` | Same, batched — a whole stack of accepted cards in one call. Every board card must be accepted. | `card_ids`, `archive_id` | `{archive, cards: [moved cards]}` |
| `archive_stack` | Retire a whole stack in one call: its cards move into the archive, the stack is recorded in the manifest (`archived_stacks`: title + card order) and dissolved. Without `archive_id` an archive named after the stack (type `deck`) is created or reused — one archive per retired stack. | `stack_id`, `archive_id` (optional) | `{stack, archive, cards: [moved cards]}` |
| `restore_card` | Bring an archived card back to the board (Accepted column, history intact). | `card_id` | the card |

### Transcripts

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `list_transcripts` | A card's stored session transcripts: one metadata row per round (`round`, `session_id`, `created_at`, `raw_bytes`, `gz_bytes`). The transcripts live compressed in the card's project store (`<project>/.vibe-harness/transcripts.db`). | `card_id` | array of rows |
| `get_transcript` | One round's transcript as text: the parsed journal events (assistant text, reasoning, tool calls and their results), capped at `limit` events. The debugging surface for "what did the model actually do in that round" — the raw tar.gz blob is not readable by a model. | `card_id`, `round` (`run-1`, `review-2`, `resolve-1`, …), `limit` (1–500, default 200) | `{card_id, round, events, text}` |

### Models, projects, templates, stats

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `list_models` | The model aliases the board offers (the same list the role pickers use), plus which model each role currently uses. Sessions resolve their model from the **role** — worker, reviewer or resolver — so this is the list that decides what actually runs. | — | `{models: [{alias, label, backend, thinking}], roles: [{role, model, backend, inherited}], default_model, card}` |
| `list_projects` | Registered projects (name + path). Read-only by design. | — | array of `{name, path}` |
| `list_templates` | Card templates usable with `create_card`. | — | array of templates |
| `stats` | Counts by status, total tokens, total/avg runtime. | `project` (optional) | `{counts, total_tokens, live_tokens, token_breakdown, total_runtime_seconds, avg_runtime_seconds, project}` |

### Talking to the user

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `ask_user` | Posts a question **on the card** and blocks the session until the user answers, cancels, or the timeout hits. Only available inside a card session. | `question`, `options` (1–8, rendered as buttons), `timeout_seconds` (1–3600, default 600) | `{question_id, question, status, answer?, hint?}` — `status` is `answered`, `timeout` or `cancelled` |

Use it only when the spec is genuinely ambiguous and a wrong guess is costly;
minor ambiguity should just be decided and noted in `NOTES.md`. On `timeout` or
`cancelled`, pick a sensible default and record it.

---

## 2. Read-only vs mutating

These tools only read the board — they are safe to call at any time and never
change a card, a session or a file:

| Read-only tool | Reads |
|---|---|
| `current_card` | this session's card |
| `list_cards` | the board (optionally filtered) |
| `get_card` | one card |
| `card_log` | the tail of a session log |
| `list_dependencies` | dependency links |
| `list_stacks` | the board's stacks |
| `list_archives` | archives |
| `get_archive` | one archive |
| `list_transcripts` | a card's transcript rows |
| `get_transcript` | one round's parsed transcript |
| `list_models` | model aliases and role choices |
| `list_projects` | registered projects |
| `list_templates` | card templates |
| `stats` | board statistics |

Every other tool **mutates the board**: `create_card`, `update_card`,
`delete_card`, `run_card`, `stop_card`, `accept_card`, `accept_all_cards`,
`revert_card`, `add_dependency`, `remove_dependency`, `create_stack`,
`update_stack`, `delete_stack`, `stack_cards`, `unstack_card`,
`create_archive`, `archive_card`, `archive_cards`, `archive_stack`,
`restore_card`, and `ask_user` (it posts a question on the card).

---

## 3. Mailbox MCP server — `harness-mailbox_*` (3 tools)

Attached per card through the `.vibe/config.toml` the harness writes into the
card's worktree (`app/mailbox.py`), so the persistent worker and the fresh
reviewer can talk without sharing context. State lives on the filesystem in a
per-card mailbox directory; the role comes from `VIBE_HARNESS_MAILBOX_ROLE`.

| Tool | What it does | Arguments | Returns |
|---|---|---|---|
| `post_message` | Append a message addressed to the other side. `from` is the caller's role. `to="user"` replies to the person watching the run (the popup shows it). | `to` (`worker` \| `reviewer` \| `user`), `text` | the stored message `{seq, from, to, text, ts}` |
| `wait_for_message` | Block until an unconsumed message for the caller's role arrives. The worker prompt calls it with `timeout_seconds=0` between major steps, so a silent user never stalls a run. | `timeout_seconds` (default 60) | `{message: {seq, from, to, text, ts}}`, or `{"none": true, "role": …}` on timeout |
| `submit_verdict` | The reviewer's structured verdict, stored per round (`verdicts/<round>.json`) and mirrored to `verdict.json`. **Reviewer role only** — any other role gets a tool error. | `approved`, `reason` | `{ok: true, verdict: {approved, reason, from, round, ts}}` |

The harness reads only the current round's verdict file, so a round that never
calls `submit_verdict` sees no verdict rather than inheriting the previous one.

The `user` target is the steering channel: the activity popup's footer posts
guidance with `POST /api/cards/<id>/messages` (stored as `from: "user"`,
`to: "worker"`), the worker's next non-blocking `wait_for_message` returns it,
and the worker's `post_message(to="user", ...)` reply shows up in the popup.
There is no `user` session, so nothing ever waits for that role.

---

## 4. Vibe's built-in tools

Not MCP — these ship with the CLI and are always present unless filtered out.
Names are the snake_case of the tool class.

| Tool | Purpose |
|---|---|
| `bash` | Run a shell command (the legacy one-shot tool) |
| `read_file` | Read a file, with offset/limit |
| `write_file` | Create or overwrite a file |
| `edit` | Exact string replacement in a file |
| `grep` | Content search |
| `todo` | Session task list |
| `agents.spawn` | Spawn a subagent (`task` is the legacy name for this) |
| `skill` | Load a skill |
| `web_fetch` / `web_search` | Network access, when enabled |
| `ask_user_question` | Vibe's own question tool (distinct from `harness_ask_user`) |
| `bash_output`, `bash_stdin`, `bash_sessions`, `bash_log_file` | Managed background shells — POSIX names; native Windows gets `git_bash*` or `powershell*` instead |

`VIBE_HARNESS_ENABLED_TOOLS` (comma-separated) becomes repeated
`--enabled-tools` flags, which in headless mode **disables every other tool** —
that is the main token lever, since unused tool schemas still cost prompt
tokens. When the allowlist is set, the runner appends `harness_*` so the board
tools survive it (`app/runner.py`, `_build_cmd`).

Which shell variant exists is decided by `managed_shell_tools_enabled` plus the
`vibe_cli_managed_shell_tools` experiment: the default keeps the legacy
one-shot `bash`, the managed variant exposes the `bash_output` family above.

---

## How to read the live surface

The schemas the model actually receives come straight from the `TOOLS` lists:

```bash
# the 35 harness tools, with descriptions and JSON schemas
uv run python -c "import json, app.mcp_server as m; print(json.dumps(m.TOOLS, indent=2))"

# the 3 mailbox tools
uv run python -c "import json, app.mailbox_server as m; print(json.dumps(m.TOOLS, indent=2))"

# just the names
uv run python -c "import app.mcp_server as m; print('\n'.join(t['name'] for t in m.TOOLS))"
```

Inside a running session the same list arrives over MCP `tools/list` on stdio,
and the model calls them as `harness_<tool>`. To see which built-ins a session
was allowed, read `config.enabled_tools` in its `export.json`
(`sessions/<id>/run-<n>/export/export.json`).

## Gotchas

- **No project tools.** Creating, deleting, or switching projects is a
  user-side action only; `create_card`/`update_card` reject `project` and
  `workdir` outright.
- **409s are normal answers**, not crashes: `delete_card`/`run_card` while
  running, `accept_card` without an approved review, `revert_card` on a card
  with no recorded merge commit (or a conflicting revert), `archive_card` on a
  non-accepted card, `add_dependency` on a cycle or a live session.
- **`ask_user` blocks the session.** A timeout is not an error — decide and
  record the choice in `NOTES.md`.
- **`submit_verdict` is reviewer-only**; the worker calling it gets a tool
  error.
- **Review and resolver sessions have no harness MCP**, so a prompt that tells
  the reviewer to "check the board" cannot work.
- **`model` is not in the published schema.** `create_card`/`update_card` read
  a `model` argument in the implementation and `list_models` advertises it, but
  neither schema declares it (`additionalProperties: false`), so a strict
  client rejects it.
- **A card's `model` field is stored, not applied.** The board's per-card
  picker writes `card.model`, but `SessionRunner._session_model` resolves every
  session from its role's model (`models.json`), so pinning a card does not
  change what runs today. Set the model on the role instead.
