#!/usr/bin/env python3
"""vibe-harness MCP server (stdio transport).

Lets the models that work on cards talk to this harness: the same board the
user sees. The harness injects this server into every vibe session it spawns
(via the ``VIBE_MCP_SERVERS`` env var, see ``app/runner.py``), so inside a run
the model sees an ``mcp_harness`` tool group with ``harness_*`` tools.

The server is a thin, stdlib-only JSON-RPC 2.0 loop over stdin/stdout (MCP
stdio transport, newline-delimited messages). It forwards tool calls to the
running dashboard's HTTP API (``VIBE_HARNESS_URL``, default
``http://127.0.0.1:8734``) so the dashboard stays the single writer of cards
and sessions.

Tool surface mirrors what the user can do on the dashboard — list/get/create/
update/delete cards, run/stop/accept/revert, dependency links (add/remove/list),
logs, templates, stats, read-only project list, and archives
(create/list/get archives, move cards into/out of them) — with one deliberate
exception: there is NO tool to create, delete, or switch projects (archiving
a whole project deck stays a user-side action). Cards created through this MCP
inherit the calling card's project/workdir and cannot be pointed elsewhere.

``ask_user`` is the one tool that talks to the *user* instead of the board: it
posts a question on the current card and blocks until the user answers
on the card (or cancels / the timeout hits), returning the answer to the model.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PROTOCOL_VERSION = "2026-07-28"
SERVER_NAME = "vibe-harness"
SERVER_VERSION = "0.1.0"
DEFAULT_URL = "http://127.0.0.1:8734"

# ask_user defaults/caps (the runner injects VIBE_HARNESS_ASK_USER_TIMEOUT into
# this server's env; 600 s fallback when run outside a harness session).
DEFAULT_ASK_TIMEOUT_SECONDS = 600.0
MAX_ASK_TIMEOUT_SECONDS = 3600.0
# Server-side long-poll chunk: each wait request blocks at most this long, the
# ask_user loop re-issues until the total timeout is spent (keeps proxies and
# the HTTP client timeout comfortably above the server wait).
ASK_WAIT_CHUNK_SECONDS = 30.0

# get_transcript caps (mirror app.activity.DEFAULT_LIMIT / MAX_LIMIT: the
# API rejects a larger limit, so the tool clamps instead of erroring).
DEFAULT_TRANSCRIPT_EVENTS = 200
MAX_TRANSCRIPT_EVENTS = 500

# Harness root = parent of app/; used to derive the current card id from the
# session workdir (sessions/<id>/worktree or sessions/<id>/workspace) when the
# VIBE_HARNESS_NOTE_ID env var is not set.
HARNESS_ROOT = Path(__file__).resolve().parent.parent

STATUSES = ("draft", "running", "reviewing", "done", "failed", "stopped", "accepted")

# Args that would let a model aim a card at another folder. They are rejected
# by create_card/update_card: new cards inherit the current card's project.
FORBIDDEN_NOTE_ARGS = ("project", "workdir")


class HarnessError(Exception):
    """A harness API call failed; the message is shown to the model."""


def harness_url() -> str:
    return (os.environ.get("VIBE_HARNESS_URL") or DEFAULT_URL).rstrip("/")


def current_card_id() -> str | None:
    """The card this vibe session is working on, if any.

    Primary source: VIBE_HARNESS_NOTE_ID (baked into the MCP server's env by
    the runner). Fallback: the MCP server's cwd is the session workdir, whose
    path is <harness root>/sessions/<card id>/(worktree|workspace).
    """
    card_id = os.environ.get("VIBE_HARNESS_NOTE_ID")
    if card_id:
        return card_id
    try:
        rel = Path.cwd().resolve().relative_to(HARNESS_ROOT / "sessions")
    except ValueError:
        return None
    return rel.parts[0] if rel.parts else None


class HarnessAPI:
    """Minimal JSON-over-HTTP client for the dashboard API."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict | None = None,
                timeout: float | None = None) -> object:
        url = self.base_url + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise HarnessError(f"harness API {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise HarnessError(
                f"harness dashboard not reachable at {self.base_url} ({exc.reason}) — "
                "is the dashboard (uvicorn) running?") from exc
        if not body:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return body

    def get(self, path: str, timeout: float | None = None) -> object:
        return self.request("GET", path, timeout=timeout)

    def post(self, path: str, payload: dict | None = None) -> object:
        return self.request("POST", path, payload)

    def put(self, path: str, payload: dict) -> object:
        return self.request("PUT", path, payload)

    def delete(self, path: str) -> object:
        return self.request("DELETE", path)


