# MCPTOOLS.md — every tool an LLM session can call

A spawned `vibe` session sees tools from three places. This file documents all
of them, as they exist in the code today.

| Source | Published as | Count | Who gets it |
|---|---|---|---|
| Harness MCP server (`app/mcp_server.py`) | `harness_<tool>` | 31 | `run` sessions (implement + fix rounds) |
| Mailbox MCP server (`app/mailbox_server.py`) | `harness-mailbox_<tool>` | 3 | worker and reviewer sessions, when `VIBE_HARNESS_MAILBOX` is on |
| Vibe's built-in tools | bare names (`bash`, `read_file`, …) | platform-dependent | every session, filtered by `VIBE_HARNESS_ENABLED_TOOLS` |

The `harness_` prefix is `VIBE_HARNESS_MCP_SERVER_NAME` (default `harness`):
vibe publishes an MCP tool as `<server name>_<tool name>`. Review and resolver
sessions are deliberately MCP-free — they only get the built-ins.

---

## 1. Harness MCP server — `harness_*` (31 tools)

A thin JSON-RPC stdio server that forwards every call to the dashboard's HTTP
API (`VIBE_HARNESS_URL`, default `http://127.0.0.1:8734`), so the dashboard
stays the single writer of cards and sessions. Injected through
`VIBE_MCP_SERVERS` by `app/runner.py`.

### Context and reading

| Tool | What it does | Args |
|---|---|---|
| `current_card` | The card this session is implementing (id, title, status, project, workdir), or null outside a card session. The "ground yourself first" call. | — |
| `list_cards` | Cards on the board, newest first, each with `depends_on` and — for a card that failed — `session.failure` (`{kind, detail}`: why it failed, in one line). | `status` (one of draft/running/reviewing/done/failed/stopped/accepted), `project` |
| `get_card` | One card in full: body, session record, review, git info, and `session.failure` when it failed. | `card_id` |
| `card_log` | Tail of the card's current session log. | `card_id`, `tail` (1–5000, default 200) |
| `list_templates` | Card templates usable with `create_card`. | — |
| `stats` | Counts by status, total tokens, total/avg runtime. | `project` (optional) |
| `list_projects` | Registered projects (name + path). Read-only by design. | — |

### Writing cards

| Tool | What it does | Args |
|---|---|---|
| `create_card` | New card, starts as a draft. Split oversized work into one card per subtask. | `title`, `body`, `template`, `auto_run`, `auto_accept` |
| `update_card` | Edit title/body/auto_run/auto_accept. Editing a finished card resets it to draft. | `card_id`, + any of the above |
| `delete_card` | Delete a card with its session dir and linked worktree. 409 while running. | `card_id` |

`create_card` and `update_card` **reject `project` and `workdir`**: a card
created through the MCP inherits the calling card's project, and there is no
tool to create, delete, or switch projects.

A card created through the MCP also becomes a **child** of the calling card
(`parent_id`): splitting a spec into sub-cards is decomposing that card, so the
board draws the tree — the parent card shows `⤷ N sub-cards`, each child shows
`↳ <parent>`, and both jump to the other card. `/api/split-report` compares
tokens per accepted outcome for split vs unsplit cards, which is the only
measure of whether decomposing actually pays.

### Running and accepting

| Tool | What it does | Args |
|---|---|---|
| `run_card` | Run a card now — spawns a headless vibe session. 409 while already running. | `card_id` |
| `stop_card` | Stop the live session (SIGTERM, escalating to SIGKILL). | `card_id` |
| `accept_card` | Accept a done card: test gate, then merge its branch into the base branch when git-backed. Needs an approved review (or an exhausted review loop). | `card_id` |

### Dependencies (pipeline order)

| Tool | What it does | Args |
|---|---|---|
| `add_dependency` | `card_id` runs automatically once `depends_on` reaches done/accepted. Starts immediately if the dependency is already complete, otherwise waits (`session.waiting_for`). 409 on self-link, cycle, unknown card, or a card with a live session. | `card_id`, `depends_on` |
| `remove_dependency` | Drop the link. If it was the last unmet dependency of a draft, that draft starts immediately. | `card_id`, `depends_on` |
| `list_dependencies` | With `card_id`: `{depends_on, blocks, unmet}`. Without: every card's `depends_on`. | `card_id` (optional) |

