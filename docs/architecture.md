# Architecture

What lives where, and how to run the tests.

## Modules

```
app/
  __init__.py       # package marker
  config.py         # paths, defaults, env overrides
  cards.py          # CardStore: CRUD over cards/*.md (YAML frontmatter)
  runner.py         # SessionRunner: spawn/stop/track headless sessions, the
                    #   queue, the review loop, accept and revert
  backends.py       # the two agent CLIs (vibe, claude): command line, where
                    #   output lands, how it is normalized into usage/session id
  models.py         # per-role model choice — picking a model picks its backend
  settings.py       # backend, concurrency cap, accept mode (local merge or PR)
  mcp_server.py     # stdio MCP server: lets spawned models talk to the board
  mailbox.py        # harness side of the per-card MCP mailbox
  mailbox_server.py # the per-card mailbox server a session spawns (stdio)
  questions.py      # QuestionStore: per-card Q&A between a model and the user
  activity.py       # live activity feed of a running session (C + click)
  liveusage.py      # live token usage of a running session
  diffs.py          # the diff behind the D popup (card / round / worktree)
  dupes.py          # mechanical clone detector behind the duplication review
  failures.py       # why a card failed, in one line
  tokens.py         # one definition of what a card cost
  transcripts.py    # compressed session transcripts in SQLite
  search.py         # search across the board and the archives
  stacks.py         # StackStore: named piles of cards on the board
  archives.py       # ArchiveStore: archives/<slug>-<id>/ folders
  projects.py       # project registry (projects.json)
  routemd.py        # per-project git init + agent.md + route.md
  gitops.py         # the per-card git workflow: branch, worktree, merge, revert
  praccept.py       # accept by opening a pull request (accept_mode = pr)
  sleepguard.py     # keep the machine awake while cards run
  fsdialog.py       # native folder picker for the dashboard
  main.py           # FastAPI app: API routes + static file serving
  templates/        # starter card templates
  static/           # dashboard UI (vanilla JS, no build step)
cards/              # post-it cards (markdown + frontmatter)
sessions/           # per-card dirs: workspace/ (or the project folder) + per-round
                    #   run-N/ and review-N/ (prompt.md, session.log, export/)
tests/              # pytest suite
```

The frontend is `app/static/app.js` + `style.css` + `index.html`, served as-is:
vanilla JavaScript, no build step, no bundler, no framework. A change is live on
reload.

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
| `sessions/` | per-card run/review rounds: `prompt.md`, `session.log`, `export/`, worktrees, `mailbox/`, `questions.json` |
| `archives/` | archive folders (`archives/<name>-<id>/`) |
| `projects.json` | the registered projects |
| `stacks.json` | the board's stacks |
| `models.json` | the per-role model choice |
| `settings.json` | backend, concurrency cap, accept mode |
| `NOTES.md` | the agents' decision log (local working notes) |

Session transcripts are the exception: they are compressed into SQLite and kept
in `.vibe-harness/` inside each project (`<project>/.vibe-harness/transcripts.db`),
so a project's transcripts live with the project and leave with it. The harness
root's own `.vibe-harness/transcripts.db` only holds rounds of cards with no
project.
