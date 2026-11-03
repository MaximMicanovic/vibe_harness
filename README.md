# Vibe Harness

**A local kanban board for AI coding agents.**

You write a spec on a card. The harness hands it to a headless agent CLI —
[Mistral Vibe](https://mistral.ai) or
[Claude Code](https://claude.com/claude-code) — which implements it on its own
git branch in its own worktree. A second, read-only session then reviews the
diff and runs the tests. Only then can you accept the card — and accepting
merges the branch.

## Why

Coding agents are good at a single task and bad at being trusted. A chat window
gives you no review gate, no isolation, no record, and no parallel runs.

Vibe Harness puts the agent behind a board:

- **A spec, not a conversation.** Each card is a self-contained brief, and the
  session that implements it starts fresh — it cannot drift over a long chat.
- **Nothing ships unreviewed.** A separate reviewer session inspects the diff
  and runs the test suite itself. A card cannot be accepted without an approval,
  and cannot be merged with red tests.
- **Isolation by default.** Every card runs on `harness/<card-id>` in a linked
  git worktree, so several cards in one repository run concurrently and your
  working copy stays on your branch.
- **A record you keep.** Prompts, logs, token usage and cost for every round
  stay on the card. Runs survive a server restart.
- **The agent can use the board.** Run sessions get an MCP server with 35
  tools: split a spec into sub-cards, chain them with dependencies, stack cards,
  archive finished work — and ask *you* a question on the card when the spec is
  genuinely ambiguous.

The harness is a board, not an agent: it drives a headless CLI and reads back
what that CLI reports. **Mistral Vibe** and **Claude Code** are both supported,
and the model you pick for a role decides which one runs it — there is no
separate backend switch. Each keeps its own flags and its own export parsing;
everything downstream is backend-agnostic.

## What else it does

- **C + click** — a live activity popup showing what the model is doing right
  now, with an input to steer a running session.
- **D + click** — the diff, with line numbers, side-by-side mode, word-level
  highlighting and syntax colours.
- **Accept all** — merge the whole Done column, one at a time.
- **Revert** — undo an accepted card's merge and reopen it.
- **PR mode** — accept by opening a pull request with the verdict as the body.
- **Search** — across the board and the archives.
- **Notifications** — a desktop ping when a card finishes.
- **Concurrency cap** — a queue instead of ten sessions at once.
- **Sleep guard** — the machine stays awake while cards run.
- **Transcripts** — every session kept, compressed, per project.
- **Duplication review** — the reviewer checks for copy-paste and redundancy.

## Quickstart

```bash
uv venv
uv pip install -e ".[dev]"
uvicorn app.main:app --reload --reload-dir app --port 8734
```

Open <http://127.0.0.1:8734>, register a project from the **Project** dropdown
(**＋ Add project…** at the bottom), write a card, and press **▶ Run** (the
button still says *Run with Vibe*; the backend follows the model you pick).

Always pass `--reload-dir app`: card worktrees live under `sessions/`, and
watching those makes every agent edit restart the server.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- **Either** the `vibe` CLI **or** the `claude` CLI on `PATH` — whichever you
  use — authenticated for that CLI (`vibe --setup`, or `claude` logged in).

The dashboard itself needs no key: it only shells out to the CLI you chose.

## Documentation

| | |
|---|---|
| [How a run works](docs/how-it-works.md) | The pipeline: implement → test gate → review → accept, and the per-card git workflow |
| [Configuration](docs/configuration.md) | Every `VIBE_HARNESS_*` environment variable |
| [The harness MCP](docs/mcp.md) | The tools a spawned model gets — full reference in [`MCPTOOLS.md`](MCPTOOLS.md) |
| [Projects](docs/projects.md) | Registering a project, and the `agent.md` / `route.md` it sets up |
| [Architecture](docs/architecture.md) | What lives where, data on disk, running the tests |
| [Token routing](route.md) | How work is routed through the pipeline and how to keep spend down |

## Status

Early and personal: built to run one person's board on one machine. The API and
the on-disk format are not stable yet.

## License

No license has been chosen yet, so all rights are reserved. If you want this to
be reusable, add a `LICENSE` file (MIT and Apache-2.0 are the usual picks for a
project like this).