### Archives

| Tool | What it does | Args |
|---|---|---|
| `create_archive` | Folder under `archives/` with a manifest and a `cards/` subfolder. | `name`, `type` (default `cards`), `description` |
| `list_archives` | Archives newest first: id, name, type, item count, archived decks. | `type` (optional) |
| `get_archive` | One archive: manifest plus its archived cards. | `archive_id` |
| `archive_card` | Move a card into an archive. Only **accepted** cards can leave the board (409 otherwise); an archived card can move between archives. | `card_id`, `archive_id` |
| `archive_cards` | Same, batched — e.g. a whole deck of accepted cards in one call. | `card_ids`, `archive_id` |
| `archive_stack` | Retire a whole deck in one call: its cards move into the archive, the deck is recorded in the manifest (`archived_stacks`: title + card order) and dissolved. Without `archive_id` an archive named after the deck (type `deck`) is created or reused — one archive per retired deck. | `stack_id`, `archive_id` (optional) |
| `restore_card` | Bring an archived card back to the board (Accepted column, history intact). | `card_id` |

### Decks (stacks)

| Tool | What it does | Args |
|---|---|---|
| `list_stacks` | The board's decks: id, col, title, `ids` (bottom → top). | — |
| `create_stack` | New deck from at least two cards, all in one column. A card lives in one deck at a time. | `title`, `card_ids`, `col` |
| `update_stack` | Rename a deck and/or replace its members (keeps id and header). | `stack_id`, `title`, `card_ids` |
| `delete_stack` | Dissolve a deck; its cards stay on the board. | `stack_id` |
| `stack_cards` | The way to move cards: add to `stack_id`, or create a deck when it is omitted (then pass `title`). | `card_ids`, `stack_id`, `title` |
| `unstack_card` | Pull one card out of its deck; a deck left with one card is dissolved. | `card_id` |

### Talking to the user

| Tool | What it does | Args |
|---|---|---|
| `ask_user` | Posts a question **on the card** and blocks the session until the user answers, cancels, or the timeout hits. Returns `status: answered` (with the answer), `timeout`, or `cancelled` — in the last two, pick a sensible default and record it in `NOTES.md`. | `question`, `options` (1–8, rendered as buttons), `timeout_seconds` (1–3600, default 600) |

Use it only when the spec is genuinely ambiguous and a wrong guess is costly;
minor ambiguity should just be decided and noted.

---

## 2. Mailbox MCP server — `harness-mailbox_*` (3 tools)

Attached per card through the `.vibe/config.toml` the harness writes into the
card's worktree (`app/mailbox.py`), so the persistent worker and the fresh
reviewer can talk without sharing context. State lives on the filesystem in a
per-card mailbox directory; the role comes from `VIBE_HARNESS_MAILBOX_ROLE`.

| Tool | What it does | Args |
|---|---|---|
| `post_message` | Append a message addressed to the other side. `from` is the caller's role. | `to` (`worker` \| `reviewer`), `text` |
| `wait_for_message` | Block until an unconsumed message for the caller's role arrives; `{"none": true}` on timeout. | `timeout_seconds` (default 60) |
| `submit_verdict` | The reviewer's structured verdict, stored per round (`verdicts/<round>.json`) and mirrored to `verdict.json`. **Reviewer role only** — any other role gets a tool error. | `approved`, `reason` |

The harness reads only the current round's verdict file, so a round that never
calls `submit_verdict` sees no verdict rather than inheriting the previous one.

---

## 3. Vibe's built-in tools

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
# the 31 harness tools, with descriptions and JSON schemas
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
  running, `accept_card` without an approved review, `archive_card` on a
  non-accepted card, `add_dependency` on a cycle or a live session.
- **`ask_user` blocks the session.** A timeout is not an error — decide and
  record the choice in `NOTES.md`.
- **`submit_verdict` is reviewer-only**; the worker calling it gets a tool
  error.
- **Review and resolver sessions have no harness MCP**, so a prompt that tells
  the reviewer to "check the board" cannot work.
