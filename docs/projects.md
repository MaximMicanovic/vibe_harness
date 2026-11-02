# Projects

The dashboard is project-centric: a project is a folder the harness may run
cards in, and registering one sets it up for agents.

The dashboard is **project-centric**. Register a project with the **＋ Add
project…** entry at the bottom of the **Project** dropdown in the top bar (name
and folder — on macOS, **Browse…** opens the native folder picker and fills in
the path; the folder is created if missing), then pick it in that same
dropdown. The board and the stats bar then show only that project's cards, and
the top-bar selection is remembered per browser, so a reload lands back on the
project you were working in. The **last used project** is likewise remembered
per browser — updated when you save a card with a project (or without one) and
when you pick a concrete project in the top bar — and when you press **+ New
card**, the editor's **Project** dropdown lets you pick the project and
automatically defaults to it (the top-bar selection is the fallback when
nothing was used yet).

When you run a card, Vibe works in the project's folder (runs are auto-approved,
so it edits that project directly). A card's **Work folder override** field
redirects a single card elsewhere; a card with no project runs in an isolated
sandbox (`sessions/<id>/workspace`).

Registering a project also **sets up its folder**: `git init` (with an initial
commit, or an immediate commit of the new files if it's already a repo), a
managed **`agent.md`** with rules for agents working there — including "always
keep `route.md` up to date when relevant" — and an **empty `route.md`** that
agents fill in. Every server start re-syncs the managed `agent.md` copies
(remove its marker comment to keep your own version); `route.md` is agent-owned
and never touched by the harness once it exists. Session prompts tell each Vibe
agent to read and follow `agent.md`.

Safety: optionally restrict which folders runs may use:

```bash
export VIBE_HARNESS_ALLOWED_WORKDIRS=/Users/you/programming,/Users/you/work
```

(comma-separated absolute path prefixes; empty = allow all; violations return 403).
