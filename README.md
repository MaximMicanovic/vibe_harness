# Vibe Harness

**A local kanban board for AI coding agents.**

You write a spec on a card. The harness hands it to a headless
[Mistral Vibe](https://mistral.ai) session, which implements it on its own git
branch in its own worktree. A second, read-only session then reviews the diff
and runs the tests. Only then can you accept the card — and accepting merges the
branch.

## Why

Coding agents are good at a single task and bad at being trusted. A chat window
gives you no review gate, no isolation, no record, and no way to run five things
at once without them stepping on each other.

Vibe Harness puts the agent behind a board:

- **A spec, not a conversation.** Each card is a self-contained brief. The
  session that implements it starts fresh, so it cannot drift from what you
  asked for over a long chat.
- **Nothing ships unreviewed.** A separate reviewer session inspects the diff
  and runs the test suite itself. A card cannot be accepted without an approval,
  and cannot be merged with red tests.
- **Isolation by default.** Every card runs on `harness/<card-id>` in a linked
  git worktree, so several cards in one repository run concurrently and your
  working copy stays on your branch.
- **A record you keep.** Prompts, logs, token usage and cost for every round
  stay on the card. Runs survive a server restart.
- **The agent can use the board.** Run sessions get an MCP server with ~30
  tools: split a spec into sub-cards, chain them with dependencies, stack cards
  into decks, archive finished work — and ask *you* a question on the card when
  the spec is genuinely ambiguous.

It is a **Vibe-only** tool: it shells out to the `vibe` CLI in headless mode and
reads Vibe's `export.json` for token usage and run metadata. No other agent CLIs
are supported.

## Quickstart

```bash
uv venv
uv pip install -e ".[dev]"
uvicorn app.main:app --reload --reload-dir app --port 8734
```

Open <http://127.0.0.1:8734>, register a project with **＋ Project**, write a
card, and press **Run with Vibe**.

Always pass `--reload-dir app`: card worktrees live under `sessions/`, and
watching those makes every agent edit restart the server.

**Requirements:** Python 3.12+, [uv](https://docs.astral.sh/uv/), the `vibe` CLI
on `PATH`, and a Vibe API key configured for it (`vibe --setup`). The dashboard
itself needs no key — it only shells out to `vibe`.

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
