# Projects

The dashboard is project-centric: a project is a folder the harness may run
cards in, and registering one sets it up for agents.

## Registering and picking a project

Register a project with the **＋ Add project…** entry at the bottom of the
**Project** dropdown in the top bar (name and folder — on macOS, **Browse…**
opens the native folder picker and fills in the path; the folder is created if
missing), then pick it in that same dropdown. The board and the stats bar then
show only that project's cards, and the top-bar selection is remembered per
browser, so a reload lands back on the project you were working in.

The **last used project** is likewise remembered per browser — updated when you
save a card with a project (or without one) and when you pick a concrete
project in the top bar — and when you press **+ New card**, the editor's
**Project** dropdown lets you pick the project and automatically defaults to it
(the top-bar selection is the fallback when nothing was used yet).

## What registering sets up

Opening a project always prepares its folder, so the first card run starts from
a clean, agent-ready repository (`app/routemd.py`, `app/gitops.py`):

- **`git init`** when the folder is not a repository yet — on branch `main`,
  with a minimal `.gitignore` (committed, so test artifacts never dirty the
  tree), a repo-local `user.name`/`user.email` when none is configured (never a
  global setting), and an **initial commit** that folds in `.gitignore`,
  `agent.md` and `route.md`. An existing repository is never re-initialized;
  the new files are committed on their own so the tree stays clean.
- **`agent.md`** — a **managed** rules file for agents working in the folder
  (it carries a `<!-- vibe-harness:agent.md managed -->` marker). It tells every
  session to keep `route.md` up to date when a change affects routing or token
  spend, to run the project's test suite before finishing, and to record
  ambiguity choices in `NOTES.md`. Every server start re-syncs the managed
  copies (`routemd.sync_all_projects`), so rule updates propagate; delete the
  marker comment to keep your own version — a file without the marker is never
  overwritten.
- **`route.md`** — created **empty** and agent-owned: the harness only creates
  it when missing and never touches an existing one. Agents fill it in as they
  learn how work is routed through the project.

Session prompts tell each agent to read and follow `agent.md`.

## Where a card runs

When you run a card, the agent works in the project's folder (runs are
auto-approved, so it edits that project directly). A card's **Work folder
override** field redirects a single card elsewhere; a card with no project runs
in an isolated sandbox (`sessions/<id>/workspace`).

Git-backed runs branch per card (`harness/<card-id>`) in a linked worktree under
`sessions/<card-id>/worktree`, so the project folder itself stays on its base
branch while cards run — see
[`how-it-works.md`](how-it-works.md#git-workflow).

## Where the transcripts live

Each round's session transcript is compressed into the **project's own store**:
`<project>/.vibe-harness/transcripts.db` (SQLite, one tar.gz blob per round).
A project's transcripts therefore live with the project and leave with it, and
the folder is kept out of that repository's git through `.git/info/exclude` —
it never shows up as an untracked file or dirties a card branch. A card with no
project writes to the harness store
(`<harness root>/.vibe-harness/transcripts.db`) instead, and
`VIBE_HARNESS_TRANSCRIPTS_DB` forces one explicit database for everything.

Read them back with `harness_list_transcripts` / `harness_get_transcript` (see
[`MCPTOOLS.md`](../MCPTOOLS.md)) or the API
(`GET /api/cards/<id>/transcripts`, `GET /api/cards/<id>/transcripts/<round>/events`).

## Safety: restricting where runs may work

Optionally restrict which folders runs may use:

```bash
export VIBE_HARNESS_ALLOWED_WORKDIRS=/Users/you/programming,/Users/you/work
```

(comma-separated absolute path prefixes; empty = allow all; violations return 403).