# --------------------------------------------------------------------------
# tool definitions
# --------------------------------------------------------------------------

_COMMON = (
    "Talks to the vibe-harness dashboard (the post-it board this session runs "
    "on). By design there is NO tool here to create, delete, or switch "
    "projects: cards created via this MCP inherit the current card's project."
)

TOOLS: list[dict] = [
    {
        "name": "current_card",
        "description": (
            "Context about the card this session is implementing (id, title, "
            "status, project, workdir), or null when not running inside a "
            "harness card session. Call this first to ground yourself. " + _COMMON),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_cards",
        "description": "List cards on the board, newest first. Optional filters: "
            "status, project. Each card includes depends_on (ids of cards it runs "
            "after) and, when it failed, session.failure ({kind, detail}) — why it "
            "failed, in one line. " + _COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": list(STATUSES)},
                "project": {"type": "string", "description": "Project name to filter by"},
            },
        },
    },
    {
        "name": "get_card",
        "description": "Get one card (full body, session record, review, git info, "
            "and session.failure when it failed) by id. " + _COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "create_card",
        "description": (
            "Create a new card on the board. It starts as a draft; set "
            "auto_run=true to run it immediately, or leave it in draft and run "
            "it later with run_card. When the task at hand is too big for one "
            "session, split it: create one card per subtask here. The new card "
            "inherits the current card's project/workdir — the project and "
            "workdir arguments are intentionally not accepted (no project "
            "swapping through the MCP). " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 1},
                "body": {"type": "string", "description": "The spec (markdown) for the card"},
                "template": {"type": "string", "description": "Optional card template name (see list_templates)"},
                "auto_run": {"type": "boolean", "description": "Run the new card immediately (default false = leave as draft)"},
                "auto_accept": {"type": "boolean", "description": "Auto-accept after an approved review (default false)"},
            },
            "required": ["title", "body"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_card",
        "description": (
            "Update a card's title/body/auto_run/auto_accept. Editing a "
            "finished card resets it to draft. project/workdir are "
            "intentionally not accepted (no project swapping through the "
            "MCP). " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "auto_run": {"type": "boolean"},
                "auto_accept": {"type": "boolean"},
            },
            "required": ["card_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "delete_card",
        "description": "Delete a card (and its session dir / linked worktree). 409 while it is running. " + _COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "run_card",
        "description": (
            "Run a card now (e.g. a draft you created earlier — the harness "
            "spawns a headless vibe session for it). 409 while already "
            "running. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "stop_card",
        "description": "Stop a card's live session (SIGTERM, escalating to SIGKILL). " + _COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "accept_card",
        "description": (
            "Accept a done card (test gate, then ship it: in accept_mode "
            "'local' the git branch is merged into the base branch; in 'pr' "
            "mode the branch is pushed and a pull request is opened with the "
            "review verdict as its body). Only possible with an approved "
            "review (or an exhausted review loop). Only one accept merges "
            "into a repository at a time: a second accept for the same repo "
            "is queued (session.accept_queued + queue_reason) and merged "
            "automatically when the repo frees. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "accept_all_cards",
        "description": (
            "Accept every card currently in Done, merging them one at a time "
            "(the batch is a snapshot: cards that reach Done later are not "
            "included). Returns the queued card ids. " + _COMMON),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "revert_card",
        "description": (
            "Undo an accepted card: `git revert` the merge commit its accept "
            "recorded, then reopen the card as a draft (its session history — "
            "tokens, logs, reviews — is kept). Needs the card's recorded merge "
            "commit: a card accepted before merge commits were recorded "
            "returns 409. A conflicting revert is aborted and returns 409 "
            "with the conflicting paths; the repository is left untouched. "
            + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "card_log",
        "description": "Tail of a card's current session log (prompt output of the running/last session). " + _COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "tail": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 200},
            },
            "required": ["card_id"],
        },
    },
    {
        "name": "list_templates",
        "description": "List the card templates available for create_card. " + _COMMON,
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_models",
        "description": (
            "List the models a card can be put on (create_card/update_card take "
            "a `model` alias), plus which model each role currently uses. A "
            "card's own model wins for its own sessions; a card without one "
            "inherits the worker role's model. " + _COMMON),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_transcripts",
        "description": (
            "List a card's stored session transcripts: one metadata row per "
            "round (round, session_id, created_at, raw_bytes, gz_bytes). The "
            "transcripts live compressed in the card's project store "
            "(`.vibe-harness/transcripts.db`); read "
            "one with get_transcript. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "get_transcript",
        "description": (
            "Read one round's session transcript as text: the parsed journal "
            "events (assistant text, reasoning, tool calls and their results), "
            "capped at `limit` events (default 200, max 500). This is the "
            "debugging surface for \"what did the model actually do in that "
            "round\" — the raw tar.gz blob is not readable by a model. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "round": {"type": "string",
                          "description": "run-1, review-2, resolve-1, ..."},
                "limit": {"type": "integer", "minimum": 1,
                          "maximum": MAX_TRANSCRIPT_EVENTS,
                          "description": "Max events to return (default 200)"},
            },
            "required": ["card_id", "round"],
        },
    },
    {
        "name": "stats",
        "description": "Board stats: counts by status, total tokens, total/avg runtime; optionally scoped to one project. " + _COMMON,
        "inputSchema": {
            "type": "object",
            "properties": {"project": {"type": "string"}},
        },
    },
    {
        "name": "list_projects",
        "description": (
            "List registered projects (name + path) — read-only. This MCP "
            "intentionally has no tool to create, delete, or switch projects."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_dependency",
        "description": (
            "Link two cards: card_id will be RUN AUTOMATICALLY once depends_on "
            "completes (status done or accepted) — the board draws a red string "
            "between the two cards. If depends_on has already completed, card_id "
            "starts running immediately; otherwise it waits (session.waiting_for) "
            "and starts as soon as every dependency completes. Use this to chain "
            "sub-cards: create the follow-up as a draft, then add_dependency "
            "card_id=follow_up, depends_on=this card. 409 on a self-link, a "
            "dependency cycle, an unknown card, or a card with a live session. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string",
                            "description": "The dependent card — the one that runs after"},
                "depends_on": {"type": "string",
                               "description": "The card it depends on — the one whose completion triggers the run"},
            },
            "required": ["card_id", "depends_on"],
            "additionalProperties": False,
        },
    },
    {
        "name": "remove_dependency",
        "description": (
            "Remove a dependency link (card_id no longer waits for depends_on). "
            "If that was the last unmet dependency of a draft, it starts running "
            "immediately. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string"},
                "depends_on": {"type": "string"},
            },
            "required": ["card_id", "depends_on"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_dependencies",
        "description": (
            "Show dependency links. With card_id: {depends_on, blocks, unmet} for "
            "that card (blocks = cards waiting on it, unmet = dependencies not yet "
            "done/accepted). Without card_id: the depends_on of every card on the "
            "board. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string",
                            "description": "Optional: one card's dependency view"},
            },
        },
    },
    {
        "name": "create_archive",
        "description": (
            "Create an archive — a folder under archives/ with a manifest and a "
            "cards/ subfolder that will hold archived cards. Archives have a "
            "type label ('cards', 'deck', 'project', or any slug) so different "
            "kinds of archives can live side by side; type defaults to 'cards'. "
            "Use this to shelve finished work: e.g. one archive per release or "
            "per retired deck. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "minLength": 1,
                         "description": "Archive name (unique; becomes the folder slug)"},
                "type": {"type": "string", "description": "Archive type label: 'cards', 'deck', 'project', or any slug (default 'cards')"},
                "description": {"type": "string", "description": "Optional free-text description stored in the manifest"},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_archives",
        "description": (
            "List archives (id, name, type, item count, archived decks), newest "
            "first. Optional type filter. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "type": {"type": "string", "description": "Only archives of this type label"},
            },
        },
    },
    {
        "name": "get_archive",
        "description": (
            "Get one archive by id: manifest plus its archived cards (items). " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {"archive_id": {"type": "string"}},
            "required": ["archive_id"],
        },
    },
    {
        "name": "archive_card",
        "description": (
            "Move a card into an archive (the archive is a folder under "
            "archives/; the card's file moves into its cards/ subfolder and it "
            "leaves the board). Only ACCEPTED cards can be archived from the "
            "board (409 otherwise); a card already in an archive can be moved to "
            "another archive. Use restore_card to bring it back. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_id": {"type": "string", "description": "The card to archive/move"},
                "archive_id": {"type": "string", "description": "Target archive"},
            },
            "required": ["card_id", "archive_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "archive_cards",
        "description": (
            "Move a batch of cards (e.g. a whole deck/stack of accepted cards) "
            "into one archive in a single call. Every board card must be accepted. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "archive_id": {"type": "string"},
            },
            "required": ["card_ids", "archive_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "archive_stack",
        "description": (
            "Retire a whole deck (stack) in one call: every card in it moves "
            "into an archive, the deck is recorded in the archive's manifest "
            "(title + card order) and dissolved from the board. Without "
            "archive_id an archive named after the deck (type 'deck') is "
            "created — or reused when one with that name exists — which is the "
            "board's structure: one archive per retired deck. Every card must "
            "be accepted (409 otherwise). " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stack_id": {"type": "string", "description": "The deck to retire"},
                "archive_id": {"type": "string",
                               "description": "Target archive (default: one named after the deck)"},
            },
            "required": ["stack_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "restore_card",
        "description": (
            "Move an archived card back to the board (it reappears in the "
            "Accepted column with its history intact). " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
    {
        "name": "ask_user",
        "description": (
            "Ask the user (the person watching this card on the "
            "dashboard) a question when the spec is genuinely ambiguous and a "
            "wrong guess would be costly. The question is shown ON THE CARD'S "
            "CARD and this tool call BLOCKS (pauses the session) until the user "
            "answers on the card, cancels, or timeout_seconds elapse. The tool "
            "result carries status \"answered\" (with the user's answer) or "
            "\"timeout\"/\"cancelled\" — in the latter two cases make a "
            "reasonable choice and record it in NOTES.md. Pass \"options\" for a "
            "multiple-choice question (the card renders one button per option). "
            "Use sparingly: for minor ambiguity just pick a sensible default and "
            "card it in NOTES.md. Only available inside a card session. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1,
                             "description": "The question to show on the card"},
                "options": {"type": "array", "items": {"type": "string"},
                            "minItems": 1, "maxItems": 8,
                            "description": "Optional multiple-choice options (card buttons)"},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 3600,
                                    "description": "How long to wait for an answer "
                                                   "(default 600, max 3600)"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_stacks",
        "description": (
            "List the board's stacks (decks): named piles of cards that sit in "
            "one column, with a header. Each stack has id, col, title and ids "
            "(bottom -> top). Use this before moving cards so you know which "
            "decks exist. " + _COMMON),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "create_stack",
        "description": (
            "Create a deck: a header (title) plus the cards it holds. All cards "
            "must be in the same column; col defaults to the first card's "
            "column. A card can be in one deck at a time, so cards already "
            "stacked elsewhere are moved here. Needs at least two cards. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The deck's header"},
                "card_ids": {"type": "array", "items": {"type": "string"},
                             "minItems": 2, "description": "Cards to put in the deck"},
                "col": {"type": "string",
                        "enum": ["draft", "running", "reviewing", "done", "accepted", "failed"],
                        "description": "Board column (default: the first card's column)"},
            },
            "required": ["title", "card_ids"],
        },
    },
    {
        "name": "update_stack",
        "description": (
            "Rename a deck (title) and/or replace its members (card_ids). "
            "Replacing members keeps the deck's id and header. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stack_id": {"type": "string"},
                "title": {"type": "string", "description": "New header"},
                "card_ids": {"type": "array", "items": {"type": "string"},
                             "minItems": 2, "description": "New member list (bottom -> top)"},
            },
            "required": ["stack_id"],
        },
    },
    {
        "name": "delete_stack",
        "description": (
            "Dissolve a deck. Its cards stay on the board as plain cards. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {"stack_id": {"type": "string"}},
            "required": ["stack_id"],
        },
    },
    {
        "name": "stack_cards",
        "description": (
            "Put cards into a deck — the way to move cards around. With "
            "stack_id they are added to that deck (leaving whatever deck they "
            "were in); without it a new deck is created, so pass title too. "
            "Cards must be in the deck's column. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {
                "card_ids": {"type": "array", "items": {"type": "string"},
                             "minItems": 1, "description": "Cards to stack"},
                "stack_id": {"type": "string",
                             "description": "Target deck (omit to create a new one)"},
                "title": {"type": "string",
                          "description": "Header for the new deck (ignored with stack_id)"},
            },
            "required": ["card_ids"],
        },
    },
    {
        "name": "unstack_card",
        "description": (
            "Pull one card out of whichever deck holds it. The card stays on "
            "the board; a deck left with a single card is dissolved. " + _COMMON),
        "inputSchema": {
            "type": "object",
            "properties": {"card_id": {"type": "string"}},
            "required": ["card_id"],
        },
    },
]


