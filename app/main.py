"""FastAPI app: HTTP API + static dashboard. Run with:  uvicorn app.main:app --reload --reload-dir app --port 8734
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import fsdialog
from app.archives import (
    ArchiveError,
    ArchiveExists,
    ArchiveNotFound,
    ArchiveStore,
    CardInOtherArchive,
    CardNotArchived,
)
from app.config import (
    ARCHIVES_DIR,
    BASE_DIR,
    MODELS_FILE,
    SETTINGS_FILE,
    CARDS_DIR,
    PROJECTS_FILE,
    STATIC_DIR,
    TEMPLATES_DIR,
    Config,
    load_config,
)
from app.gitops import GitError
from app.cards import (
    STATUSES,
    CardConflict,
    DependencyError,
    InvalidTransition,
    CardNotFound,
    CardRunning,
    CardStore,
)
from app.backends import BackendError, describe_backends
from app.models import ModelError, ModelSettings, describe as describe_models
from app.projects import ProjectError, ProjectExists, ProjectNotFound, ProjectStore
from app.questions import (
    QuestionError,
    QuestionNotFound,
    QuestionNotPending,
    QuestionStore,
)
from app import diffs, dupes, failures, gitops, routemd
from app.search import search_cards
from app.runner import (
    TOKEN_BREAKDOWN_KEYS,
    NotRunningError,
    SessionRunner,
    TestFailedError,
    WorkdirNotAllowed,
)
from app.settings import SettingsError, SettingsStore, describe_accept_modes
from app.stacks import StackError, StackNotFound, StackStore
from app.tokens import card_token_breakdown, card_tokens


class CardIn(BaseModel):
    title: str = Field(min_length=1)
    body: str = ""
    template: str | None = None
    auto_run: bool = False
    auto_accept: bool = False
    project: str | None = None
    workdir: str | None = None
    parent_id: str | None = None


class CardUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    auto_run: bool | None = None
    auto_accept: bool | None = None
    project: str | None = None
    workdir: str | None = None
    parent_id: str | None = None
    # Optimistic concurrency: the updated_at the editor loaded. When present
    # and stale the update is refused with 409 instead of overwriting somebody
    # else's edit. Absent (MCP tools, scripts) = last write wins.
    expected_updated_at: str | None = None


class ProjectIn(BaseModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)


class ArchiveIn(BaseModel):
    name: str = Field(min_length=1)
    type: str = "cards"
    description: str = ""
    # Which project the archive belongs to ("" = not tied to one). The board
    # groups the archive browser by it.
    project: str = ""


class ArchivePatchIn(BaseModel):
    project: str | None = None


class ArchiveNotesIn(BaseModel):
    card_ids: list[str] = Field(min_length=1)


class StackIn(BaseModel):
    """Create a deck: a header plus the cards it holds (same column)."""
    card_ids: list[str] = Field(min_length=2)
    title: str = ""
    col: str | None = None


class StackPatchIn(BaseModel):
    title: str | None = None
    card_ids: list[str] | None = None
    col: str | None = None


class StackCardsIn(BaseModel):
    card_ids: list[str] = Field(min_length=1)


class SettingsIn(BaseModel):
    backend: str | None = None
    # Concurrent-session cap; 0 = unlimited. None = leave it unchanged.
    max_concurrent: int | None = Field(default=None, ge=0)
    accept_mode: str | None = None
    # Hold an idle-sleep inhibitor while cards run or wait. None = unchanged.
    prevent_sleep: bool | None = None


class ModelsIn(BaseModel):
    """Per-role model choice: {role: alias}. An empty alias clears the role."""
    models: dict[str, str] = Field(default_factory=dict)


class QuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    options: list[str] | None = None


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=8000)


def static_version() -> str:
    """Fingerprint of the front-end assets (name, size, mtime) served to the tab."""
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            stat = path.stat()
            digest.update(f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode())
    return digest.hexdigest()[:12]


def create_app(config: Config | None = None,
               store: CardStore | None = None,
               runner: SessionRunner | None = None,
               projects: ProjectStore | None = None,
               questions: QuestionStore | None = None,
               archives: ArchiveStore | None = None,
               stacks: StackStore | None = None,
               models: ModelSettings | None = None,
               settings: SettingsStore | None = None) -> FastAPI:
    """Application factory. Tests pass temp-dir store/runner/projects + a fake vibe bin."""
    config = config or load_config()
    store = store or CardStore(CARDS_DIR)
    projects = projects or ProjectStore(PROJECTS_FILE)
    models = models or ModelSettings(MODELS_FILE)
    settings = settings or SettingsStore(SETTINGS_FILE)
    runner = runner or SessionRunner(store, config, projects=projects,
                                     models=models, settings=settings)
    if runner.settings is None:
        runner.settings = settings
    # The stack store prunes against the card store, so it has to live beside
    # it: falling back to the project's STACKS_FILE would let an app built
    # with a temp card store (every test app) prune the real board against
    # an empty card list and save it empty on the first GET /api/stacks.
    stacks = stacks or StackStore(store.cards_dir.parent / "stacks.json", store)
    # Archiving a deck needs the deck store (it records and dissolves the deck).
    archives = archives or ArchiveStore(ARCHIVES_DIR, store, projects, stacks=stacks)
    # An archive store passed in by a caller (tests, embedders) may predate the
    # deck wiring; give it this app's stack store so /api/archives/stacks works.
    if archives.stacks is None:
        archives.stacks = stacks
    # Share the runner's QuestionStore so the HTTP endpoints and the runner
    # (which cancels pending questions when a session ends) see one state.
    questions = questions or runner.questions

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not config.vibe_available():
            print(f"WARNING: vibe CLI {config.vibe_bin!r} not found on PATH — "
                  f"runs will fail until the Vibe CLI is installed.")
        try:
            runner.recover_stale()
        except Exception as exc:
            # Startup recovery is best effort: a broken card must never keep
            # the server from coming up (per-card isolation lives in
            # SessionRunner.recover_stale).
            print(f"WARNING: startup recovery failed: {exc!r}")
        # Auto-create/auto-update the managed route.md in every project folder.
        routemd.sync_all_projects(projects)
        # Keep the machine awake while cards run or wait. A sleeping machine
        # suspends the session process, the CLI gives up and reports
        # `interrupted` (exit 3) — a card that looks failed but was only
        # paused. The inhibitor is a child process, so it dies with the server.
        try:
            runner.sleep_guard.start(runner.has_pending_work)
        except Exception as exc:
            print(f"WARNING: could not start the sleep guard: {exc!r}")
        yield
        try:
            runner.sleep_guard.stop()
        except Exception:
            pass

    app = FastAPI(title="vibe-harness", lifespan=lifespan)

    @app.middleware("http")
    async def _revalidate_static(request, call_next):
        """Make the browser revalidate the dashboard assets on every load.

        StaticFiles sends an ETag but no Cache-Control, so a reload can be
        answered from the browser's heuristic cache. `no-cache` keeps the
        revalidation (cheap 304s) while guaranteeing a reload picks up a new
        app.js/style.css.
        """
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # --- error mapping (spec §8) -------------------------------------------
    @app.exception_handler(CardNotFound)
    async def _card_not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(CardRunning)
    async def _card_running(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InvalidTransition)
    async def _invalid_transition(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DependencyError)
    async def _dependency_error(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CardConflict)
    async def _card_conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(NotRunningError)
    async def _not_running(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(WorkdirNotAllowed)
    async def _workdir_not_allowed(request, exc):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(GitError)
    async def _git_error(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(TestFailedError)
    async def _test_failed(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ProjectNotFound)
    async def _project_not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ProjectExists)
    async def _project_exists(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ProjectError)
    async def _project_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(QuestionNotFound)
    async def _question_not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(QuestionNotPending)
    async def _question_not_pending(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(QuestionError)
    async def _question_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ArchiveNotFound)
    async def _archive_not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(CardNotArchived)
    async def _card_not_archived(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ArchiveExists)
    async def _archive_exists(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CardInOtherArchive)
    async def _card_in_other_archive(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(ArchiveError)
    async def _archive_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(StackNotFound)
    async def _stack_not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(StackError)
    async def _stack_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(ModelError)
    async def _model_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(BackendError)
    async def _backend_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(SettingsError)
    async def _settings_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # --- cards ---------------------------------------------------------------
    @app.get("/api/cards")
    def list_cards():
        # Attach each card's Q&A history so the cards (and the MCP) can show a
        # pending question without an extra request per card; annotate_card
        # attaches session.live_tokens for live sessions (zero I/O when
        # nothing is running).
        return [runner.annotate_card(dict(n, questions=questions.list(n["id"]))) for n in store.list()]

    @app.post("/api/cards", status_code=201)
    def create_card(payload: CardIn):
        card = store.create(payload.title, payload.body,
                            template=payload.template, auto_run=payload.auto_run,
                            auto_accept=payload.auto_accept,
                            project=payload.project, workdir=payload.workdir,
                            parent_id=payload.parent_id)
        if card["auto_run"] and card["status"] == "draft":
            try:
                runner.run(card["id"])
            except CardRunning:
                pass
            card = store.get(card["id"])
        return card

    @app.get("/api/cards/{card_id}")
    def get_card(card_id: str):
        return runner.annotate_card(dict(store.get(card_id), questions=questions.list(card_id)))

    @app.put("/api/cards/{card_id}")
    def update_card(card_id: str, payload: CardUpdate):
        card = store.update(card_id, title=payload.title,
                            body=payload.body, auto_run=payload.auto_run,
                            auto_accept=payload.auto_accept,
                            project=payload.project, workdir=payload.workdir,
                            parent_id=payload.parent_id,
                            expected_updated_at=payload.expected_updated_at)
        if card["auto_run"] and card["status"] == "draft":
            try:
                runner.run(card_id)
            except CardRunning:
                pass
            card = store.get(card_id)
        return card

    @app.delete("/api/cards/{card_id}", status_code=204)
    def delete_card(card_id: str):
        # Removes the card, its linked git worktree (if any), and its
        # sessions/<id>/ dir (which holds questions.json too).
        # CardRunning -> 409, CardNotFound -> 404.
        runner.delete_card(card_id)
        questions.drop(card_id)
        return Response(status_code=204)

    # --- dependencies ("runs after" links between cards) ----------------------
    @app.post("/api/cards/{card_id}/depends/{dep_id}")
    def add_dependency(card_id: str, dep_id: str):
        # card_id will be run automatically once dep_id completes
        # (done/accepted). 404 unknown card, 409 self-link/cycle/live card.
        # A draft whose dependencies are already satisfied starts running
        # right away; otherwise it is marked waiting (session.waiting_for).
        store.add_dependency(card_id, dep_id)
        card = runner.run_if_ready(card_id)
        return runner.annotate_card(dict(card, questions=questions.list(card_id)))

    @app.delete("/api/cards/{card_id}/depends/{dep_id}")
    def remove_dependency(card_id: str, dep_id: str):
        store.remove_dependency(card_id, dep_id)
        # If the removed edge was the last unmet dependency, run the draft now.
        card = runner.run_if_ready(card_id)
        return runner.annotate_card(dict(card, questions=questions.list(card_id)))

    @app.get("/api/cards/{card_id}/dependencies")
    def get_dependencies(card_id: str):
        # {depends_on, blocks, unmet} for one card (CardNotFound -> 404).
        return store.dependencies(card_id)

    # --- sessions --------------------------------------------------------------
    @app.post("/api/cards/{card_id}/run")
    def run_card(card_id: str):
        return runner.run(card_id)

    @app.post("/api/cards/{card_id}/stop", status_code=204)
    def stop_card(card_id: str):
        runner.stop(card_id)
        return Response(status_code=204)

    @app.post("/api/cards/{card_id}/accept")
    def accept_card(card_id: str):
        # Gate + git merge + (on conflict) resolver spawn, all inside the
        # runner. GitError -> 409 (dirty tree, resolver already running, ...).
        # A second accept for a repo that is already merging is not an error:
        # the card comes back queued (session.accept_queued + queue_reason) and
        # merges automatically when the repo frees.
        return runner.accept_card(card_id)

    @app.post("/api/cards/accept-all")
    def accept_all_cards():
        # Snapshot the cards in Done NOW and merge them one at a time in a
        # background drain. A card that reaches Done later is not part of this
        # batch — it renders below the button and the next click picks it up.
        return runner.accept_all()

    @app.post("/api/cards/{card_id}/revert")
    def revert_card(card_id: str):
        # Undo an accepted card: revert its merge commit and reopen it as a
        # draft. An archived card comes back to the board first (the existing
        # restore path), so the revert lands on a card the board can show.
        # Missing merge commit / dirty tree / conflict -> 409.
        try:
            store.get(card_id)
        except CardNotFound:
            archives.restore_card(card_id)      # CardNotArchived -> 404
        return runner.revert_card(card_id)

    @app.get("/api/cards/{card_id}/log")
    def get_log(card_id: str, tail: int = Query(200, ge=1, le=5000)):
        return {"lines": runner.log_tail(card_id, n=tail)}

    @app.get("/api/cards/{card_id}/diff")
    def get_diff(card_id: str, range: str = Query(diffs.DEFAULT_RANGE)):
        # What the card actually changed: card (what accept merged), round
        # (what the reviewer judged) or worktree (uncommitted changes), plus
        # the verdict record for it. A sandbox card answers "not git-backed";
        # an unknown range is a 400.
        card = store.get(card_id)
        try:
            return diffs.card_diff(card, range)
        except diffs.DiffRangeError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # --- questions (model asks the user on the card) ------------------
    @app.post("/api/cards/{card_id}/questions", status_code=201)
    def ask_question(card_id: str, payload: QuestionIn):
        # Used by the MCP ask_user tool: register the pending question that is
        # shown on the card. CardNotFound -> 404.
        store.get(card_id)
        return questions.ask(card_id, payload.question, payload.options)

    @app.get("/api/cards/{card_id}/questions")
    def list_questions(card_id: str):
        store.get(card_id)
        return questions.list(card_id)

    @app.get("/api/cards/{card_id}/questions/{question_id}/wait")
    def wait_question(card_id: str, question_id: str,
                      timeout: float = Query(30.0, ge=0, le=120)):
        # Long-poll: returns the question record once it is answered/cancelled,
        # or the still-pending record after `timeout` seconds. Sync endpoint,
        # so FastAPI runs it in the threadpool and the event loop stays free.
        store.get(card_id)
        return questions.wait(card_id, question_id, timeout)

    @app.post("/api/cards/{card_id}/questions/{question_id}/answer")
    def answer_question(card_id: str, question_id: str, payload: AnswerIn):
        # The user answered on the card; wakes the model's waiting ask_user.
        store.get(card_id)
        return questions.answer(card_id, question_id, payload.answer, by="user")

    @app.post("/api/cards/{card_id}/questions/{question_id}/cancel")
    def cancel_question(card_id: str, question_id: str):
        # The user dismissed the question (or the runner cancelled it when the
        # session ended); the waiting ask_user returns "cancelled".
        store.get(card_id)
        cancelled = questions.cancel(card_id, question_id)
        return cancelled[0] if cancelled else questions.get(card_id, question_id)

    # --- projects --------------------------------------------------------------
    @app.get("/api/projects")
    def list_projects():
        return projects.list()

    @app.post("/api/projects", status_code=201)
    def create_project(payload: ProjectIn):
        project = projects.create(payload.name, payload.path)
        # Opening a new project always sets up its folder: git init + initial
        # commit, managed agent.md, empty (agent-owned) route.md.
        try:
            setup = routemd.setup_project(Path(project["path"]), init_repo=True)
        except Exception as exc:
            setup = {"route_md": None, "agent_md": None, "repo": None,
                     "setup_error": str(exc)}
        return {**project, **setup}

    @app.delete("/api/projects/{name}", status_code=204)
    def delete_project(name: str):
        projects.delete(name)
        return Response(status_code=204)

    # --- archives (the archive is a folder structure: archives/<slug>-<id>/) --
    @app.get("/api/archives")
    def list_archives():
        return archives.list()

    @app.post("/api/archives", status_code=201)
    def create_archive(payload: ArchiveIn):
        # A typed archive folder: archives/<slug>-<id>/{archive.md, cards/}
        return archives.create(payload.name, type=payload.type,
                               description=payload.description,
                               project=payload.project)

    @app.patch("/api/archives/{archive_id}")
    def patch_archive(archive_id: str, payload: ArchivePatchIn):
        # Move an archive under a project header ("" = no project).
        if payload.project is None:
            return archives.get(archive_id)
        return archives.set_project(archive_id, payload.project)

    @app.get("/api/archives/{archive_id}")
    def get_archive(archive_id: str):
        # manifest + the archived cards (items)
        return archives.get(archive_id)

    def _archived_git_infos(card_ids: list[str]) -> list[dict]:
        """Snapshot the git records of cards about to be archived (they leave
        the board, so they cannot be read afterwards)."""
        infos = []
        for card_id in card_ids:
            try:
                git_info = (store.get(card_id).get("session") or {}).get("git") or {}
            except Exception:
                continue
            if git_info.get("worktree"):
                infos.append(git_info)
        return infos

    def _drop_archived_worktrees(git_infos: list[dict]) -> None:
        """Archiving a card removes its linked git worktree (the card branch
        is kept): an archived card is off the board, so nothing reads the
        worktree any more — and the disk cost of keeping every accepted card's
        worktree is exactly what this bounds. Best effort: a broken repo must
        never block the archive."""
        for git_info in git_infos:
            try:
                gitops.remove_card_worktree(git_info)
            except Exception:
                pass

    @app.post("/api/archives/{archive_id}/cards/{card_id}")
    def archive_card(archive_id: str, card_id: str):
        # Move one card into the archive (from the board it must be accepted;
        # from another archive it is a plain move). 404 unknown, 409 gate.
        git_infos = _archived_git_infos([card_id])
        result = archives.archive_card(card_id, archive_id)
        _drop_archived_worktrees(git_infos)
        return result

    @app.post("/api/archives/{archive_id}/cards")
    def archive_cards(archive_id: str, payload: ArchiveNotesIn):
        # Move a batch of cards (e.g. a whole deck/stack) into the archive.
        git_infos = _archived_git_infos(list(payload.card_ids))
        result = archives.archive_cards(payload.card_ids, archive_id)
        _drop_archived_worktrees(git_infos)
        return result

    @app.post("/api/archives/{archive_id}/stacks/{stack_id}")
    def archive_stack(archive_id: str, stack_id: str):
        # Retire a whole deck into this archive: its cards move, the deck is
        # recorded in the manifest and dissolved. 404 unknown, 409 gate.
        git_infos = _archived_git_infos(list(stacks.get(stack_id)["ids"]))
        result = archives.archive_stack(stack_id, archive_id)
        _drop_archived_worktrees(git_infos)
        return result

    @app.post("/api/archives/stacks/{stack_id}")
    def archive_stack_into_new_archive(stack_id: str):
        # Same, but the archive is created (or reused) under the deck's title —
        # one archive per retired deck is the board's structure.
        git_infos = _archived_git_infos(list(stacks.get(stack_id)["ids"]))
        result = archives.archive_stack(stack_id)
        _drop_archived_worktrees(git_infos)
        return result

    @app.delete("/api/archives/{archive_id}/cards/{card_id}")
    def unarchive_card(archive_id: str, card_id: str):
        # Restore a card from this archive back to the board.
        return archives.restore_card(card_id, archive_id=archive_id)

    @app.post("/api/cards/{card_id}/restore")
    def restore_card(card_id: str):
        # Restore an archived card to the board, whichever archive holds it.
        return archives.restore_card(card_id)

    # --- search (board cards + every archived card) --------------------------
    @app.get("/api/search")
    def search_endpoint(response: Response, q: str = ""):
        # A linear scan over the board and every archive: no index, no deps.
        # The list is capped (RESULT_LIMIT); the headers say whether it was cut
        # short and how many cards matched in total, so the documented list
        # shape stays a list.
        results, total = search_cards(q, store, archives)
        response.headers["X-Search-Truncated"] = "true" if total > len(results) else "false"
        response.headers["X-Search-Total"] = str(total)
        return results

    # --- settings (which agent CLI the harness drives, concurrency cap, how
    # accept ships) ----------------------------------------------------------
    def _settings_payload() -> dict:
        """Everything the sidebar's Settings panel needs in one round trip.

        `max_concurrent` is the effective cap (the stored setting, else
        Config.max_concurrent; 0 = unlimited) and `running` is the current
        occupancy, so the panel can say "2 of 3 running". `accept_mode` is the
        effective mode (the stored setting, else Config.accept_mode) and
        `accept_modes` the choices the panel offers. `failure_kinds` is
        `failures.KINDS` — the board builds its failure filter from it, so a
        new kind reaches the UI without a second copy to keep in sync.
        """
        return {"backend": settings.backend(),
                "failure_kinds": list(failures.KINDS),
                "backends": describe_backends(config),
                "max_concurrent": settings.max_concurrent(config.max_concurrent),
                "running": runner.running_count(),
                "accept_mode": settings.accept_mode(config.accept_mode),
                "accept_modes": describe_accept_modes(),
                # Sleep guard: whether the machine is currently being kept
                # awake, and whether this platform can do it at all.
                "prevent_sleep": runner.sleep_guard.enabled,
                "sleep_held": runner.sleep_guard.held(),
                "sleep_supported": runner.sleep_guard.supported()}

    @app.get("/api/settings")
    def get_settings():
        return _settings_payload()

    @app.put("/api/settings")
    def put_settings(payload: SettingsIn):
        if payload.backend is not None:
            settings.set_backend(payload.backend)
        if payload.max_concurrent is not None:
            settings.set_max_concurrent(payload.max_concurrent)
        if payload.accept_mode is not None:
            settings.set_accept_mode(payload.accept_mode)
        if payload.prevent_sleep is not None:
            runner.sleep_guard.enabled = bool(payload.prevent_sleep)
            if not runner.sleep_guard.enabled:
                runner.sleep_guard.release()
        return _settings_payload()

    # --- models (per-role) ---------------------------------------------------
    # Which model each kind of session runs on: the worker (run-N), the
    # reviewer (review-N) and the merge-conflict resolver (resolve-N). The
    # runner pins the choice per spawn with VIBE_ACTIVE_MODEL, so the user's
    # own ~/.vibe/config.toml is never rewritten.
    @app.get("/api/models")
    def get_models():
        return describe_models(models)

    @app.put("/api/models")
    def put_models(payload: ModelsIn):
        models.update(payload.models)
        return describe_models(models)

    # --- stacks (decks) ------------------------------------------------------
    # Server state, not browser state: the MCP tools stack cards and move them
    # between decks, so the board and an LLM must see the same piles.
    @app.get("/api/stacks")
    def list_stacks():
        return stacks.list()

    @app.post("/api/stacks", status_code=201)
    def create_stack(payload: StackIn):
        col = payload.col or stacks.column_of(payload.card_ids[0])
        return stacks.create(col, payload.title, payload.card_ids)

    @app.get("/api/stacks/{stack_id}")
    def get_stack(stack_id: str):
        return stacks.get(stack_id)

    @app.patch("/api/stacks/{stack_id}")
    def patch_stack(stack_id: str, payload: StackPatchIn):
        # Rename a deck (title) and/or replace its members.
        return stacks.update(stack_id, title=payload.title,
                             card_ids=payload.card_ids, col=payload.col)

    @app.delete("/api/stacks/{stack_id}")
    def delete_stack(stack_id: str):
        # Dissolve a deck; its cards stay on the board.
        return stacks.delete(stack_id)

    @app.post("/api/stacks/{stack_id}/cards")
    def add_stack_cards(stack_id: str, payload: StackCardsIn):
        # Add (or move) cards into this deck.
        return stacks.add_cards(stack_id, payload.card_ids)

    @app.delete("/api/stacks/{stack_id}/cards/{card_id}")
    def remove_stack_card(stack_id: str, card_id: str):
        # Pull one card out of a deck (dissolves a deck left with one card).
        stacks.get(stack_id)  # 404 when the deck is gone
        return stacks.remove_card(card_id)

    @app.post("/api/cards/{card_id}/unstack")
    def unstack_card(card_id: str):
        # Pull a card out of whichever deck holds it.
        return stacks.remove_card(card_id)

    @app.post("/api/archives/{archive_id}/projects/{project_name}")
    def archive_project(archive_id: str, project_name: str):
        # Archive an entire deck (project): its non-live cards move into the
        # archive, the deck is recorded in the manifest, the project is
        # unregistered. 404 unknown archive/project, 409 live cards.
        return archives.archive_project(project_name, archive_id)

    # --- misc ------------------------------------------------------------------
    @app.post("/api/fs/choose-folder")
    def choose_folder():
        # Sync endpoint: the native dialog blocks, FastAPI runs this in a
        # threadpool worker so the event loop stays responsive.
        try:
            return {"path": fsdialog.choose_folder()}
        except fsdialog.FolderPickerCancelled:
            return JSONResponse(status_code=409, content={"detail": "cancelled"})
        except fsdialog.FolderPickerUnavailable as exc:
            return JSONResponse(status_code=501, content={"detail": str(exc)})

    @app.get("/api/templates")
    def list_templates():
        templates = []
        for path in sorted(TEMPLATES_DIR.glob("*.md")):
            templates.append({"name": path.stem,
                              "body": path.read_text(encoding="utf-8")})
        return templates

    @app.get("/api/version")
    def version():
        """Fingerprint of the served front-end assets.

        The dashboard is a long-lived tab: without this, a browser keeps running
        whatever app.js it loaded days ago and every front-end fix looks like it
        did nothing. The client polls this and reloads itself when it changes.
        """
        return {"version": static_version()}

    @app.get("/api/split-report")
    def split_report():
        """Do split cards cost less per accepted outcome than unsplit ones?

        The only question that decides whether an orchestrator earns its place.
        A card is "split" when it has children (a worker broke its spec into
        sub-cards); the subtree's tokens are the parent's own plus every
        descendant's, because that is what the split actually cost.
        """
        cards = store.list()
        by_id = {n["id"]: n for n in cards}
        children: dict[str, list[str]] = {}
        for n in cards:
            parent = n.get("parent_id")
            if parent and parent in by_id:
                children.setdefault(parent, []).append(n["id"])

        def subtree_tokens(card_id: str, seen: set[str]) -> int:
            if card_id in seen:
                return 0
            seen.add(card_id)
            card = by_id.get(card_id) or {}
            own = card_tokens(card.get("session") or {})
            return own + sum(subtree_tokens(k, seen) for k in children.get(card_id, []))

        def bucket(rows: list[dict]) -> dict:
            accepted = [n for n in rows if n.get("status") == "accepted"]
            tokens = sum(subtree_tokens(n["id"], set()) for n in rows)
            return {
                "cards": len(rows),
                "accepted": len(accepted),
                "tokens": tokens,
                "tokens_per_accepted": (round(tokens / len(accepted)) if accepted else None),
            }

        # Only top-level cards are counted as "one outcome": a child's cost is
        # already inside its parent's subtree, so counting it again would
        # double-count the split.
        roots = [n for n in cards if not n.get("parent_id")]
        split = [n for n in roots if children.get(n["id"])]
        unsplit = [n for n in roots if not children.get(n["id"])]
        return {
            "split": bucket(split),
            "unsplit": bucket(unsplit),
            "trees": [
                {
                    "id": n["id"],
                    "title": n["title"],
                    "status": n["status"],
                    "children": len(children.get(n["id"], [])),
                    "tokens": subtree_tokens(n["id"], set()),
                }
                for n in sorted(split, key=lambda n: n.get("created_at") or "")
            ],
        }

    @app.get("/api/duplication")
    def duplication():
        """The mechanical duplication report for this project's source.

        The same detector the reviewer is handed (`app/dupes.py`), so "the
        project is growing" is a number instead of an impression: every entry
        is a block of at least six normalized lines that appears in two or more
        places, with the file and line of each occurrence. Paths are relative
        to the project root (`app/runner.py`), the same form a diff uses.
        """
        clones = dupes.find_clones(BASE_DIR)
        return {"count": len(clones), "clones": clones}

    @app.get("/api/stats")
    def stats(project: str | None = Query(None)):
        cards = [runner.annotate_card(n) for n in store.list()]
        if project is not None:
            cards = [n for n in cards if (n.get("project") or "") == project]
        counts = {s: 0 for s in STATUSES}
        total_tokens = 0
        live_tokens = 0  # the in-flight increment over the last recorded totals
        # Per-type token breakdown summed over the cards (cached/output/input);
        # a type no card has ever reported stays None (unknown, not zero).
        breakdown_seen = {k: False for k in TOKEN_BREAKDOWN_KEYS}
        token_breakdown = {k: 0 for k in TOKEN_BREAKDOWN_KEYS}
        total_runtime = 0.0
        finished = 0
        for card in cards:
            counts[card["status"]] = counts.get(card["status"], 0) + 1
            session = card.get("session") or {}
            # The card's total cost (worker + review + resolver rounds), the
            # same number its chip shows — so the board total is exactly the
            # sum of the per-card totals.
            recorded = session.get("card_tokens") or 0
            if session.get("live_tokens") is not None:
                # Live session: count the live-updating per-card total (which
                # already includes the recorded tokens of earlier rounds).
                total_tokens += session["live_tokens"]
                live_tokens += max(0, session["live_tokens"] - recorded)
                bd = session.get("live_token_breakdown")
            else:
                total_tokens += recorded
                bd = card_token_breakdown(session)
            for k in TOKEN_BREAKDOWN_KEYS:
                v = (bd or {}).get(k)
                if v is not None:
                    token_breakdown[k] += v
                    breakdown_seen[k] = True
            if session.get("started_at") and session.get("finished_at"):
                try:
                    start = datetime.fromisoformat(session["started_at"])
                    end = datetime.fromisoformat(session["finished_at"])
                    total_runtime += (end - start).total_seconds()
                    finished += 1
                except ValueError:
                    pass
        avg = total_runtime / finished if finished else 0.0
        return {
            "counts": counts,
            "total_tokens": total_tokens,
            "live_tokens": live_tokens,
            "token_breakdown": {k: (token_breakdown[k] if breakdown_seen[k] else None)
                                for k in TOKEN_BREAKDOWN_KEYS},
            "total_runtime_seconds": round(total_runtime, 3),
            "avg_runtime_seconds": round(avg, 3),
            "project": project,
        }

    # --- server restart (top-bar button) --------------------------------------
    @app.post("/api/server/restart")
    def restart_server():
        """Hard-restart the dashboard server.

        Only acts when THIS process is the one serving on the configured port
        (a live uvicorn run — never an in-process TestClient, and never a
        different server that happens to share the port): spawns a detached
        shell that kills the port listener (and its --reload parent), then
        relaunches uvicorn with --reload. Returns before the old process
        dies, so the dashboard can poll until the API is back.
        """
        import os
        proc = subprocess.run(["lsof", "-ti", f"tcp:{config.port}"],
                              capture_output=True, text=True)
        pids = {int(p) for p in proc.stdout.split() if p.strip().isdigit()}
        if os.getpid() not in pids:
            return JSONResponse(status_code=409, content={
                "detail": f"this process is not the server on {config.host}:{config.port}"})
        script = (
            "sleep 2; "
            f"PID=$(lsof -ti tcp:{config.port} 2>/dev/null); "
            "if [ -n \"$PID\" ]; then "
            "PP=$(ps -o ppid= -p $PID 2>/dev/null | tr -d ' '); "
            "kill $PID 2>/dev/null; "
            "[ -n \"$PP\" ] && [ \"$PP\" != \"1\" ] && kill $PP 2>/dev/null; "
            "fi; "
            "sleep 1; "
            f"cd '{BASE_DIR}' && nohup '{sys.executable}' -m uvicorn app.main:app "
            f"--host {config.host} --port {config.port} --reload "
            "--reload-dir app "  # never watch sessions/ — card worktrees live there
            ">> /tmp/vibe-harness-server.log 2>&1 &"
        )
        subprocess.Popen(["sh", "-c", script], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"status": "restarting", "port": config.port}

    # Catch-all static mount LAST so /api/* routes take precedence.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def __getattr__(name: str):
    """Build the ASGI app on first access, not at import.

    `app = create_app()` at module level meant that merely importing this
    module — which every test does, for `create_app` — built a real CardStore
    on the repo's cards/ dir, and its legacy notes/ -> cards/ migration then
    moved tracked files in every card worktree. `uvicorn app.main:app` still
    works: it resolves the attribute, which lands here. The result is cached
    in the module globals so later accesses skip this hook.
    """
    if name != "app":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    built = create_app()
    globals()["app"] = built
    return built
