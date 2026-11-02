# route.md — Routing guide: spend tokens where they matter

How vibe-harness routes work through the pipeline, and how to keep token usage
low. This is a Vibe-CLI-only tool: every session is a headless `vibe` run, and
the harness reads each run's `export.json` for token usage. There is no
per-session `--model` flag on the CLI — model routing happens through Vibe
agents (see §3).

## 0. Project folders: git init + agent.md + route.md

This file (in the harness root) is the **harness's** routing guide. Each
registered project gets its **own** `route.md`, which starts **empty** and is
maintained by the agents working in that project — the harness only creates it
if missing and never modifies an existing one.

Opening a new project (＋ Project → `POST /api/projects`) always sets up its
folder (`app/routemd.py::setup_project`):

- **`git init`** the folder (if it isn't a repo yet) with an initial commit that
  folds in `.gitignore`, `agent.md` and `route.md` — the work tree starts clean,
  so the first card run doesn't hit the dirty-tree guard. If the folder is an
  existing repo, the new files are committed immediately as their own commit.
- **`agent.md`** — a *managed* rules file for every vibe session working in the
  folder (marker comment on top). Its rules include: **always keep `route.md`
  up to date when a change is routing/token-relevant**, run the test suite
  before finishing, and record ambiguous choices in `NOTES.md`. The session
  prompts (implement / fixer / merge-resolver) explicitly tell the agent to read
  and follow `agent.md`. On every server start, managed copies are re-synced so
  rule updates propagate; remove the marker line in a project's `agent.md` to
  make it user-owned (never overwritten). The API response reports
  `agent_md: created|updated|unchanged|custom` and `route_md: created|exists`.
- **`route.md`** — created empty per project; agents fill it in per the
  agent.md rules.

Deleting a project only unregisters it — the files stay on disk. A setup error
for one project is reported in the response (`setup_error`) and never blocks
registration of the others.

## 1. The pipeline and where tokens go

```
you write a card (spec)
  → run-N      vibe session implements the spec in the work folder   ← big spend
               (run-1 spawns fresh; fixer rounds run-N>1 RESUME the same
                session via `vibe --resume <session_id>` — cached context,
                no workspace re-read)
  → test gate  VIBE_HARNESS_TEST_CMD runs in the workspace           ← free (no LLM)
  → review-N   FRESH vibe --agent ask process every round, but its   ← small spend
               prompt only carries the spec + `git diff
               <last-reviewed-commit>..HEAD` (round 1: from the
               MERGE-BASE of the card branch and its base, so a branch
               that is merely behind its base is not read as a revert)
               + the test output + the mechanical duplication report
               for the files the diff touches (`app/dupes.py`; the whole
               app when there is no diff), and it
               can ask the persistent worker via the per-card MCP mailbox
               (`harness-mailbox`: post_message / wait_for_message /
               submit_verdict — the verdict is read from submit_verdict,
               VERDICT: text is only a fallback)
  → (reject → fixer run-N+1 (resumed) … capped at VIBE_HARNESS_MAX_REVIEW_ROUNDS)
  → accept     test gate once more, then ship the card branch:       ← free
               merge it locally with --no-ff (accept_mode=local, the
               default: one merge commit per card, and the branch and
               its worktree are KEPT so the diff stays readable) or
               push it and open a PR with the verdict as the body
               (accept_mode=pr: `git push -u origin harness/<id>` +
               `gh pr create`; the branch and worktree are kept for
               follow-up commits, and nothing is merged locally)
               ONE accept per repository at a time: a second accept
               for the same repo is queued (accept_queued +
               queue_reason) and merged by the drain when the repo
               frees; different repos still accept concurrently.
  → accept all POST /api/cards/accept-all (the Done column's button)  ← free
               snapshots the cards in Done NOW and merges them one at
               a time in a background drain (they share one main
               worktree, so they cannot run concurrently); a conflicted
               accept hands to a resolver and the batch WAITS for it.
               A card that reaches Done mid-batch is not included — it
               renders below the button for the next click. The whole
               snapshot (read Done, skip queued/resolving, patch
               accept_queued, start the drain) is ONE critical section
               under `_accept_lock`, so two simultaneous clicks queue
               each card once: one call returns the ids, the other
               returns `{"queued": [], "running": true}`.
  → inspect    GET /api/cards/<id>/diff?range=card|round|worktree    ← free
               (the board's Diff panel: D over a card, or the Diff
                button) — no LLM tokens, it only reads git
  → revert     POST /api/cards/<id>/revert (or the MCP revert_card)  ← free
               `git revert` the card's merge commit in the project
               folder, then accepted -> draft with the session kept;
               a conflict aborts (409 + paths), no resolver is spawned
```

Tokens are spent almost entirely in **run** sessions; **review** sessions are
cheap (fresh read-only `ask` agent, diff-scoped prompt, no edits); the **test
gates and git steps cost no LLM tokens at all**. Resuming the worker session
across fix rounds is the main token saver: a fresh round no longer re-reads
the whole workspace (input tokens went ~200k → 2–3M per session before this).

## 2. Routing rules the harness applies automatically

- **A lost edit race is refused, not silently overwritten.** `PUT
  /api/cards/{id}` takes an optional `expected_updated_at`; when it is stale the
  save is a 409 and nothing is written (the editor keeps the user's text and
  offers reload-theirs / overwrite-anyway). Callers that send nothing — the MCP
  `update_card` tool, scripts — stay unconditional. This is a token guard as
  much as a data guard: a silently overwritten spec means the next paid run
  implements the wrong card.
- **Fail fast before review.** With `VIBE_HARNESS_TEST_CMD` set, a failing test
  suite rejects the round *before* any reviewer session is spawned — you never
  pay for a review of code that is already known to be broken. The fixer gets
  the test output as the rejection reason, so it fixes the right thing.
- **Workers are told to work test-driven.** Every worker prompt (run-1 and each
  fixer round) carries a TDD rule block: failing test first (red), smallest fix
  (green), refactor while green, full suite green before finishing. It costs a
  few dozen prompt tokens per round and pairs with the test gate above; disable
  with `VIBE_HARNESS_TDD=0`.
- **Growth is checked, not just correctness.** Every worker prompt also carries
  the reuse rule — *before adding a helper, search for an existing one
  (`grep -rn`); reuse or extend it; do not copy a block — extract it; do not add
  code nothing calls* — and every review prompt carries a **Duplication and
  redundancy** section: the mechanical clone report for the files the diff
  touches (`app/dupes.py`, blocks of ≥6 normalized lines appearing in two or
  more places) plus four criteria (re-implements something existing? copies a
  block? adds code nothing calls / a flag nothing reads / a one-caller
  abstraction / a duplicate file? proportionate to the spec?). The reviewer must
  REJECT when the diff increases the clone count without a stated justification,
  and name the duplicate. Prevention is cheaper than a rejection round: the rule
  costs ~40 prompt tokens, a rejection costs a whole fixer round. The same
  report is on the board at `GET /api/duplication` (`{count, clones}`), and each
  finalized run records `session.diff_stat` (`{files, added, removed}` from
  `git diff --stat <base>...<branch>`) so the card shows a `+412 −38` chip — a
  900-line card for a small spec is visible at a glance instead of only in
  review.
- **Dead code is a test failure, not a review finding.** `tests/test_no_dead_code.py`
  runs `ruff check app/` (the `dev` extra installs ruff; the test skips without
  it) with a deliberately minimal config — `F401` unused import, `F841` unused
  local — so leftovers fail the suite instead of costing a reviewer round to
  spot. It is not a style linter: nothing else is enabled.
- **Reviewer is always the cheap read-only agent.** The review pass always runs
  with `--agent <VIBE_HARNESS_REVIEW_AGENT>` (default `ask`) — never the
  configured worker agent — and the prompt forbids modifying files, so the
  reviewer makes no edit tool calls. The reviewer also runs the test suite and
  must REJECT on failure, which keeps broken code from reaching Done.
- **Worker persists across fix rounds; reviewer is fresh but diff-scoped.**
  run-1 spawns a fresh vibe session; every rejection spawns the fixer with
  `vibe --resume <session_id> --prompt-file <round>/prompt.md` (session id from
  the previous round's `export.json`, kept in `session.worker_session_id`), so
  the worker keeps its cached context. Every review round is a NEW process, but
  its prompt only carries the spec, the test-suite output, and
  `git diff <last-reviewed-commit>..HEAD` in the card's worktree — small context,
  independent judgment, no anchoring. Disable with `VIBE_HARNESS_WORKER_RESUME=0`
  / `VIBE_HARNESS_REVIEW_DIFF=0`.
- **Per-card MCP mailbox.** Each run attaches `app/mailbox_server.py` (tiny stdio
  MCP server) to the card's worktree via `.vibe/config.toml` (git-excluded there);
  state lives in `sessions/<id>/mailbox/`. The fresh reviewer can
  `post_message`/`wait_for_message` with the persistent worker mid-review, and
  its verdict arrives as the structured `submit_verdict` call. Verdicts are
  scoped per review round: every session gets `VIBE_HARNESS_MAILBOX_ROUND=<kind>-<n>`,
  `submit_verdict` is reviewer-role-only and writes `verdicts/<round>.json`
  (mirrored to the top-level `verdict.json` as "latest"); the harness clears the
  round's file before spawning the review and reads only that file afterwards —
  a round that never calls `submit_verdict` falls back to `VERDICT:` text parsing
  (`via="log"`) instead of inheriting the previous round's verdict. The harness
  only supervises: round cap, accept (merge or PR), token accounting, mailbox
  lifecycle.
  Disable with `VIBE_HARNESS_MAILBOX=0`.
- **Accept mode decides where the change lands, not what it costs.**
  `VIBE_HARNESS_ACCEPT_MODE` (or the sidebar's **Accept mode** control,
  `PUT /api/settings {"accept_mode": …}`) picks how an accepted card ships:
  `local` (default) merges the card branch into the base branch in the project
  folder; `pr` pushes `harness/<id>` to `origin` and opens a pull request whose
  body carries the audit trail (spec, review verdict, test output tail, diff
  stat). Both run the same accept-time test gate and cost no LLM tokens; `pr`
  additionally needs `gh`, `gh auth login` and an `origin` remote, and fails
  with a 409 naming the missing piece before anything is pushed. In `pr` mode
  the branch and its worktree are kept (the PR may need follow-up commits) —
  including across restarts: startup pruning skips PR-accepted cards, because
  the `-u` push makes `git branch -d` consider the branch merged and would
  otherwise delete it. A repo therefore accumulates one worktree per
  accepted-but-unmerged card until the PR lands.
- **One accept per repository at a time.** Every accept merges in the repo's
  MAIN worktree (the project folder), and git allows exactly one merge in
  progress there, so `SessionRunner` holds a per-repo accept lock
  (`_accepting_repos`). A second accept for the same repo neither merges nor
  fails: the card is queued (`session.accept_queued` + `queue_reason` =
  "waiting for the merge in <repo> to finish") and the existing accept drain
  merges it as soon as the repo frees — no second click. Accepts for
  *different* repos still run concurrently. The repo stays locked while a
  conflict resolver owns the merge and is released when the resolver
  concludes or fails. The resolver's commit is only recorded as the card's
  `merge_commit` when the card branch is an ancestor of HEAD
  (`git merge-base --is-ancestor`); otherwise the card stays done with a
  `merge_error` instead of being accepted with another card's commit. A
  failed resolver drops the card out of the accept queue (no automatic
  retry — that would spawn resolver after resolver); a card accepted by its
  resolver is cleared of `accept_queued` / `queue_reason`.
- **Loop cap.** Rejections (review or test) loop through a fixer, capped at
  `VIBE_HARNESS_MAX_REVIEW_ROUNDS` (default 5). After the cap the card lands in
  Done unapproved (`review.exhausted`) and *you* decide — no more paid rounds.
- **Dependency links order the pipeline.** A card with `depends_on` (drag a
  draft card onto another card, or `harness_add_dependency`) does not start while
  any dependency is unfinished: `run` marks it waiting (`session.waiting_for`)
  instead of spawning a session, and `_drain_dependents` starts it automatically
  once every dependency is **merged** (`accepted`). Reviewer approval alone
  (`done`) does not unblock: the dependent builds on the merged result, not on a
  branch that may still be rejected or reverted. failed/stopped do not unblock
  either.
  So a chain of linked drafts costs the same tokens as running them by hand, in
  order, with no idle time between them — but do not link cards that should run
  independently, they would serialize.
- **The machine is kept awake while there is work.** While any session is
  live or any card is queued (a slot wait, an accept batch, a resolver),
  the server holds an idle-sleep inhibitor — `caffeinate -i -s` on macOS,
  `systemd-inhibit` on Linux — and releases it when the work is done.
  Without it a sleeping machine suspends the session process, the CLI
  gives up and reports `interrupted` (exit 3), which the board shows as a
  failed card even though nothing was wrong with the work. The inhibitor
  is a child process, so it dies with the server. `VIBE_HARNESS_PREVENT_SLEEP=0`
  (or the sidebar Settings toggle) turns it off. A card merely waiting on
  an unmerged dependency does NOT count as work — nothing would happen
  until the user merges it, so it would keep the machine awake forever.
  Closing the lid still sleeps the machine; only `sudo pmset -a
  disablesleep 1` overrides that. A session that was signalled from
  outside is classified `interrupted` (not `crashed`) so the board says
  "re-run it" instead of pretending the work failed.
- **Concurrency cap: at most N sessions at once.** `VIBE_HARNESS_MAX_CONCURRENT`
  (default 3, `0` = unlimited) or the sidebar's **Settings** panel
  (`settings.json`, read per run — no restart). A Run that finds no free slot is
  *queued*, not failed: the card stays in Draft with
  `session.queued`/`queue_reason` ("waiting for a free slot (2/3 running)") and
  `_drain_queued_runs` starts it, FIFO by `session.queued_at`, as soon as a
  session ends (end of every session, after Stop, after a resolver concludes,
  and at startup). Drains are serialized and the cap check plus the slot
  registration are atomic, so two sessions ending together cannot start the
  same card twice or push the board past the cap, and a session's slot is
  always released. One card holds one slot at a time — run → review → fixer are
  sequential — so the cap bounds concurrent sessions, worktrees and test suites,
  and therefore the peak spend rate. It does not reduce the tokens a single card
  costs; it stops ten Run clicks from starting ten sessions at once.
- **Hard guardrails on every session** (passed to `vibe`):
  - `VIBE_HARNESS_MAX_PRICE` → `--max-price <usd>`: absolute per-session cost
    cap; the session is interrupted when exceeded.
  - `VIBE_HARNESS_TIME_LIMIT` → `--time-limit <s>` (default 1800): wall-clock
    cap; vibe writes the export and exits.
  - `VIBE_HARNESS_ENABLED_TOOLS` → repeated `--enabled-tools`: in headless mode
    this *disables all other tools*, which removes whole tool schemas from the
    prompt and cuts tool-call tokens. Allowlist only what the task needs
    (e.g. `read,write,edit,bash`).
- **Small workspaces.** Git-backed runs happen in a per-card linked worktree
  (`sessions/<id>/worktree`, a full checkout of the card's project repo on the
  card's own branch — the project folder itself stays on its base branch), not
  in a giant monorepo root; project-less cards run in an isolated
  `sessions/<id>/workspace`. Smaller working directory + smaller allowlist =
  smaller prompts.

## 3. Model routing (Vibe agents, not CLI flags)

The `vibe` CLI has no `--model` flag; the model comes from Vibe's config
(`~/.vibe/config.toml` → `active_model`, currently
`deepseek-v4-1-flash-1017` = Mistral-hosted DeepSeek V4.1 Flash 1017 on the
`mistral` provider, with `mistral-large-4-0-3` / `zai-glm-5-3-flash` /
`glm-5-3` also registered in `models`) or from a custom agent TOML in
`~/.vibe/agents/<name>.toml`, which can pin a cheaper model. Card: a
Mistral-hosted third-party model needs a full `name` + `provider = "mistral"`
entry — an alias-only entry is dropped unless the server's routed-model
registry knows it.

**Give every hand-written model entry its real token window.** Vibe only knows
the window of models in its built-in table (`mistral-large-4`, `zai-glm-5-3`,
Claude, GPT…); for anything else it assumes 262,144 tokens and warns
`window_guessed`. The DeepSeek entry therefore carries
`max_context_length = 1048576` and `max_output_tokens = 131072` (the model
card's 1M context / 128k output).

**The window alone does not raise the cap you see.** Vibe's status bar shows
context use against the *compaction threshold*, not the window, and the default
rule caps that at `min(256_000, window - 60_000)` — so a 1M model still
displayed and compacted at 256k. The DeepSeek entry sets
`auto_compact_threshold = 800000` explicitly, matching the threshold Mistral's
own routed models ship (800k on a 1M window, ~76%). Check a new entry with
`vibe -p ok --output-dir DIR` and read `config.models.<alias>` in
`export.json`; a running TUI keeps the value it started with, so restart it
(or `/reload`) after editing.

**Name the entry after the model.** The status bar and the model picker show
`display_name`, falling back to the alias, so an entry aliased `deepseek` reads
`deepseek[max]`. The DeepSeek entry uses the full model id as its alias plus
`display_name = "DeepSeek V4.1 Flash 1017"`; `active_model` must then name that
alias.

Route sessions to agents via the harness env vars:

| Route | Env var | Default | Tip |
|---|---|---|---|
| Worker (implement/fix) | `VIBE_HARNESS_AGENT` | Vibe's `default_agent` | Use a custom agent pinned to a cheaper model for small/mechanical cards |
| Reviewer | `VIBE_HARNESS_REVIEW_AGENT` | `ask` | Keep it read-only; a small model is fine — it only inspects and answers a verdict |

Example `~/.vibe/agents/cheap.toml` pinned to a small model, then
`VIBE_HARNESS_AGENT=cheap` for routine cards and the large model for hard ones.

### Per-role models (the sidebar's Models panel)

Each kind of session can run on its own model, chosen in the board's sidebar
(🧠 Models) and stored in `models.json` at the harness root:

| Role | Session | What it does |
|---|---|---|
| Worker | `run-N` | Implements the card and its fixer rounds |
| Review | `review-N` | Reviews the diff and the test output |
| Merge conflict | `resolve-N` | Resolves a conflicted merge on accept |
| Orchestrator | — | No session yet: the worker splits a spec into sub-cards itself |

A role left on **inherit** runs on the user's own `active_model`. A chosen
model is pinned for that one spawned session with `VIBE_ACTIVE_MODEL` — Vibe's
config layers put `VIBE_*` env vars *above* `~/.vibe/config.toml`, so the
global config is never rewritten and two concurrent sessions cannot race each
other's model. `GET/PUT /api/models` is the API; `app/models.py` owns the
store. The orchestrator role is offered but disabled: nothing would use it.

## 4. Write cards so the first run succeeds

The cheapest token is the one never spent:

- One card = one small, unambiguous task. Split big specs into separate cards
  (they run on separate branches and merge independently).
- Be concrete: name files, expected behavior, and the test command to satisfy.
  Ambiguity burns fixer rounds.
- Set `VIBE_HARNESS_TEST_CMD` (e.g. `pytest -q`, `npm test`) so the test gate
  catches breakage before the reviewer — and so accept is ship-only-green.
- On a shared repo, set `VIBE_HARNESS_ACCEPT_MODE=pr`: the review verdict rides
  along as the PR body, so a human reviewer reads the audit trail instead of
  paying for another agent pass over the same diff.
- Don't re-run a Done card casually; each re-run clears the session and starts
  a fresh paid run. Edit → run only when the spec actually changed.
- Use the card `template` field for recurring card shapes so the spec is
  already structured.

## 5. Measure what you spend

- **One definition of a card's cost** (`app/tokens.py::card_tokens`): the
  worker's `export.json` total (`session.tokens` — already cumulative across
  resumed fixer rounds, so it is added once, never per round) **plus** every
  review round (`session.reviews[]`, falling back to `session.review` for cards
  written before the list existed) **plus** every resolver round
  (`session.resolver`). The per-card chip, `/api/stats` and
  `/api/split-report` all read this one number, so they cannot disagree.
- **Stats bar** (`GET /api/stats`, optionally `?project=<name>`): counts by
  status, Σ card totals, Σ runtime — scope it per project to compare. The
  total is exactly the sum of the per-card chips. Hovering the **Tokens**
  counter shows the token-type breakdown (`token_breakdown`: cached · output ·
  input, summed over the visible cards; a type nobody reported stays hidden).
- **Per-card chips:** each card shows its total cost (worker + review +
  resolver rounds) and model; hovering a token chip shows the matching type
  breakdown (`session.card_token_breakdown`, or `session.live_token_breakdown`
  while live). A git-backed card also shows its **growth**: `session.diff_stat`
  (`{files, added, removed}`, from `git diff --stat <base>...<branch>`, recorded
  when a run finalizes) renders as a `+412 −38` chip with the file count on
  hover. It costs nothing — one git call per finalized run, no LLM tokens — and
  it is the cheapest way to spot a card that spent 900 lines on a 50-line spec.
  `GET /api/duplication` is the matching project-wide number: `{count, clones}`
  from the same detector the reviewer reads.
- **Live while running:** while a session is live, the card chip and the
  stats-bar total update in place (`session.live_tokens` on the card,
  `live_tokens` in `/api/stats`; the top-bar pill pulses while > 0). The
  counter only ever climbs: a resumed round's journal is cumulative, and the
  last live value is kept as `session.live_high_water` when a round ends, so
  the number does not dip between rounds (review → fixer included). Source: the
  spawned vibe's own session journal — `$VIBE_HOME/logs/session/unified/<id>/journal/*.jsonl`
  carries the cumulative `contextUsage` (`totalTokens` plus `inputTokens` /
  `outputTokens` / `cachedInputTokens`, which feed the live hover breakdown),
  and `logs/session/active/<id>.lock.json` maps the spawned pid to the session
  (`app/liveusage.py`). Cost: incremental tail reads only, ≤ 1 scan per 2 s
  per live card, zero I/O when nothing is running, no extra requests from the
  browser (it rides the existing 2 s poll) and no LLM spend. Best effort: if
  the journal is missing or changes schema, the card falls back to the last
  recorded `export.json` count.
- **Watching a live session costs nothing.** Hold **C** and click a card to
  open the activity popup: it polls `GET /api/cards/<id>/activity?cursor=<n>`
  once a second while the card is running/reviewing, and that endpoint only
  reads the same vibe journal the live token counter reads
  (`app/activity.py`, cursor = journal lines already consumed, ≤ 200 events and
  2000 chars per event per call). No LLM call, no session spawned, no extra
  request when no popup is open — the popup is the only caller. It replaced the
  chat-mode toggle, whose 💬 panel opened the session LOG (the CLI's final
  output, empty until the run ends) and therefore answered nothing while a
  session was live.
- **Per-round artifacts:** `sessions/<id>/run-N/export/export.json`,
  `review-N/export/export.json` and `resolve-N/export/export.json` hold
  `usage.total_tokens` (plus
  `input_tokens` / `output_tokens` / `cached_input_tokens` when the model
  reports them), the model, and the stop reason — diff these across
  cards/agents to find your expensive routes.
- **Per-round verdicts:** `session.reviews` keeps every round's record
  (`{approved, reason, round, tokens, model, log, export_dir, via, commit}`);
  `session.review` is the last one. The Diff panel's `round` range reads them,
  so you can see what each review round actually judged — and what it cost —
  without re-running anything. Round 1 has no previous round, so it starts at
  the merge-base of the card branch and its base (never the base tip, which
  would show the base's newer commits as deletions).
- USD cost is intentionally not shown in the UI, but `--max-price` still caps
  real spend per session.
- **Search costs nothing.** The top bar's search box filters the board in the
  browser (the cards, bodies included, are already in `state.cards`) and asks
  `GET /api/search?q=` for the archive hits — a linear scan over the board's
  cards and every archived card (`app/search.py`), capped at 50 results, with
  no index and no LLM call. Typing therefore spends zero tokens: the only cost
  is one small HTTP request per debounced keystroke, and no session is spawned.

### Why a card failed — read the badge, not the log

Reading a session log to work out *why* a card failed costs tokens and time.
The harness answers it for free: `app/failures.py::classify(card)` turns the
session record into `{kind, detail}`, `annotate_card` attaches it as
`session.failure` (so the API and the MCP both carry it), the card shows the
kind as a badge with the detail on hover, and the top bar's **Failures** filter
narrows the board to one kind.

**One source of truth for the kinds.** `failures.KINDS` is the contract, and
`GET /api/settings` publishes it as `failure_kinds`; `app/static/app.js` builds
the filter from that response (falling back to its label table for the first
paint, before the fetch resolves). Adding a kind to the classifier therefore
reaches the UI with no JS edit — the filter used to keep its own copy and
silently drift.

Kinds, in priority order — each one points at a different knob:

| kind | what it means | what to change |
|---|---|---|
| `stopped_by_user` | you pressed Stop | nothing — it was you |
| `interrupted` | the session was signalled from outside (a sleeping machine suspends it) | re-run — the work was never judged |
| `git_conflict` | the accept merge conflicted / a resolver was involved | resolve the conflict, then Accept again |
| `timeout` | the session hit its limit (`stop_reason`, or the harness watchdog) | raise `VIBE_HARNESS_TIME_LIMIT`, or split the card |
| `tests_failed` | the test gate rejected it | fix the spec or `VIBE_HARNESS_TEST_CMD`; the fixer already got the output |
| `reviewer_rejected` | the review loop ran out with a rejection | tighten the spec — the reviewer's reason is in `review.reason` |
| `no_verdict` | the reviewer never answered | re-run; check the review log if it repeats |
| `crashed` | non-zero exit (or a failed commit) with no other explanation | this is the one worth reading the log for |

A card that did not fail has no `session.failure` at all.

## 6. Low-token preset (copy-paste)

```bash
export VIBE_HARNESS_MAX_PRICE=1.00          # hard per-session cost cap
export VIBE_HARNESS_TIME_LIMIT=600          # 10 min per session
export VIBE_HARNESS_MAX_REVIEW_ROUNDS=3     # stop paying for fixer loops sooner
export VIBE_HARNESS_TEST_CMD="pytest -q"    # fail fast, review only green code
export VIBE_HARNESS_TEST_TIMEOUT=300
export VIBE_HARNESS_ENABLED_TOOLS="read,write,edit,bash,grep,glob"
export VIBE_HARNESS_REVIEW_AGENT=ask        # read-only reviewer (default)
export VIBE_HARNESS_WORKER_RESUME=1        # resume the worker across fix rounds (default)
export VIBE_HARNESS_REVIEW_DIFF=1          # reviewer sees only diff-since-last-review + tests (default)
export VIBE_HARNESS_MAILBOX=1              # per-card MCP mailbox for worker<->reviewer (default)
# export VIBE_HARNESS_AGENT=cheap           # custom agent pinned to a small model
```

## 7. The harness MCP (models talking back to the board)

Every **run** session (implement/fix — not review, not the merge resolver)
gets a `harness` MCP server injected through vibe's `VIBE_MCP_SERVERS` env var
(`app/runner.py::SessionRunner._session_env`), so no global vibe config is
touched. The server (`app/mcp_server.py`, stdlib-only stdio JSON-RPC) forwards
to the dashboard HTTP API — the dashboard stays the single writer of cards
and sessions. Tools are published as `harness_<tool>` in the `mcp_harness`
group: `current_card`, `list_cards`, `get_card`, `create_card`, `update_card`,
`delete_card`, `run_card`, `stop_card`, `accept_card`,
`accept_all_cards`, `revert_card`,
`card_log`,
`list_templates`, `stats`, a **read-only** `list_projects`, `ask_user`, the
dependency-link tools `add_dependency` / `remove_dependency` /
`list_dependencies` (chain cards so one runs automatically once another is
merged — accepted; the dashboard draws a red string between the
linked cards, and dragging a draft card onto another card creates the same
link), the **archive** tools `create_archive` / `list_archives` /
`get_archive` / `archive_card` / `archive_cards` / `archive_stack` /
`restore_card` (see below),
and the **stack** tools `list_stacks` / `create_stack` / `update_stack` /
`delete_stack` / `stack_cards` / `unstack_card` (see below).

- **Stacks (piles of cards).** A stack is a named pile of cards that sit in one
  board column, with an optional header. Stacks are **server state**
  (`stacks.json`, `app/stacks.py`, `/api/stacks`), not browser state: the board
  and the MCP tools must see the same piles, and an LLM cannot reach a
  browser's localStorage. Models can build a pile (`create_stack` with a
  `title`), rename it (`update_stack`), move cards in (`stack_cards`, which
  detaches them from whatever pile they were in) and out (`unstack_card`), and
  dissolve it (`delete_stack` — the cards stay on the board). A pile needs at
  least two cards and only holds cards from one column (400 otherwise); a pile
  that falls below two cards is dissolved. Stacking costs no tokens (pure
  state edits).

- **Archives (cards & decks).** The archive is a folder structure:
  `archives/<slug>-<id>/{archive.md, cards/}` per archive, each with a
  free-form type label (`cards`/`deck`/`project`/…). Archives created before
  the notes → cards rename keep their items in `notes/` instead; every read
  (`list`/`get`/`restore`/move) looks in both folders, new items always land
  in `cards/`. Models can create typed
  archives and move **accepted cards** into them (`archive_card`, or
  `archive_cards` for a batch) and back (`restore_card`). `archive_stack`
  retires a whole deck in one call: its cards move, the deck is recorded
  in the manifest (`archived_stacks`: title + card order) and dissolved,
  and without an `archive_id` the archive is created (or reused) under
  the deck's title — one archive per retired deck, which is the board's
  structure. Archiving a whole *project* deck
  (`POST /api/archives/{id}/projects/{name}`) stays a user-side action —
  there is deliberately no MCP tool for it, same as no project
  create/delete/switch. Archiving costs no tokens (pure file moves) and keeps
  the card's full history; archived cards leave the board. `GET
  /api/archives/{id}` also groups the archive's cards under project headers
  (`projects`: one group per card `project`, unfiled cards last under `name:
  null`, decks archived wholesale keep a header even with no cards left) —
  the sidebar's archive tree and the Archives modal render that as one more
  level, so an archive reads as its projects.

- **`ask_user` (model → user, blocking).** When the spec is genuinely
  ambiguous, the model calls `harness_ask_user(question, options?,
  timeout_seconds?)`: the question is posted to the card
  (`sessions/<id>/questions.json` via the dashboard API) and the tool call
  long-polls until the user answers on the card, dismisses it, or the timeout
  hits (default `VIBE_HARNESS_ASK_USER_TIMEOUT` = 600 s, capped at 3600 s).
  The result carries the answer, or `timeout`/`cancelled` (then the model
  makes a reasonable choice and records it in NOTES.md). While blocked, the
  session spends no LLM tokens — the wait is a plain HTTP long-poll
  (`GET /api/cards/<id>/questions/<qid>/wait`), so asking is cheap; the cost
  risk is only the *pause*, bounded by the timeout. Pending questions are
  cancelled when the session ends (runner `_finalize`/`recover_stale`), and
  the run/fix prompts tell the model to use it sparingly (minor ambiguity →
  pick a default + NOTES.md).

- **Split big specs:** the run/fix prompts tell the model to *first* judge
  whether the spec is too big for one session and, if so, create one card per
  subtask via `harness_create_card` — drafts by default, `auto_run=true` to
  run immediately, `harness_run_card` later. This is the "mission
  decomposition" idea from §7 below, now implemented at the model's
  discretion: **watch token spend on cards that spawn sub-cards** — each
  sub-card run is another paid session (capped per card by
  `--max-price`/`--time-limit` and the review-loop cap).
- **No project swapping:** the MCP has no create/delete/switch-project tool,
  and `create_card`/`update_card` reject `project`/`workdir` args — sub-cards
  inherit the current card's project/workdir (the MCP server learns the
  current card from `VIBE_HARNESS_NOTE_ID` in its env, falling back to the
  `sessions/<id>/…` workdir path).
- **Allowlists:** when `VIBE_HARNESS_ENABLED_TOOLS` is set, the runner appends
  the `harness_*` glob so the MCP tools survive the allowlist
  (`app/runner.py::_build_cmd`).
- **Knobs:** `VIBE_HARNESS_MCP_ENABLED=0` disables the injection entirely;
  `VIBE_HARNESS_MCP_SERVER_NAME` renames the alias (tools become
  `<name>_<tool>`).

## 8. Future routing ideas (not yet implemented)

- Per-card agent/model choice in the editor.
- `--max-turns` (supported by `vibe` headless mode, not yet passed by the
  harness) as an extra per-session turn cap.
- Mission decomposition: one orchestrator card spawning sub-sessions per
  subtask. *(Partially landed: the harness MCP lets a run split its spec into
  sub-cards, and those sub-cards are now CHILDREN of the card that created them
  — `parent_id`, shown on the board as `⤷ N sub-cards` / `↳ <parent>` chips.
  There is still no orchestrator session: the worker decomposes itself. The
  sidebar's ⤷ Split panel reports tokens per accepted outcome for split vs
  unsplit cards, which is the measurement that decides whether a coordinator
  session is worth building.)*

### Why there is no orchestrator session yet

Planning up front is the cheap, easy part and the most likely to be wrong: a
planner writes the plan before any code exists, from the spec alone, and the
plan is stale by the third sub-task. The value of decomposition is **context
isolation** (five focused sessions beat one 3M-token session), not the plan
document. So the order is: make decomposition *visible* (done — `parent_id` +
the tree chips), *measure* it (done — `/api/split-report`), and only then build
a coordinator. When it comes, it should be a small state machine with no
write access (it routes work, it does not do it), woken by child-completion
events rather than polling, with a token budget for the whole subtree.