# --------------------------------------------------------------------------
# tool implementations
# --------------------------------------------------------------------------

def _api() -> HarnessAPI:
    return HarnessAPI(harness_url())


def _reject_forbidden_args(args: dict) -> str | None:
    for key in FORBIDDEN_NOTE_ARGS:
        if key in args:
            return (
                f"argument '{key}' is not allowed here: the harness MCP has no "
                "way to set or switch projects — cards created/updated through "
                "it stay in the current card's project"
            )
    return None


def _current_card(api: HarnessAPI) -> dict | None:
    card_id = current_card_id()
    if not card_id:
        return None
    try:
        card = api.get(f"/api/cards/{card_id}")
    except HarnessError:
        return None
    return card if isinstance(card, dict) else None


def tool_current_card(args: dict) -> object:
    card_id = current_card_id()
    if not card_id:
        return {"card_id": None,
                "detail": "not running inside a harness card session (no card context)"}
    card = _current_card(_api())
    return {"card_id": card_id, "card": card}


def tool_list_cards(args: dict) -> object:
    cards = _api().get("/api/cards")
    if not isinstance(cards, list):
        return cards
    status = args.get("status")
    project = args.get("project")
    if status:
        cards = [n for n in cards if n.get("status") == status]
    if project:
        cards = [n for n in cards if (n.get("project") or "") == project]
    return cards


