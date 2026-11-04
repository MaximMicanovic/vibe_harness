"""SuperviseMixin: process supervision for `SessionRunner` — spawning/stopping
session processes, the time-limit watchdog, the session registry, outcome
recording, restart recovery, and the read-only introspection endpoints'
helpers (activity, messages, log tail) — plus backend/model/env resolution
for spawned sessions. Moved out of the runner core (`app/runner/__init__.py`); the mixin is composed
into `SessionRunner` there.
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from app import (
    activity,
    gitops,
    liveusage,
    mailbox,
    praccept,
)
from app.backends import Backend, get_backend
from app.cards import CardRunning, LIVE_STATUSES
from app.gitops import GitError
from .prompts import (
    REVIEW_TEST_OUTPUT_MAX_CHARS,
    export_token_breakdown,
    now_iso,
    review_prompt_template,
)


# Grace period between SIGTERM and SIGKILL when stopping a session.
STOP_GRACE_SECONDS = 10


# The harness MCP server (app/mcp_server.py), injected into spawned vibe
# sessions via the VIBE_MCP_SERVERS env var so the model can talk to the board.
MCP_SERVER_PATH = Path(__file__).resolve().parent.parent / "mcp_server.py"


#: "not given" for the model arguments below: resolve the role's own choice.
_UNSET = object()

#: Windows process-creation flag (no setsid there): the session gets its own
#: process group, and stop() kills the tree with `taskkill /T`.
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _detach_kwargs() -> dict:
    """How to spawn a session detached from the server's process group.

    POSIX: ``start_new_session=True`` (setsid — the pid becomes the pgid, so
    stop() can signal the whole group with killpg). Windows has no setsid:
    ``CREATE_NEW_PROCESS_GROUP`` instead, and the tree is stopped with
    ``taskkill /T /F`` (see _terminate_session).
    """
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP",
                                         _CREATE_NEW_PROCESS_GROUP)}
    return {"start_new_session": True}


def _windows_pid_alive(pid: int) -> bool:
    """Windows pid liveness via OpenProcess/GetExitCodeProcess.

    os.kill(pid, 0) cannot be used here: on Windows os.kill with any other
    signal calls TerminateProcess — sig 0 would KILL the process instead of
    probing it. Off Windows there is no ctypes.windll, so this reports False.
    """
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259                     # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return False


class TestFailedError(Exception):
    """The configured test command failed at a gate point (maps to HTTP 409)."""


class NotRunningError(Exception):
    """Raised by stop() when the card has no live session."""


class _AdoptedProc:
    """Shim for a session process re-adopted by pid after a server restart.

    The process is not our child (start_new_session detached it), so waitpid
    is impossible; poll() checks liveness with os.kill(pid, 0) and the exit
    code is read from the round's export.json, which a clean vibe exit (or a
    SIGTERM/time-limit stop, exit 3) always writes.
    """

    def __init__(self, pid: int, export_path: Path):
        self.pid = pid
        self._export_path = export_path
        self._exit: int | None = None

    def _alive(self) -> bool:
        return SuperviseMixin._pid_alive(self.pid)

    def _read_exit_code(self) -> int | None:
        try:
            export = json.loads(self._export_path.read_text(encoding="utf-8"))
            code = export.get("exit_code")
            return code if isinstance(code, int) else None
        except Exception:
            return None

    def poll(self) -> int | None:
        if self._alive():
            return None
        if self._exit is None:
            self._exit = self._read_exit_code()
        return self._exit

    def wait(self) -> int | None:
        while self._alive():
            time.sleep(0.5)
        return self.poll()


class SuperviseMixin:

    def _role_choice(self, kind: str) -> dict:
        """The model + backend one session kind resolves to.

        A role with its own choice uses it; "inherit" follows the worker's
        choice, backend included; when the worker inherits too, the fallback is
        Vibe with the CLI's own default model.
        """
        if self.models is None:
            return {"model": None, "backend": self.backend().key}
        return self.models.resolve_session(kind)

    def _role_model(self, kind: str) -> str | None:
        """The model chosen for one session kind, or None to inherit.

        The backend decides how it is passed: Claude takes `--model`, Vibe has
        no such flag and reads VIBE_ACTIVE_MODEL from the environment.
        """
        return self._role_choice(kind)["model"]

    def _role_backend(self, kind: str) -> Backend:
        """The backend that runs the model chosen for one session kind."""
        return get_backend(self._role_choice(kind)["backend"])

    def _pinned_backend(self, card: dict | None, kind: str,
                        resume: str | None) -> str | None:
        """The backend a resumed worker round must keep.

        A fixer round resumes the worker session (`--resume <id>`), and a Vibe
        session id cannot be resumed by Claude. The backend is recorded on the
        card's session when the run starts, so changing the worker's model
        mid-run cannot switch backends under the resume.
        """
        if kind != "run" or not resume or not card:
            return None
        return (card.get("session") or {}).get("backend")

    def _session_backend(self, kind: str, card: dict | None = None,
                         resume: str | None = None) -> Backend:
        """The backend for one session: the card's pin for a resumed worker
        round, else the role's resolved backend."""
        pinned = self._pinned_backend(card, kind, resume)
        return get_backend(pinned or self._role_choice(kind)["backend"])

    def _session_model(self, kind: str, card: dict | None = None,
                       resume: str | None = None) -> str | None:
        """The model for one session, keeping a resumed round on its backend.

        The role's model may have moved to the other platform since the run
        started; the resumed session keeps its pinned backend, so it runs on
        that backend's own default instead of a model it cannot run.
        """
        model = self._role_model(kind)
        if model and self.models is not None:
            backend = self._session_backend(kind, card, resume)
            if not self.models.offers(backend.key, model):
                return None
        return model

    def _model_env(self, kind: str, backend: Backend | None = None,
                   model: str | None | object = _UNSET) -> dict:
        """VIBE_ACTIVE_MODEL for one session, or {} when nothing is pinned.

        Vibe's config layers put VIBE_* env vars above the user's config.toml,
        so this pins the model for this one spawned session — the global config
        is never rewritten, and two sessions cannot race each other's model.
        Claude takes `--model` instead, so it gets no env var.
        """
        backend = backend or self._role_backend(kind)
        if backend.key != "vibe":
            return {}
        if model is _UNSET:
            model = self._role_model(kind)
        return {"VIBE_ACTIVE_MODEL": model} if model else {}

    def _session_env(self, card_id: str) -> dict:
        """Environment for a spawned vibe session.

        Injects the harness MCP server into VIBE_MCP_SERVERS (vibe reads it as
        JSON) so the model gets the mcp_<name> tool group. Any servers already
        present in the inherited VIBE_MCP_SERVERS are kept; an entry with our
        name is replaced. The MCP server process gets VIBE_HARNESS_URL (which
        dashboard to talk to) and VIBE_HARNESS_NOTE_ID (which card this session
        implements) through its env map.
        """
        env = dict(os.environ)
        name = self.config.mcp_server_name
        servers: list = []
        raw = env.get("VIBE_MCP_SERVERS")
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    servers = [s for s in parsed
                               if isinstance(s, dict) and s.get("name") != name]
            except ValueError:
                servers = []
        servers.append({
            "name": name,
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(MCP_SERVER_PATH)],
            "env": {
                "VIBE_HARNESS_URL": self.config.api_base_url(),
                "VIBE_HARNESS_NOTE_ID": card_id,
                "VIBE_HARNESS_ASK_USER_TIMEOUT": str(self.config.ask_user_timeout_seconds),
            },
        })
        env["VIBE_MCP_SERVERS"] = json.dumps(servers)
        # Never let an ambient per-card mailbox (e.g. the harness itself
        # running inside a vibe session) leak into a spawned session — the
        # card's own mailbox vars are set by _spawn when configured.
        for key in [k for k in env if k.startswith("VIBE_HARNESS_MAILBOX")]:
            del env[key]
        return env

    def _adopt_session(self, card: dict, pid: int,
                       export_dir: str | None = None) -> None:
        """Re-register a session process that survived a server restart/reload
        (start_new_session detaches it, so it is not our child and we cannot
        waitpid it). The _AdoptedProc shim polls pid liveness; the exit code
        comes from the round's export.json, which a clean vibe exit writes.

        `export_dir` overrides the round dir used to derive the session kind
        (run/review/resolve) and to read the exit code — pass the resolver's
        export_dir to re-adopt a conflict resolver (kind parses as "resolve").
        """
        card_id = card["id"]
        session = card.get("session") or {}
        if export_dir is None:
            export_dir = session.get("export_dir") or ""
        rdir = Path(export_dir).parent if export_dir else self.sessions_dir / card_id
        parts = rdir.name.split("-")
        kind = parts[0] if parts and parts[0] else "run"
        try:
            round_no = int(parts[-1]) if len(parts) > 1 else 1
        except ValueError:
            round_no = 1
        with self._lock:
            if card_id in self._registry:
                return
            # A worker round keeps the platform pinned on the card's session;
            # a review/resolver round is fresh, so its role's backend applies.
            pinned = session.get("backend") if kind == "run" else None
            entry = {"proc": _AdoptedProc(pid, rdir / "export" / "export.json"),
                     "kind": kind, "round": round_no,
                     "stop_requested": False, "adopted": True,
                     "backend": pinned or self._role_backend(kind).key,
                     "model": self._role_model(kind)}
            self._registry[card_id] = entry
        target = self._finalize_resolver if kind == "resolve" else self._finalize
        threading.Thread(target=target, args=(card_id,), daemon=True).start()

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Liveness probe that is safe on every OS (see _windows_pid_alive)."""
        if sys.platform == "win32":
            return _windows_pid_alive(pid)
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def backend(self) -> Backend:
        """The fallback agent CLI (vibe / claude) when no model is chosen.

        The platform now comes from the model choice (see `_role_backend`);
        `settings.backend` is only the internal fallback for a role that has no
        choice at all.
        """
        key = self.settings.backend() if self.settings else None
        return get_backend(key)

    def _build_cmd(self, prompt_path: Path, ws: Path, rdir: Path,
                   kind: str, resume: str | None = None,
                   backend: Backend | None = None,
                   mcp_config: Path | None = None,
                   model: str | None | object = _UNSET) -> list[str]:
        """The command line for one session. kind: 'run', 'review' or 'resolve'.

        `resume` (a session id from a previous round's export) resumes that
        session instead of starting a new one — used for fixer rounds so the
        persistent worker keeps its cached context. Reviewers are always fresh
        (no resume). The flags themselves belong to the backend, including how
        the role's model is passed (`--model` for Claude, nothing for Vibe).
        """
        backend = backend or self._role_backend(kind)
        if model is _UNSET:
            model = self._role_model(kind)
        return backend.build_cmd(config=self.config, prompt_path=prompt_path,
                                 ws=ws, rdir=rdir, kind=kind, resume=resume,
                                 mcp_config=mcp_config, model=model)

    def _claude_mcp_config(self, rdir: Path, card_id: str) -> Path:
        """Write the `--mcp-config` file that gives a Claude run the board tools.

        Vibe reads the same server from VIBE_MCP_SERVERS; Claude wants a JSON
        file with an `mcpServers` map. Same command, same env, so the tools
        behave identically whichever backend spawned the session.
        """
        config = {
            "mcpServers": {
                self.config.mcp_server_name: {
                    "command": sys.executable,
                    "args": [str(MCP_SERVER_PATH)],
                    "env": {
                        "VIBE_HARNESS_URL": self.config.api_base_url(),
                        "VIBE_HARNESS_NOTE_ID": card_id,
                        "VIBE_HARNESS_ASK_USER_TIMEOUT": str(self.config.ask_user_timeout_seconds),
                    },
                }
            }
        }
        path = rdir / "mcp.json"
        path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        return path

    def _watch_time_limit(self, card_id: str, proc) -> None:
        """Terminate a session that outlives `time_limit_seconds`.

        Only used for backends without a native time limit (Claude). Vibe
        enforces its own `--time-limit` and writes the export on the way out;
        here the harness has to stop the process, so the round ends as a
        timeout rather than a clean finish.
        """
        limit = max(1, int(self.config.time_limit_seconds))

        def watch() -> None:
            try:
                proc.wait(timeout=limit)
                return
            except subprocess.TimeoutExpired:
                pass
            with self._lock:
                entry = self._registry.get(card_id)
                if entry is not None and entry.get("proc") is proc:
                    entry["stop_requested"] = True
                    entry["timed_out"] = True
            self._terminate_session(proc, force=False)

        threading.Thread(target=watch, daemon=True).start()

    def _spawn(self, card_id: str, *, kind: str, prompt: str,
               extra_patch: dict | None = None,
               workdir: Path | None = None,
               resume: str | None = None) -> None:
        """Start one vibe session. kind: 'run' (implement/fix) or 'review'.

        The card transitions to 'running' or 'reviewing'; artifacts for this
        round live in sessions/<id>/<kind>-<n>/. extra_patch is merged into the
        session record (e.g. the previous run's outcome, or a review record).
        `workdir` overrides the effective work folder (the caller passes the
        card's linked git worktree for the first run of a git-backed card);
        otherwise it resolves via _resolve_ws, which prefers the linked
        worktree recorded in session.git for git-backed cards.

        `resume` is a vibe session id (from the previous run round's
        export.json): the session is resumed with `vibe --resume <id>` so the
        persistent worker keeps its context across fix rounds. Only worker
        ('run') rounds are ever resumed; reviewers are always fresh.
        """
        # One live session per card. A second spawn would overwrite the
        # registry entry: the first process would go unmanaged and its finalize
        # thread could never release the slot (the cap would silently shrink).
        # A sequential handover (run -> review -> fixer) is fine — the previous
        # process has already exited by then.
        with self._lock:
            existing = self._registry.get(card_id)
        if existing is not None and existing["proc"].poll() is None:
            raise CardRunning(f"card {card_id} already has a live session")
        card = self.store.get(card_id)
        sdir = self.sessions_dir / card_id
        sdir.mkdir(parents=True, exist_ok=True)
        round_no = self._next_round(card_id, kind)
        rdir = sdir / f"{kind}-{round_no}"
        rdir.mkdir(parents=True, exist_ok=True)

        prompt_path = rdir / "prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        if workdir is not None:
            # Explicit work folder (the card's linked worktree): a real work
            # folder, trusted for this invocation only.
            ws, trust_flags = Path(workdir), ["--trust"]
        else:
            ws, _is_sandbox, trust_flags = self._resolve_ws(card, use_worktree=True)

        # Per-card MCP mailbox: hand the spawned vibe process the mailbox dir,
        # this side's role, and this round's key; the stdio server (attached
        # via the worktree's .vibe/config.toml) inherits them.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("VIBE_HARNESS_MAILBOX")}
        if self.config.mailbox:
            mdir = mailbox.mailbox_dir(self.sessions_dir, card_id)
            if mdir.is_dir():
                round_key = f"{kind}-{round_no}"
                if kind == "review":
                    # A fresh review round must not inherit an earlier round's
                    # structured verdict: clear THIS round's verdict file
                    # before the reviewer process starts.
                    mailbox.clear_round_verdict(mdir, round_key)
                env.update({
                    "VIBE_HARNESS_MAILBOX_DIR": str(mdir),
                    "VIBE_HARNESS_MAILBOX_ROLE": ("worker" if kind == "run"
                                                  else "reviewer"),
                    "VIBE_HARNESS_MAILBOX_ROUND": round_key,
                    "VIBE_HARNESS_MAILBOX_SERVER": str(mailbox.SERVER_SCRIPT)})

        backend = self._session_backend(kind, card, resume)
        model = self._session_model(kind, card, resume)
        log_path = rdir / "session.log"
        log_file = open(log_path, "ab")
        try:
            # Run sessions (implement/fix) get the harness MCP server; review
            # and resolver sessions stay MCP-free. Vibe takes it through
            # VIBE_MCP_SERVERS, Claude through a --mcp-config file.
            mcp_config = None
            if kind == "run" and self.config.mcp_enabled:
                if backend.key == "claude":
                    mcp_config = self._claude_mcp_config(rdir, card_id)
                else:
                    run_env = self._session_env(card_id)
                    if env:
                        run_env.update(env)
                    env = run_env
            cmd = self._build_cmd(prompt_path, ws, rdir, kind, resume=resume,
                                  backend=backend, mcp_config=mcp_config,
                                  model=model) + trust_flags
            env.update(self._model_env(kind, backend, model))
            stdin_file = open(prompt_path, "rb") if backend.prompt_on_stdin else None
            try:
                proc = subprocess.Popen(
                    cmd, stdin=stdin_file, stdout=log_file,
                    stderr=subprocess.STDOUT, cwd=ws, env=env, **_detach_kwargs())
            finally:
                if stdin_file is not None:
                    stdin_file.close()
            if not backend.native_time_limit:
                # Claude has no --time-limit: enforce it here, the same way the
                # Stop button does, so a stuck session cannot run forever.
                self._watch_time_limit(card_id, proc)
        finally:
            # The child owns its own dup of the fd; drop the parent's copy.
            log_file.close()

        with self._lock:
            self._registry[card_id] = {"proc": proc, "stop_requested": False,
                                      "kind": kind, "round": round_no,
                                      "resumed": resume is not None,
                                      # The backend and model this round asked
                                      # for: the export only reports the model
                                      # that actually ran, which is exactly
                                      # what a rejected choice never produces.
                                      "backend": backend.key,
                                      "model": model,
                                      "live_tokens": None, "live_breakdown": None,
                                      "vibe_session_id": None,
                                      "usage_offsets": {}, "usage_scanned_at": 0.0}

        patch = {
            "pid": proc.pid,
            "started_at": now_iso(),
            "finished_at": None,
            "exit_code": None,
            "workdir": str(ws),
            "log": str(log_path),
            "export_dir": str(rdir / "export"),
        }
        if kind == "run":
            # Pin the platform for this card's worker session: a fixer round
            # resumes this session id, and a Vibe id cannot be resumed by
            # Claude (nor the other way round). Only a run writes it — a review
            # or resolver round on the other backend must not repin the worker.
            patch["backend"] = backend.key
        if kind == "review":
            rounds = (card.get("session") or {}).get("review_rounds") or 0
            patch["review_rounds"] = rounds + 1
        if extra_patch:
            patch.update(extra_patch)
        status = "reviewing" if kind == "review" else "running"
        self.store.set_status(card_id, status, session_patch=patch)

        threading.Thread(target=self._finalize, args=(card_id,), daemon=True).start()

    def _next_round(self, card_id: str, kind: str) -> int:
        """Per-kind session counter: run-1, review-1, run-2, review-2, ..."""
        sdir = self.sessions_dir / card_id
        highest = 0
        if sdir.is_dir():
            for child in sdir.iterdir():
                m = re.fullmatch(rf"{kind}-(\d+)", child.name)
                if m and child.is_dir():
                    highest = max(highest, int(m.group(1)))
        return highest + 1

    def _read_export(self, rdir: Path, backend: Backend | None = None) -> dict:
        """One round's output, normalized to the harness's export shape.

        Each backend knows where its own output lands (vibe: export/export.json,
        claude: the JSON result on stdout, teed into session.log) and how to map
        it onto `usage` / `session_id` / `config.active_model` / `stop_reason`.
        """
        return (backend or self.backend()).read_export(rdir)

    def _model_error(self, rdir: Path, model: str | None,
                     exit_code: int | None,
                     backend: Backend | None = None) -> str | None:
        """The backend's reason for refusing `model`, or None.

        A rejected model exits non-zero like any other crash, so the cause has
        to be read back out of the round's log: the round then fails naming the
        model instead of looking like a random crash — or, worse, like a
        success on the backend's own default model.
        """
        if not model or not exit_code:
            return None
        try:
            text = (rdir / "session.log").read_text(encoding="utf-8",
                                                    errors="replace")
        except OSError:
            return None
        return (backend or self.backend()).model_error(text, model)

    # -------------------------------------------------------------- finalize

    def _finalize(self, card_id: str) -> None:
        """Runs in a daemon thread: wait for exit, record the outcome, and
        drive the review loop.

        The session slot is released in a `finally`: a failure while recording
        the outcome (e.g. the card moved on and the store refuses the status
        write) must never leave a stale registry entry — that would shrink the
        concurrency cap for the rest of the process.
        """
        with self._lock:
            entry = self._registry.get(card_id)
        if entry is None:
            return
        try:
            self._record_outcome(card_id, entry)
        finally:
            self._release_slot(card_id, entry)

    def _record_outcome(self, card_id: str, entry: dict) -> None:
        """Record one finished session and drive the next pipeline step."""
        proc = entry["proc"]
        kind = entry["kind"]

        exit_code = proc.wait()
        card = self.store.get(card_id)
        sdir = self.sessions_dir / card_id
        rdir = sdir / f"{kind}-{entry['round']}"
        session = card.get("session") or {}
        ws = Path(session.get("workdir") or (sdir / "workspace"))
        # The last live total shown for the card, persisted as a floor so the
        # display does not dip between rounds (the registry entry is gone until
        # the next round's export lands).
        high_water = self._live_high_water(card)
        high_water_patch = ({"live_high_water": high_water}
                            if high_water is not None else {})

        # The round's own backend: the worker, the reviewer and the resolver
        # can each run on a different platform, and each backend writes its
        # output somewhere else.
        backend = get_backend(entry.get("backend"))
        export = self._read_export(rdir, backend)
        self._capture_transcript(card_id, f"{kind}-{entry['round']}", rdir)
        usage = export.get("usage") or export.get("token_usage") or {}
        tokens = usage.get("total_tokens", usage.get("total"))
        # Per-type breakdown (cached/output/input) for the token-counter hover
        # tooltip; None when the export carries only a total.
        token_breakdown = export_token_breakdown(usage) if isinstance(usage, dict) else None
        export_config = export.get("config") or export.get("effective_config") or {}
        model = export_config.get("active_model") or export_config.get("model")
        stop_reason = export.get("stop_reason")
        # A model the backend refused: the round failed for that reason, so say
        # so instead of reporting a generic crash (or a silent fallback).
        model_error = self._model_error(rdir, entry.get("model"), exit_code,
                                        backend)
        if model_error:
            stop_reason = "model_error"
        # The vibe session id — the resume key for the next fixer round.
        worker_session_id = export.get("session_id")

        if kind == "run":
            # --- test gate: run the test command in the workspace ---
            # None when no test command is configured, else (passed, output).
            tested = None
            if exit_code == 0 and self.config.test_cmd:
                tested = self._run_tests(ws)
            verified = None if tested is None else tested[0]

            outcome = {
                "finished_at": now_iso(),
                "exit_code": exit_code,
                "verified": verified,
                "model": model,
                "tokens": tokens,
                "stop_reason": stop_reason,
                **high_water_patch,
            }
            if token_breakdown is not None:
                outcome["token_breakdown"] = token_breakdown
            if model_error:
                outcome["model_error"] = model_error
            if worker_session_id:
                outcome["worker_session_id"] = worker_session_id
            if entry.get("timed_out"):
                # The harness watchdog killed it (a backend without a native
                # time limit): a timeout, not the user pressing Stop.
                outcome["timed_out"] = True
            if entry["stop_requested"]:
                self.store.set_status(card_id, "stopped", session_patch=outcome)
            elif exit_code != 0:
                self.store.set_status(card_id, "failed", session_patch=outcome)
            else:
                # Success -> commit the result on the card branch (when the
                # run is git-backed), run the test gate, then hand over to
                # the reviewer — or loop back to a fixer when tests fail.
                git_info = session.get("git")
                commit_error = None
                if git_info:
                    title = " ".join((card.get("title") or "card").split())[:80]
                    try:
                        gitops.commit_workspace(
                            Path(session.get("workdir") or ws),
                            f"vibe-harness: {title} (card {card_id})")
                    except GitError as exc:
                        commit_error = str(exc)
                    else:
                        # The growth signal: how much this card added and
                        # removed against its base. A 900-line card for a small
                        # spec is then visible on the board, not only in review.
                        counts = praccept.diff_stat_counts(git_info)
                        if counts is not None:
                            outcome["diff_stat"] = counts
                if commit_error:
                    self.store.set_status(card_id, "failed", session_patch={
                        **outcome, "git_error": commit_error})
                elif tested is not None and not tested[0]:
                    # Test-gate rejection: back to running, the fixer gets
                    # the test output as the rejection reason; the run
                    # outcome (verified=False, tokens, ...) is recorded too.
                    tail = tested[1][-4000:]
                    self._loop_reject(card_id, card,
                                      f"the test command failed:\n{tail}",
                                      source="test",
                                      session_patch={
                                          "test_output": tail,
                                          **{k: v for k, v in outcome.items()
                                             if k != "finished_at"},
                                      })
                else:
                    # Loop step 1: hand over to a FRESH reviewer, scoped to
                    # the diff since the last reviewed commit + the test
                    # output (small context, independent judgment).
                    handover = {k: v for k, v in outcome.items() if k != "finished_at"}
                    # review_diff=true: the diff since the last reviewed commit
                    # (small context); false: the WHOLE card diff
                    # (merge-base..HEAD). Sandbox runs still get (None, None).
                    diff_text, diff_range = self._review_diff(
                        session, ws, full=not self.config.review_diff)
                    test_tail = None
                    if tested is not None:
                        test_tail = tested[1][-REVIEW_TEST_OUTPUT_MAX_CHARS:]
                    self._spawn(card_id, kind="review",
                                prompt=review_prompt_template(card["body"],
                                                              diff_text, diff_range,
                                                              test_tail,
                                                              duplication_root=ws,
                                                              mailbox=self._prompt_mailbox(
                                                                  "review", card)),
                                extra_patch=handover)
        else:  # review
            # Verdict channel 1 (preferred): the structured `submit_verdict`
            # MCP tool call, recorded by the mailbox server as this round's
            # verdicts/review-<n>.json (reviewer role only). Reading ONLY this
            # round's file means a review round that never called
            # submit_verdict cannot inherit the previous round's verdict.
            verdict = None
            via = None
            mdir = mailbox.mailbox_dir(self.sessions_dir, card_id)
            if self.config.mailbox:
                structured = mailbox.read_verdict(
                    mdir, round=f"review-{entry['round']}")
                if structured is not None:
                    verdict, via = structured, "submit_verdict"
            # Fallback channel 2: VERDICT: text in the log / REVIEW.md.
            if verdict is None:
                logged = self._read_verdict(rdir, ws)
                if logged is not None:
                    verdict, via = logged, "log"
            if verdict is None:
                approved, reason = False, "reviewer did not produce a verdict"
            else:
                approved, reason = verdict
            review_record = {
                "approved": approved,
                "reason": reason,
                "round": entry["round"],
                "tokens": tokens,
                # The reviewer's model, shown next to the diff it judged.
                "model": model,
                "log": str(rdir / "session.log"),
                "export_dir": str(rdir / "export"),
                "via": via,
            }
            if verdict is None:
                # No verdict channel answered: recorded explicitly so the
                # failure classifier can tell "the reviewer said no" from "the
                # reviewer said nothing" without parsing the reason text.
                review_record["no_verdict"] = True
            if model_error:
                # The reviewer never ran: its model was refused. Recorded so
                # the round does not read as a reviewer that simply said no.
                review_record["model_error"] = model_error
            if token_breakdown is not None:
                review_record["token_breakdown"] = token_breakdown
            # Supervision bookkeeping: which commit this review covered (the
            # next review diffs from it) and how many mailbox messages were
            # exchanged so far.
            review_patch: dict = {**high_water_patch}
            head = self._reviewed_commit(session)
            if head:
                review_record["commit"] = head
                review_patch["last_reviewed_commit"] = head
            # Review history: every round's record is kept (session.review
            # stays the LAST one for backward compatibility), so the diff
            # panel can show what each round judged.
            review_patch["reviews"] = [*(session.get("reviews") or []),
                                       review_record]
            if self.config.mailbox and mdir.is_dir():
                review_record["messages"] = len(mailbox.list_messages(mdir))
            if entry["stop_requested"]:
                stopped_patch = {
                    "finished_at": now_iso(),
                    "exit_code": exit_code,
                    **high_water_patch,
                }
                if entry.get("timed_out"):
                    stopped_patch["timed_out"] = True
                self.store.set_status(card_id, "stopped", session_patch=stopped_patch)
            elif model_error:
                # The reviewer never ran: its model was refused. Retrying the
                # same model would fail identically, so fail the round now
                # instead of spending a fixer + review round per loop step.
                self.store.set_status(card_id, "failed", session_patch={
                    "review": review_record,
                    "finished_at": now_iso(),
                    **review_patch,
                })
            elif approved:
                # Loop step 2a: approved -> done, then auto-accept if asked.
                done = self.store.set_status(card_id, "done", session_patch={
                    "review": review_record,
                    "finished_at": now_iso(),
                    **review_patch,
                })
                if done.get("auto_accept"):
                    # Only the approved path ever auto-accepts, and never with
                    # force: an exhausted review loop is always the user's
                    # explicit call (POST .../accept {"force": true}).
                    try:
                        self.accept_card(card_id)
                    except Exception:
                        # Best effort: the user can still accept manually.
                        pass
            else:
                # Loop step 2b: rejected -> back to running; the fixer
                # session (RESUMED worker) receives the rejection reason (or
                # the loop is exhausted and the card lands in 'done' for
                # manual accept).
                self._loop_reject(card_id, card, reason or "",
                                  source="review", review_record=review_record,
                                  session_patch=review_patch)

    # ----------------------------------------------------------------- stop

    @staticmethod
    def _signal_session(pid: int, sig: int) -> None:
        """POSIX only: signal a session process — its process group when it was
        started detached (start_new_session => pid == pgid), else the pid
        itself (a re-adopted process after a server restart may not be a group
        leader). Never call this on Windows; use _terminate_session."""
        try:
            os.killpg(pid, sig)
            return
        except (ProcessLookupError, PermissionError):
            pass
        try:
            os.kill(pid, sig)
        except OSError:
            pass

    @staticmethod
    def _terminate_session(proc, *, force: bool) -> None:
        """Stop a session process on any platform.

        POSIX: SIGTERM (graceful — the session writes its export on the way
        out), escalating to SIGKILL. Windows has no signals: proc.terminate()/
        proc.kill() map to TerminateProcess for the spawned child, and
        `taskkill /PID <pid> /T /F` kills the whole detached tree — the only
        option for an adopted pid, which has no Popen handle.
        """
        if sys.platform == "win32":
            if hasattr(proc, "terminate"):
                try:
                    (proc.kill if force else proc.terminate)()
                except OSError:
                    pass
            try:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                               capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return
        SuperviseMixin._signal_session(
            proc.pid, signal.SIGKILL if force else signal.SIGTERM)

    def stop(self, card_id: str) -> None:
        with self._lock:
            entry = self._registry.get(card_id)
        if entry is None or entry["proc"].poll() is not None:
            raise NotRunningError(f"card {card_id} has no live session")
        entry["stop_requested"] = True
        proc = entry["proc"]
        # start_new_session=True makes proc.pid the process-group id for
        # sessions we spawned ourselves; an adopted session (registered by
        # pid after a restart) is signalled by pid when it leads no group.
        self._terminate_session(proc, force=False)

        def _escalate() -> None:
            time.sleep(STOP_GRACE_SECONDS)
            if proc.poll() is None:
                self._terminate_session(proc, force=True)

        threading.Thread(target=_escalate, daemon=True).start()
        # _finalize records status "stopped" once the process exits and drains
        # the queue there; drain here too so a stop that races an exit already
        # observed still releases the slot for the next queued card.
        try:
            self._drain_queued_runs()
        except Exception:
            pass  # never let the queue drain break a stop

    # ---------------------------------------------------------------- status

    def delete_card(self, card_id: str) -> None:
        """Delete a card: remove its linked git worktree (if any), then the
        card file and its sessions dir. Raises CardNotFound (404) / CardRunning
        (409) like the store. The card's branch (if still unmerged) is kept —
        recover unmerged work manually."""
        card = self.store.get(card_id)
        self.store.delete(card_id)
        git_info = (card.get("session") or {}).get("git") or {}
        if git_info.get("worktree"):
            try:
                gitops.remove_card_worktree(git_info)
            except GitError:
                pass  # best effort; "git worktree prune" cleans up later
        shutil.rmtree(self.sessions_dir / card_id, ignore_errors=True)
        self.questions.drop(card_id)

    def is_running(self, card_id: str) -> bool:
        with self._lock:
            entry = self._registry.get(card_id)
        return entry is not None and entry["proc"].poll() is None

    # ---------------------------------------------------------- live activity

    def activity(self, card_id: str, cursor: int = 0,
                 limit: int = activity.DEFAULT_LIMIT) -> dict:
        """The card's live activity feed: what the model is doing right now.

        `cursor` is the number of journal lines the client already consumed
        (0 = the last `limit` events). The journal is the vibe session's, found
        the same way live token usage finds it: the live registry entry's pid
        maps to the session, and a card with no live session falls back to its
        last worker session. A card with neither, or a journal that is gone,
        is an empty feed — never an error (see app/activity.py).
        """
        card = self.store.get(card_id)      # CardNotFound -> 404
        session_id = self._activity_session_id(card)
        if not session_id:
            return {"cursor": 0, "events": []}
        journal = (liveusage.vibe_home() / "logs" / "session" / "unified"
                   / session_id / "journal")
        return activity.read_activity(journal, cursor=cursor, limit=limit)

    def _activity_session_id(self, card: dict) -> str | None:
        """The vibe session whose journal carries this card's activity.

        The live session wins (its journal is being appended right now); a
        card with no live registry entry falls back to the worker session
        recorded when the last round ended.
        """
        with self._lock:
            entry = self._registry.get(card["id"])
        if entry is not None:
            session_id = entry.get("vibe_session_id")
            proc = entry.get("proc")
            if not session_id and proc is not None:
                try:
                    session_id = liveusage.find_session_id_by_pid(
                        liveusage.vibe_home(), proc.pid)
                except Exception:
                    session_id = None
                if session_id:
                    entry["vibe_session_id"] = session_id
            if session_id:
                return session_id
        return (card.get("session") or {}).get("worker_session_id")

    # ------------------------------------------------- steering (user -> worker)

    def messages(self, card_id: str) -> list[dict]:
        """The card's mailbox conversation, oldest first (both directions).

        The popup shows the user <-> worker exchange; reviewer <-> worker
        traffic is internal and filtered out client-side.
        """
        self.store.get(card_id)      # CardNotFound -> 404
        return mailbox.list_messages(mailbox.mailbox_dir(self.sessions_dir, card_id))

    def post_message(self, card_id: str, text: str) -> dict:
        """Append the user's guidance to the card's mailbox, addressed to the
        worker. The worker's next `wait_for_message(timeout_seconds=0)` returns
        it. Empty text is a ValueError (the API maps it to 400)."""
        self.store.get(card_id)      # CardNotFound -> 404
        return mailbox.post_message(
            mailbox.mailbox_dir(self.sessions_dir, card_id), "user", "worker", text)

    def recover_stale(self) -> None:
        """Startup: reconcile cards after a dashboard restart/reload (kill -9
        included) — no stuck cards, no orphaned worktrees.

        - A live card (running/reviewing) whose session process is still
          alive is RE-ADOPTED: the in-memory registry is gone after a reload,
          so we register a pid shim and a finalize thread — otherwise the
          card would be orphaned (dead pid, stuck 'running') when the process
          eventually exits. stop() works for adopted sessions by pid. Only
          cards with a dead pid are failed. Pending questions are cancelled in
          both cases (their long-poll waiter died with the old server).
        - A `done` card still flagged git.resolving (resolver died with the
          server): a live resolver pid is re-adopted; a dead one whose merge
          already concluded (MERGE_HEAD gone, tree clean) finishes the accept
          automatically; an open conflict (MERGE_HEAD set) is left for a
          resolver.
        - Orphaned per-card worktrees/branches of NON-live cards are pruned:
          the linked worktree is removed (``git worktree remove --force`` +
          prune) and the card branch is deleted only when already merged
          (safe ``git branch -d``) — unmerged work is never force-deleted.
          ACCEPTED cards are skipped: their branch and worktree are what the
          diff view reads, and they go away only on delete/archive. That
          includes a card accepted as a pull request: its branch and worktree
          are the working copy for follow-up commits on the PR, and the ``-u``
          push sets the branch's upstream to ``origin/<branch>`` — which makes
          ``git branch -d`` consider it merged and succeed, so the safe-delete
          guard alone would not save it.
        """
        for card in self.store.list():
            status = card["status"]
            session = card.get("session") or {}
            try:
                if status in LIVE_STATUSES:
                    pid = session.get("pid")
                    if pid and self._pid_alive(pid):
                        self._adopt_session(card, pid)
                        self._cancel_questions(card["id"])
                        continue
                    git_info = session.get("git") or {}
                    if git_info.get("base_branch"):
                        # Best effort: leave the main worktree on its base branch
                        # (runs from before the worktree-per-card refactor checked
                        # the card branch out there; now the main worktree never
                        # moves and the card branch lives in its linked worktree).
                        try:
                            gitops.run_git(["checkout", "-q", git_info["base_branch"]],
                                           Path(git_info["repo"]), check=False)
                        except OSError:
                            pass
                    self.store.set_status(card["id"], "failed", session_patch={
                        "finished_at": now_iso(),
                        "exit_code": None,
                    })
                    self._cancel_questions(card["id"])
                    continue
                if status == "done" and (session.get("git") or {}).get("resolving"):
                    self._recover_resolving(card)
            except Exception:
                # One broken card (e.g. its repo folder was deleted while the
                # server was down) must never crash startup recovery.
                pass
        # Prune orphaned per-card worktrees/branches of cards that are no
        # longer live (nothing manages them after a restart). Cards still
        # resolving keep their worktree/branch — a resolver owns them — and so
        # does a card accepted as a pull request: the PR may need follow-up
        # commits, and its `-u` push makes `git branch -d` succeed (the branch
        # looks "merged" to its upstream), so pruning would delete it.
        for card in self.store.list():
            if card["status"] in LIVE_STATUSES:
                continue
            session = card.get("session") or {}
            git_info = session.get("git") or {}
            if not git_info.get("worktree") or not git_info.get("repo"):
                continue
            if card["status"] == "done" and git_info.get("resolving"):
                continue
            if (card["status"] == "accepted" or git_info.get("merged")
                    or session.get("pr_url") or session.get("pr_opened_at")):
                # An accepted card keeps its branch and worktree: they are
                # what the diff view reads (and, for a PR accept, the working
                # copy for follow-up commits). They go away on delete/archive.
                continue
            try:
                gitops.remove_card_worktree(git_info)
                branch = git_info.get("branch")
                root = Path(git_info["repo"])
                if branch and gitops.branch_exists(root, branch):
                    # Safe delete only: fails (and keeps the branch) when the
                    # card's work is not merged yet.
                    gitops.run_git(["branch", "-q", "-d", branch], root,
                                   check=False)
            except Exception:
                pass  # best effort: one broken card/repo must not break startup
        # A merge conflict may have been resolved while the dashboard was
        # down: start any runs that were queued behind it.
        try:
            self._drain_queued_runs()
        except Exception:
            pass  # never let the queue drain break startup recovery
        # Dependencies completed while the dashboard was down: start the
        # drafts that were waiting on them.
        try:
            self._drain_dependents()
        except Exception:
            pass  # never let the dependency drain break startup recovery
        # An "Accept all" batch was interrupted by the restart: the cards are
        # still flagged accept_queued, so pick the batch back up.
        try:
            if any((c.get("session") or {}).get("accept_queued")
                   for c in self.store.list()):
                self._start_accept_drain()
        except Exception:
            pass  # never let the accept drain break startup recovery

    def log_tail(self, card_id: str, n: int = 200) -> str:
        card = self.store.get(card_id)
        session = card.get("session") or {}
        log_path = session.get("log")
        if not log_path:
            return ""
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                last = deque((line.rstrip("\r\n") for line in f), maxlen=n)
        except OSError:
            return ""
        return "\n".join(last)

