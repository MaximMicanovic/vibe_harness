"""QuestionStore: per-card Q&A between a spawned model and the dashboard user.

A run session's model can call the harness MCP tool ``harness_ask_user`` when
the spec is ambiguous and a wrong guess would be costly. The question is shown
on the card in the dashboard; the tool call blocks until the user
answers on the card (or cancels, or the timeout hits) and returns the answer
to the model, which then continues.

State is file-backed (one ``sessions/<card_id>/questions.json`` per card,
atomic writes like CardStore) so it survives a dashboard restart, plus an
in-memory copy guarded by a Condition so the long-poll ``wait`` endpoint can
block efficiently instead of busy-polling the file.

Record shape::

    {
      "id": "a1b2c3",
      "question": "Should X be sync or async?",
      "options": ["sync", "async"] | None,
      "asked_at": "2025-01-01T12:00:00+00:00",
      "status": "pending" | "answered" | "cancelled",
      "answer": "async" | None,          # when answered
      "answered_at": ... | None,
      "answered_by": "user" | None,
      "cancelled_at": ... | None,         # when cancelled
    }

The runner cancels any still-pending questions when a session ends (the model
process is gone, so nobody would ever read the answer) and drops the state when
the card is deleted.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

PENDING = "pending"
ANSWERED = "answered"
CANCELLED = "cancelled"
STATUSES = (PENDING, ANSWERED, CANCELLED)

# Keep at most this many questions per card (oldest answered/cancelled trimmed).
MAX_QUESTIONS_PER_NOTE = 50


class QuestionError(Exception):
    """Bad question request (maps to HTTP 400)."""


class QuestionNotFound(QuestionError):
    """Unknown card or question id (maps to HTTP 404)."""


class QuestionNotPending(QuestionError):
    """The question is already answered/cancelled (maps to HTTP 409)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


class QuestionStore:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = Path(sessions_dir)
        # card_id -> list of question records (newest last); the Condition
        # wakes long-poll waiters when any record changes.
        self._cond = threading.Condition()
        self._cache: dict[str, list[dict]] = {}

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _path(self, card_id: str) -> Path:
        return self.sessions_dir / card_id / "questions.json"

    def _load(self, card_id: str) -> list[dict]:
        with self._cond:
            if card_id in self._cache:
                return list(self._cache[card_id])
        path = self._path(card_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            questions = data.get("questions", []) if isinstance(data, dict) else []
        except (OSError, ValueError):
            questions = []
        questions = [q for q in questions if isinstance(q, dict) and "id" in q]
        with self._cond:
            self._cache[card_id] = list(questions)
            return list(questions)

    def _save(self, card_id: str, questions: list[dict]) -> None:
        with self._cond:
            self._cache[card_id] = list(questions)
            self._cond.notify_all()
        _atomic_write(self._path(card_id), {"questions": questions})

    # ------------------------------------------------------------------
    # public interface
    # ------------------------------------------------------------------

    def ask(self, card_id: str, question: str, options: list[str] | None = None) -> dict:
        """Register a new pending question for a card; returns the record."""
        question = (question or "").strip()
        if not question:
            raise QuestionError("question must be a non-empty string")
        if len(question) > 4000:
            raise QuestionError("question is too long (max 4000 chars)")
        if options is not None:
            if not isinstance(options, list) or not (1 <= len(options) <= 8) \
                    or not all(isinstance(o, str) and o.strip() for o in options):
                raise QuestionError("options must be a list of 1..8 non-empty strings")
            options = [o.strip() for o in options]
        record = {
            "id": secrets.token_hex(3),
            "question": question,
            "options": options,
            "asked_at": _now(),
            "status": PENDING,
            "answer": None,
            "answered_at": None,
            "answered_by": None,
            "cancelled_at": None,
        }
        with self._cond:
            questions = self._load(card_id)
            questions.append(record)
            # Trim oldest resolved questions so the file cannot grow forever.
            resolved = [q for q in questions if q.get("status") != PENDING]
            if len(resolved) > MAX_QUESTIONS_PER_NOTE:
                drop = resolved[:len(resolved) - MAX_QUESTIONS_PER_NOTE]
                drop_ids = {q["id"] for q in drop}
                questions = [q for q in questions if q["id"] not in drop_ids]
            self._save(card_id, questions)
            return dict(record)

    def list(self, card_id: str) -> list[dict]:
        """All questions for a card, oldest first."""
        return [dict(q) for q in self._load(card_id)]

    def get(self, card_id: str, question_id: str) -> dict:
        for q in self._load(card_id):
            if q.get("id") == question_id:
                return dict(q)
        raise QuestionNotFound(f"card {card_id!r} has no question {question_id!r}")

    def pending(self, card_id: str) -> dict | None:
        """The oldest still-pending question, if any."""
        for q in self._load(card_id):
            if q.get("status") == PENDING:
                return dict(q)
        return None

    def answer(self, card_id: str, question_id: str, answer: str,
               by: str = "user") -> dict:
        """Record the user's answer; wakes any long-poll waiter."""
        answer = (answer or "").strip()
        if not answer:
            raise QuestionError("answer must be a non-empty string")
        with self._cond:
            questions = self._load(card_id)
            for q in questions:
                if q.get("id") == question_id:
                    if q.get("status") != PENDING:
                        raise QuestionNotPending(
                            f"question {question_id!r} is already {q.get('status')}")
                    q.update({"status": ANSWERED, "answer": answer,
                              "answered_at": _now(), "answered_by": by})
                    self._save(card_id, questions)
                    return dict(q)
            raise QuestionNotFound(f"card {card_id!r} has no question {question_id!r}")

    def cancel(self, card_id: str, question_id: str | None = None) -> list[dict]:
        """Cancel one question (question_id given) or all pending ones.

        Returns the cancelled records. Used by the user (dismiss on the card)
        and by the runner when a session ends. Cancelling is idempotent for
        already-resolved questions when cancelling all (they are skipped).
        """
        with self._cond:
            questions = self._load(card_id)
            cancelled = []
            for q in questions:
                if q.get("id") == question_id or (question_id is None
                                                  and q.get("status") == PENDING):
                    if question_id is not None and q.get("status") != PENDING:
                        raise QuestionNotPending(
                            f"question {question_id!r} is already {q.get('status')}")
                    if q.get("status") == PENDING:
                        q.update({"status": CANCELLED, "cancelled_at": _now()})
                        cancelled.append(dict(q))
            if question_id is not None and not cancelled:
                raise QuestionNotFound(
                    f"card {card_id!r} has no pending question {question_id!r}")
            if cancelled:
                self._save(card_id, questions)
            return cancelled

    def wait(self, card_id: str, question_id: str, timeout: float) -> dict:
        """Block until the question is answered/cancelled or `timeout` seconds
        pass; returns the record (still 'pending' on timeout). Powers the
        long-poll endpoint the MCP ask_user tool waits on."""
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                record = None
                for q in self._load(card_id):
                    if q.get("id") == question_id:
                        record = dict(q)
                        break
                if record is None:
                    raise QuestionNotFound(
                        f"card {card_id!r} has no question {question_id!r}")
                if record.get("status") != PENDING:
                    return record
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return record
                self._cond.wait(remaining)

    def drop(self, card_id: str) -> None:
        """Forget a card's in-memory state (the file is removed with the
        sessions dir on card delete)."""
        with self._cond:
            self._cache.pop(card_id, None)