def tool_get_card(args: dict) -> object:
    return _api().get(f"/api/cards/{args['card_id']}")


def tool_create_card(args: dict) -> object:
    if (err := _reject_forbidden_args(args)) is not None:
        raise HarnessError(err)
    payload = {
        "title": args["title"],
        "body": args.get("body", ""),
        "template": args.get("template"),
        "auto_run": bool(args.get("auto_run", False)),
        "auto_accept": bool(args.get("auto_accept", False)),
    }
    # A card can be pinned to its own model (see list_models): a worker that
    # splits a spec can put the mechanical subtasks on a cheap model and the
    # hard one on a strong model.
    if args.get("model"):
        payload["model"] = args["model"]
    # Inherit the calling card's project/workdir so the new card runs in the
    # same place — the model cannot point it anywhere else. The new card also
    # becomes a CHILD of the calling card: a worker that splits its spec into
    # sub-cards is decomposing that card, and the board shows the tree.
    parent = _current_card(_api())
    if parent:
        if parent.get("project"):
            payload["project"] = parent["project"]
        if parent.get("workdir"):
            payload["workdir"] = parent["workdir"]
        if parent.get("id"):
            payload["parent_id"] = parent["id"]
    return _api().post("/api/cards", payload)


def tool_update_card(args: dict) -> object:
    if (err := _reject_forbidden_args(args)) is not None:
        raise HarnessError(err)
    card_id = args["card_id"]
    payload = {k: v for k, v in args.items()
               if k in ("title", "body", "auto_run", "auto_accept", "model")
               and k != "card_id"}
    return _api().put(f"/api/cards/{card_id}", payload)


