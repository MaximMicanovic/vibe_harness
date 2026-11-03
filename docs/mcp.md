# The harness MCP

How spawned models talk to the board. The full tool reference, with every
argument, is in [`MCPTOOLS.md`](../MCPTOOLS.md).

Every **run** session the harness spawns gets a `harness` MCP server injected
(via vibe's `VIBE_MCP_SERVERS` env var, so no global config is touched; review
and resolver sessions stay MCP-free). Inside a run, the model sees an
`mcp_harness` tool group with `harness_*` tools backed by
`app/mcp_server.py` (a stdlib-only stdio JSON-RPC server that forwards to the
dashboard's HTTP API, so the dashboard stays the single writer):

- `harness_current_card` — which card this session implements (id, project, …)
- `harness_list_cards` / `harness_get_card` / `harness_create_card` /
  `harness_update_card` / `harness_delete_card`
- `harness_run_card` / `harness_stop_card` / `harness_accept_card` /
  `harness_accept_all_cards` / `harness_card_log`
- `harness_add_dependency` / `harness_remove_dependency` / `harness_list_dependencies` —
  link cards so one runs automatically once another is merged (accepted):
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
- `harness_list_transcripts` / `harness_get_transcript` — read a card's
  stored session transcripts: one metadata row per round, and one
  round's journal parsed into text (assistant output, reasoning, tool
  calls and results, capped at `limit` events). The transcripts are
  compressed in the card's project store
  (`<project>/.vibe-harness/transcripts.db`); a model reads the parsed text,
  never the blob.
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

## The other channel: steering through the mailbox

The `harness` MCP is how a model acts on the board; the per-card **mailbox**
MCP (`harness-mailbox`, see [`MCPTOOLS.md`](../MCPTOOLS.md)) is how it talks to
the person watching. The worker prompt tells the model to call
`wait_for_message(timeout_seconds=0)` between major steps and to reply with
`post_message(to="user", ...)`. The chat popup's footer posts guidance with
`POST /api/cards/<id>/messages` and reads the conversation back with
`GET /api/cards/<id>/messages`; the message lands in the same mailbox the
worker's next non-blocking check reads. A run with no user messages is
unaffected — the check returns `{"none": true}` immediately.
