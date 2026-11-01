# Architecture

What lives where, and how to run the tests.

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

## Data on disk

Everything the board owns lives beside the code and is **gitignored** — the
repository holds the harness, not your work.

| Path | What it is |
|---|---|
| `cards/` | the cards (markdown + YAML frontmatter) |
| `sessions/` | per-card run/review rounds: `prompt.md`, `session.log`, `export/`, worktrees |
| `archives/` | archive folders (`archives/<name>-<id>/`) |
| `projects.json` | the registered projects |
| `stacks.json` | the board's decks |
| `models.json` | the per-role model choice |
| `NOTES.md` | the agents' decision log (local working notes) |