def tool_delete_card(args: dict) -> object:
    _api().delete(f"/api/cards/{args['card_id']}")
    return {"deleted": args["card_id"]}


def tool_run_card(args: dict) -> object:
    return _api().post(f"/api/cards/{args['card_id']}/run")


def tool_stop_card(args: dict) -> object:
    _api().post(f"/api/cards/{args['card_id']}/stop")
    return {"stopped": args["card_id"]}


def tool_accept_card(args: dict) -> object:
    return _api().post(f"/api/cards/{args['card_id']}/accept")


def tool_accept_all_cards(args: dict) -> object:
    return _api().post("/api/cards/accept-all")


def tool_revert_card(args: dict) -> object:
    return _api().post(f"/api/cards/{args['card_id']}/revert")


def tool_card_log(args: dict) -> object:
    tail = args.get("tail", 200)
    return _api().get(f"/api/cards/{args['card_id']}/log?tail={int(tail)}")


def tool_list_templates(args: dict) -> object:
    return _api().get("/api/templates")


def tool_list_models(args: dict) -> object:
    """The aliases a card can be put on, plus what each role currently uses."""
    data = _api().get("/api/models")
    if not isinstance(data, dict):
        return data
    return {
        "models": [{"alias": m["alias"], "label": m.get("label"),
                    "backend": m.get("backend"),
                    "thinking": m.get("thinking")} for m in data.get("models", [])],
        "roles": [{"role": r["key"], "model": r.get("effective"),
                   "backend": r.get("effective_backend"),
                   "inherited": r.get("inherited")} for r in data.get("roles", [])],
        "default_model": data.get("default_model"),
        "card": ("Pass `model` to create_card/update_card to pin a card to one "
                 "of these aliases; omit it to inherit the worker role's model. "
                 "The alias's `backend` is the platform that runs it."),
    }


