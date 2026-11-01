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

> **Reducing token usage:** see [`route.md`](route.md) — how work is routed
> through run → test gate → review → accept, which knobs cap spend
> (`--max-price`, `--time-limit`, tool allowlist, loop cap), how to route
> sessions to cheaper Vibe agents, and a copy-paste low-token preset.
