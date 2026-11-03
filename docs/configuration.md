# Configuration

Every setting is optional and comes from an environment variable.

All optional, via environment variables:

| Env var | Default | Purpose |
|---|---|---|
| `VIBE_HARNESS_HOST` | `127.0.0.1` | Bind host |
| `VIBE_HARNESS_PORT` | `8734` | Bind port |
| `VIBE_HARNESS_TIME_LIMIT` | `1800` | Per-session time limit (s), passed to `vibe --time-limit` |
| `VIBE_HARNESS_MAX_PRICE` | unset | Per-session price cap (USD), passed to `vibe --max-price` |
| `VIBE_HARNESS_AGENT` | unset | Vibe agent (builtin or `~/.vibe/agents/NAME.toml`) |
| `VIBE_HARNESS_APPROVE` | `auto` | `auto` → `--auto-approve`, `smart` → `--smart-approve` |
| `VIBE_HARNESS_ENABLED_TOOLS` | unset | Comma-separated tool allowlist → `--enabled-tools` |
| `VIBE_HARNESS_TEST_CMD` | unset | Test command run in the workspace after each successful run and once more at accept; a failure rejects the round into the fixer loop / blocks accept with 409 |
| `VIBE_HARNESS_TEST_TIMEOUT` | `600` | Timeout (s) for the test command, separate from vibe's `--time-limit` |
| `VIBE_HARNESS_ACCEPT_CMD` | unset | Deprecated alias for `VIBE_HARNESS_TEST_CMD` |
| `VIBE_HARNESS_REVIEW_AGENT` | `ask` | Read-only builtin agent for the review pass |
| `VIBE_HARNESS_MAX_REVIEW_ROUNDS` | `5` | Review/fix loop cap per card; when hit, the card is done-but-unapproved and must be accepted manually |
| `VIBE_HARNESS_GIT_BRANCH_PREFIX` | `harness` | Per-card git branch prefix (`<prefix>/<card-id>`) for git-backed runs |
| `VIBE_HARNESS_ACCEPT_MODE` | `local` | How accepting a card ships the change: `local` merges the card branch into the base branch in the project folder; `pr` pushes the branch to `origin` and opens a pull request whose body carries the review verdict (needs `gh`, `gh auth login` and an `origin` remote). The sidebar's **Accept mode** control (`PUT /api/settings {"accept_mode": …}`) overrides it; an unknown env value falls back to `local` |
| `VIBE_HARNESS_VIBE_BIN` | `vibe` | Path/name of the Vibe CLI |
| `VIBE_HARNESS_CLAUDE_BIN` | `claude` | Path/name of the Claude Code CLI (backend `claude`) |
| `VIBE_HARNESS_ALLOWED_WORKDIRS` | unset (allow all) | Comma-separated absolute path prefixes; a card's work folder must be inside one of them, else runs return 403 |
| `VIBE_HARNESS_MCP_ENABLED` | `1` | Inject the harness MCP server into spawned run sessions (models then get the `mcp_harness` tool group); `0` disables |
| `VIBE_HARNESS_MCP_SERVER_NAME` | `harness` | MCP server alias — tools are published as `<name>_<tool>` (e.g. `harness_create_card`) |
| `VIBE_HARNESS_ASK_USER_TIMEOUT` | `600` | Default seconds `harness_ask_user` waits for your answer on the card (per question; the tool caps it at 3600) |
| `VIBE_HARNESS_TDD` | `1` | Worker prompts (run + fixer rounds) instruct the model to work test-driven: failing test first (red), smallest fix (green), refactor while green, full suite green before finishing; `0` omits the rule |
| `VIBE_HARNESS_MAX_CONCURRENT` | `3` | How many agent sessions may run at once; `0` = unlimited. A Run over the cap is queued (not failed) and starts automatically, oldest queue time first, when a session ends. The sidebar's **Settings** panel overrides this per harness (`settings.json`) and applies without a restart |
| `VIBE_HARNESS_TRANSCRIPTS_DB` | unset — one store per project | Where the compressed session transcripts live (SQLite). By default each project gets `<project>/.vibe-harness/transcripts.db` (kept out of that repo's git via `.git/info/exclude`), and a card with no project uses `<harness root>/.vibe-harness/transcripts.db`. Set this to force ONE explicit database for everything (tests, or a single shared store). Every finalized round's `export/session/` directory is stored there as one tar.gz blob and removed; `POST /api/transcripts/backfill` imports the directories written before the store existed, into each card's project store |
| `VIBE_HARNESS_PREVENT_SLEEP` | `1` | Hold an idle-sleep inhibitor (`caffeinate -i -s` on macOS, `systemd-inhibit` on Linux) while any session is live or any card is queued. A sleeping machine suspends the session process and the CLI reports `interrupted` (exit 3) — a card that looks failed but was only paused. `0` disables it; the sidebar's **Settings** panel has the same toggle. Closing the lid still sleeps the machine — only `sudo pmset -a disablesleep 1` overrides that, and the harness will not touch a system-wide root setting |

> **Reducing token usage:** see [`route.md`](route.md) — how work is routed
> through run → test gate → review → accept, which knobs cap spend
> (`--max-price`, `--time-limit`, tool allowlist, loop cap), how to route
> sessions to cheaper Vibe agents, and a copy-paste low-token preset.

## Per-role models

Each kind of session can run on its own model, chosen in the sidebar's
**Models** panel and stored in `models.json` at the harness root (`GET/PUT
/api/models`). The roles are the worker (`run-N`, including fixer rounds), the
reviewer (`review-N`) and the merge resolver (`resolve-N`).

**Choosing a model chooses the platform.** The picker lists both: the Vibe
aliases from `~/.vibe/config.toml` plus Claude's `sonnet` / `opus` / `haiku`,
and an "other…" entry for a full name like `claude-sonnet-4-6`. There is no
separate global Backend control — it was a second control for the same decision,
and the two could contradict each other (`claude` + a Vibe model cannot run).
`PUT /api/models` takes `{role, model, backend}` and refuses a backend that does
not offer the model (400). A backend whose CLI is not installed still lists its
models, disabled with the reason.

`models.json` is `{"models": {role: {"model": "...", "backend": "vibe"|"claude"}}}`.
A flat `{role: alias}` file (the old shape) still reads — every entry was
written for Vibe — and is rewritten in the new shape on the next save.

Resolution order: a role with its own choice uses its own model and backend; a
role on **inherit** follows the worker's choice; if the worker inherits too,
the fallback is **Vibe** with the CLI's own default model (`active_model`).

The backend decides how the choice is passed (`Backend.build_cmd(model=…)`):

| Backend | How the role's model reaches the CLI |
|---|---|
| `vibe` | `VIBE_ACTIVE_MODEL=<alias>` in the spawned session's environment. Vibe has no `--model` flag; its config layers put `VIBE_*` above `~/.vibe/config.toml`, so nothing global is rewritten and concurrent sessions cannot race each other's model. |
| `claude` | `--model <alias>` on the command line. |

The choice is passed for every spawn — worker, reviewer and resolver — so no
role silently runs on the backend's default. If the backend refuses the model
(Claude answers `not_found_error` and exits non-zero), the round fails with
`stop_reason: model_error` and a `model_rejected` failure whose detail names the
model; it never falls back to the default and looks successful. A refused
review model fails the card immediately rather than looping a fixer + review
round per step, and a refused resolver model is named in the failure detail
instead of a bare exit code.

The worker's backend is pinned per card run: a fixer round resumes the worker
session (`--resume <session-id>`), and a Vibe session id cannot be resumed by
Claude, so the resolved backend is recorded on the card's session
(`session.backend`) when the run starts and the fixer rounds use that. Changing
the worker's model mid-run therefore cannot switch backends under a resume; if
the new model belongs to the other platform, the resumed round runs on the
pinned backend's own default. The reviewer and the resolver are always fresh
sessions, so they may use a different backend than the worker.

The token counter follows the resolved backend: only Vibe streams live counts,
so it is hidden exactly when the worker's resolved backend is Claude.
`settings.json` keeps `backend` as the internal fallback (default `vibe`) for a
role with no choice at all; nothing in the UI writes it.