def tool_list_transcripts(args: dict) -> object:
    return _api().get(f"/api/cards/{args['card_id']}/transcripts")


def tool_get_transcript(args: dict) -> object:
    """One round's journal as text (the API parses it with parse_journal_line)."""
    try:
        limit = int(args.get("limit") or DEFAULT_TRANSCRIPT_EVENTS)
    except (TypeError, ValueError):
        raise HarnessError("limit must be an integer")
    limit = max(1, min(limit, MAX_TRANSCRIPT_EVENTS))
    round_name = urllib.parse.quote(str(args["round"]), safe="")
    return _api().get(f"/api/cards/{args['card_id']}/transcripts/{round_name}"
                      f"/events?limit={limit}")


def tool_stats(args: dict) -> object:
    project = args.get("project")
    path = "/api/stats" + (f"?project={project}" if project else "")
    return _api().get(path)


def tool_list_projects(args: dict) -> object:
    return _api().get("/api/projects")


def tool_add_dependency(args: dict) -> object:
    card_id = args["card_id"]
    depends_on = args["depends_on"]
    return _api().post(f"/api/cards/{card_id}/depends/{depends_on}")


def tool_remove_dependency(args: dict) -> object:
    card_id = args["card_id"]
    depends_on = args["depends_on"]
    return _api().delete(f"/api/cards/{card_id}/depends/{depends_on}")


def tool_list_dependencies(args: dict) -> object:
    card_id = args.get("card_id")
    if card_id:
        return _api().get(f"/api/cards/{card_id}/dependencies")
    cards = _api().get("/api/cards")
    if not isinstance(cards, list):
        return cards
    return [{"id": n.get("id"), "title": n.get("title"),
             "status": n.get("status"), "depends_on": n.get("depends_on") or []}
            for n in cards]


def tool_create_archive(args: dict) -> object:
    payload = {
        "name": args["name"],
        "type": args.get("type") or "cards",
        "description": args.get("description") or "",
    }
    return _api().post("/api/archives", payload)


def tool_list_archives(args: dict) -> object:
    archives = _api().get("/api/archives")
    if not isinstance(archives, list):
        return archives
    type_filter = args.get("type")
    if type_filter:
        archives = [a for a in archives if (a.get("type") or "") == type_filter]
    return archives


def tool_get_archive(args: dict) -> object:
    return _api().get(f"/api/archives/{args['archive_id']}")


def tool_archive_card(args: dict) -> object:
    return _api().post(
        f"/api/archives/{args['archive_id']}/cards/{args['card_id']}")


def tool_archive_cards(args: dict) -> object:
    return _api().post(f"/api/archives/{args['archive_id']}/cards",
                       {"card_ids": args["card_ids"]})


