# The harness MCP

How spawned models talk to the board. The full tool reference — every tool,
its arguments and what it returns — is in [`MCPTOOLS.md`](../MCPTOOLS.md).

## What it is for

A card run is a headless session: nobody is at its keyboard. The MCP is how it
still acts on the board the user is looking at — read the card it implements,
split an oversized spec into sub-cards, chain them with dependencies, run or
stop them, accept finished work, read a transcript, and ask the user a question
when the spec is genuinely ambiguous.

The server (`app/mcp_server.py`) is a stdlib-only stdio JSON-RPC loop that
forwards every call to the dashboard's HTTP API (`VIBE_HARNESS_URL`, default
`http://127.0.0.1:8734`), so the dashboard stays the single writer of cards and
sessions. Inside a run the model sees an `mcp_harness` tool group whose tools
are published as `harness_<tool>` (`VIBE_HARNESS_MCP_SERVER_NAME` sets the
prefix).

## How a session gets it

Only **run** sessions (implement and fix rounds) get the board tools; review
and resolver sessions stay MCP-free. The runner injects the server per backend:

| Backend | How the server is attached |
|---|---|
| Vibe | `VIBE_MCP_SERVERS` in the spawned session's environment — no global config is touched, and any server already in the inherited value is kept |
| Claude | a `--mcp-config` JSON file written into the round's directory (`mcpServers` map, same command and env) |

Disable the whole thing with `VIBE_HARNESS_MCP_ENABLED=0`. When a tool
allowlist is configured (`VIBE_HARNESS_ENABLED_TOOLS`), the harness appends
`harness_*` so the board tools survive it.

## What is on it

Grouped, with the full detail in [`MCPTOOLS.md`](../MCPTOOLS.md):

- **cards** — `current_card`, `list_cards`, `get_card`, `create_card`,
  `update_card`, `delete_card`, `run_card`, `stop_card`, `accept_card`,
  `accept_all_cards`, `revert_card`, `card_log`
- **dependencies** — `add_dependency`, `remove_dependency`,
  `list_dependencies`: link cards so one runs automatically once another is
  merged (accepted). `add_dependency(card_id=<dependent>,
  depends_on=<dependency>)`; the board draws a red string between linked cards,
  and the same link can be made by dragging a draft card onto another card.
- **stacks** — `list_stacks`, `create_stack`, `update_stack`, `delete_stack`,
  `stack_cards`, `unstack_card`: the board's named piles of cards in one column.
- **archives** — `create_archive`, `list_archives`, `get_archive`,
  `archive_card`, `archive_cards`, `archive_stack`, `restore_card`: shelve
  finished work into typed archive folders under `archives/`. `archive_stack`
  retires a whole stack in one call and records it in the archive's manifest,
  so the board's stacks are the structure the archive keeps.
- **transcripts** — `list_transcripts` / `get_transcript`: one metadata row per
  round, and one round's journal parsed into text (assistant output, reasoning,
  tool calls and results, capped at `limit` events). The transcripts are
  compressed in the card's project store
  (`<project>/.vibe-harness/transcripts.db`); a model reads the parsed text,
  never the blob. This is the debugging surface for "what did the model
  actually do in that round".
- **models / projects / templates / stats** — `list_models`, `list_projects`,
  `list_templates`, `stats` (all read-only).
- **ask_user** — **ask the user a question on the card**: the model calls it
  when the spec is ambiguous and a wrong guess would be costly. The question
  appears on the card (with optional multiple-choice buttons and a text input)
  and the tool call **blocks** until the user answers on the card, dismisses
  it, or the timeout hits; the result carries the answer (or
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
(`auto_run=true`) or later via `harness_run_card`.

## The other channel: the mailbox, and steering

The `harness` MCP is how a model acts on the board; the per-card **mailbox**
MCP (`harness-mailbox`, see [`MCPTOOLS.md`](../MCPTOOLS.md)) is how it talks to
the person watching. It is attached through the `.vibe/config.toml` the harness
writes into the card's worktree (`VIBE_HARNESS_MAILBOX`, on by default) and
gives the worker and the reviewer three tools: `post_message`,
`wait_for_message` and `submit_verdict` (reviewer only).

That mailbox is also the **steering channel**. The worker prompt tells the
model to call `wait_for_message(timeout_seconds=0)` between major steps and to
reply with `post_message(to="user", ...)`. The activity popup's footer posts
guidance with `POST /api/cards/<id>/messages` and reads the conversation back
with `GET /api/cards/<id>/messages`; the message lands in the same mailbox the
worker's next non-blocking check reads. A run with no user messages is
unaffected — the check returns `{"none": true}` immediately.