def tool_archive_stack(args: dict) -> object:
    archive_id = args.get("archive_id")
    if archive_id:
        return _api().post(f"/api/archives/{archive_id}/stacks/{args['stack_id']}")
    return _api().post(f"/api/archives/stacks/{args['stack_id']}")


def tool_restore_card(args: dict) -> object:
    return _api().post(f"/api/cards/{args['card_id']}/restore")


def tool_list_stacks(args: dict) -> object:
    return _api().get("/api/stacks")


def tool_create_stack(args: dict) -> object:
    payload = {"title": args.get("title") or "", "card_ids": args["card_ids"]}
    if args.get("col"):
        payload["col"] = args["col"]
    return _api().post("/api/stacks", payload)


def tool_update_stack(args: dict) -> object:
    payload = {}
    if "title" in args:
        payload["title"] = args["title"]
    if "card_ids" in args:
        payload["card_ids"] = args["card_ids"]
    return _api().request("PATCH", f"/api/stacks/{args['stack_id']}", payload)


def tool_delete_stack(args: dict) -> object:
    return _api().delete(f"/api/stacks/{args['stack_id']}")


def tool_stack_cards(args: dict) -> object:
    stack_id = args.get("stack_id")
    if stack_id:
        return _api().post(f"/api/stacks/{stack_id}/cards",
                           {"card_ids": args["card_ids"]})
    return _api().post("/api/stacks", {"title": args.get("title") or "",
                                       "card_ids": args["card_ids"]})


def tool_unstack_card(args: dict) -> object:
    return _api().post(f"/api/cards/{args['card_id']}/unstack")


def _ask_default_timeout() -> float:
    raw = os.environ.get("VIBE_HARNESS_ASK_USER_TIMEOUT", "").strip()
    try:
        value = float(raw) if raw else DEFAULT_ASK_TIMEOUT_SECONDS
    except ValueError:
        value = DEFAULT_ASK_TIMEOUT_SECONDS
    return min(max(value, 1.0), MAX_ASK_TIMEOUT_SECONDS)


def tool_ask_user(args: dict) -> object:
    """Post a question on the current card and block until the user
    answers (or cancels / the timeout hits); returns the answer to the model.

    Implemented as create + long-poll against the dashboard API: the POST
    registers the pending question (rendered on the card), then repeated
    GET .../wait calls block server-side until the record resolves.
    """
    card_id = current_card_id()
    if not card_id:
        raise HarnessError(
            "ask_user is only available inside a harness card session "
            "(no card context — VIBE_HARNESS_NOTE_ID is not set)")
    question = args.get("question")
    if not isinstance(question, str) or not question.strip():
        raise HarnessError("ask_user requires a non-empty 'question' string")
    options = args.get("options")
    if options is not None:
        if not isinstance(options, list) or not (1 <= len(options) <= 8) \
                or not all(isinstance(o, str) and o.strip() for o in options):
            raise HarnessError("'options' must be a list of 1..8 non-empty strings")
        options = [o.strip() for o in options]
    timeout = args.get("timeout_seconds", _ask_default_timeout())
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise HarnessError("'timeout_seconds' must be a number")
    timeout = min(max(float(timeout), 1.0), MAX_ASK_TIMEOUT_SECONDS)

    api = _api()
    payload: dict = {"question": question.strip()}
    if options:
        payload["options"] = options
    record = api.post(f"/api/cards/{card_id}/questions", payload)
    if not isinstance(record, dict) or "id" not in record:
        raise HarnessError(f"could not register the question on card {card_id!r}: {record!r}")
    qid = record["id"]

    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            # The model gave up waiting: withdraw the question from the card
            # (best effort) so it does not linger as pending.
            try:
                api.post(f"/api/cards/{card_id}/questions/{qid}/cancel")
            except HarnessError:
                pass
            return {"question_id": qid, "question": record.get("question"),
                    "status": "timeout",
                    "hint": f"no answer within {int(timeout)}s — make a reasonable "
                            "choice and record it in NOTES.md"}
        chunk = min(ASK_WAIT_CHUNK_SECONDS, remaining)
        # HTTP client timeout must comfortably exceed the server-side wait.
        res = api.get(f"/api/cards/{card_id}/questions/{qid}/wait"
                      f"?timeout={chunk:.1f}", timeout=chunk + 15.0)
        if isinstance(res, dict) and res.get("status") not in (None, "pending"):
            out = dict(res)
            if out.get("status") == "cancelled":
                out["hint"] = ("the question was cancelled (the session ended or the "
                               "user dismissed it) — make a reasonable choice and "
                               "record it in NOTES.md")
            return out
        # still pending after this chunk -> loop until the total timeout


TOOL_FUNCS = {
    "current_card": tool_current_card,
    "list_cards": tool_list_cards,
    "get_card": tool_get_card,
    "create_card": tool_create_card,
    "update_card": tool_update_card,
    "delete_card": tool_delete_card,
    "run_card": tool_run_card,
    "stop_card": tool_stop_card,
    "accept_card": tool_accept_card,
    "accept_all_cards": tool_accept_all_cards,
    "revert_card": tool_revert_card,
    "card_log": tool_card_log,
    "list_templates": tool_list_templates,
    "list_models": tool_list_models,
    "list_transcripts": tool_list_transcripts,
    "get_transcript": tool_get_transcript,
    "stats": tool_stats,
    "list_projects": tool_list_projects,
    "add_dependency": tool_add_dependency,
    "remove_dependency": tool_remove_dependency,
    "list_dependencies": tool_list_dependencies,
    "create_archive": tool_create_archive,
    "list_archives": tool_list_archives,
    "get_archive": tool_get_archive,
    "archive_card": tool_archive_card,
    "archive_cards": tool_archive_cards,
    "archive_stack": tool_archive_stack,
    "restore_card": tool_restore_card,
    "list_stacks": tool_list_stacks,
    "create_stack": tool_create_stack,
    "update_stack": tool_update_stack,
    "delete_stack": tool_delete_stack,
    "stack_cards": tool_stack_cards,
    "unstack_card": tool_unstack_card,
    "ask_user": tool_ask_user,
}


def call_tool(name: str, args: dict) -> dict:
    """Dispatch one tools/call; returns the MCP result object."""
    func = TOOL_FUNCS.get(name)
    if func is None:
        raise HarnessError(f"unknown tool {name!r}; available: {sorted(TOOL_FUNCS)}")
    if not isinstance(args, dict):
        raise HarnessError("tool arguments must be an object")
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    missing = [r for r in schema.get("required", []) if r not in args]
    if missing:
        raise HarnessError(f"missing required argument(s): {', '.join(missing)}")
    try:
        result = func(args)
        text = json.dumps(result, indent=2, default=str)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except HarnessError as exc:
        return {"content": [{"type": "text", "text": str(exc)}], "isError": True}


# --------------------------------------------------------------------------
# JSON-RPC 2.0 / MCP stdio loop
# --------------------------------------------------------------------------

def _result(mid, result) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def handle_message(msg: dict) -> dict | None:
    """Handle one JSON-RPC message; returns the response or None for notifications."""
    method = msg.get("method")
    mid = msg.get("id")
    if not isinstance(method, str):
        return None if mid is None else _error(mid, -32600, "invalid request")
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        # Spec backward-compat: echo the client's requested protocolVersion
        # when it is a string; fall back to our own only when it sends none.
        params = msg.get("params") or {}
        version = params.get("protocolVersion") if isinstance(params, dict) else None
        return _result(mid, {
            "protocolVersion": version if isinstance(version, str) else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": TOOLS})
    if method == "tools/call":
        params = msg.get("params") or {}
        name = params.get("name") if isinstance(params, dict) else None
        args = params.get("arguments") if isinstance(params, dict) else None
        if not isinstance(name, str):
            return _error(mid, -32602, "tools/call requires a tool name")
        if name not in TOOL_FUNCS:
            return _error(mid, -32602, f"unknown tool {name!r}")
        try:
            result = call_tool(name, args if isinstance(args, dict) else {})
        except Exception as exc:  # never crash the loop on one bad call
            result = {"content": [{"type": "text", "text": f"error: {exc}"}], "isError": True}
        return _result(mid, result)
    if method == "resources/list":
        return _result(mid, {"resources": []})
    if method == "prompts/list":
        return _result(mid, {"prompts": []})
    if mid is None:
        return None
    return _error(mid, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = _error(None, -32700, "parse error")
        else:
            if not isinstance(msg, dict):
                resp = _error(None, -32600, "invalid request")
            else:
                resp = handle_message(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
