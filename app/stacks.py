"""StackStore: named piles of cards ("decks") on the board.

A stack groups cards that sit in the same column, with an optional header.
Stacks used to live only in the browser's localStorage, which meant an LLM
could neither see nor change them; they are server state now (stacks.json) so
the MCP tools can stack cards and move them between decks.

Layout of stacks.json:

    {"stacks": [{"id": "st-1a2b3c4d", "col": "accepted",
                 "title": "Merge queue",
                 "ids": ["<card id>", ...],   # bottom -> top
                 "created_at": "...", "updated_at": "..."}]}

Invariants (enforced here, mirrored by the board's renderer):

- every member renders in the stack's column;
- a card is in at most one stack;
- a stack needs at least two members — dropping below that dissolves it.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from app.cards import CardNotFound, CardStore

# Which board column a status renders in. Mirrors COLUMNS in app/static/app.js:
# "stopped" cards sit in the Failed column.
COLUMN_OF_STATUS = {
    "draft": "draft",
    "running": "running",
    "reviewing": "reviewing",
    "done": "done",
    "accepted": "accepted",
    "failed": "failed",
    "stopped": "failed",
}
COLUMNS = ("draft", "running", "reviewing", "done", "accepted", "failed")

MIN_MEMBERS = 2


class StackError(Exception): ...        # maps to HTTP 400
class StackNotFound(StackError): ...    # maps to HTTP 404


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return "st-" + secrets.token_hex(4)


class StackStore:
    def __init__(self, path: Path, cards: CardStore):
        self.path = Path(path)
        self.cards = cards
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        stacks = raw.get("stacks") if isinstance(raw, dict) else raw
        if not isinstance(stacks, list):
            return []
        out = []
        for s in stacks:
            if not isinstance(s, dict):
                continue
            ids = [i for i in (s.get("ids") or []) if isinstance(i, str)]
            col = s.get("col")
            if not isinstance(s.get("id"), str) or col not in COLUMNS or not ids:
                continue
            out.append({
                "id": s["id"],
                "col": col,
                "title": str(s.get("title") or ""),
                "ids": ids,
                "created_at": s.get("created_at") or _now(),
                "updated_at": s.get("updated_at") or _now(),
            })
        return out

    def _save(self, stacks: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(json.dumps({"stacks": stacks}, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    def _card_column(self, card_id: str) -> str:
        try:
            card = self.cards.get(card_id)
        except CardNotFound:
            raise StackError(f"card {card_id!r} not found")
        col = COLUMN_OF_STATUS.get(card.get("status"))
        if col is None:
            raise StackError(f"card {card_id!r} has no board column "
                             f"(status {card.get('status')!r})")
        return col

    def _check_members(self, ids: list[str], col: str) -> list[str]:
        """Dedupe, require >= MIN_MEMBERS, and require every card in `col`."""
        seen: list[str] = []
        for nid in ids:
            if not isinstance(nid, str):
                raise StackError("card ids must be strings")
            if nid not in seen:
                seen.append(nid)
        if len(seen) < MIN_MEMBERS:
            raise StackError(f"a stack needs at least {MIN_MEMBERS} cards")
        for nid in seen:
            actual = self._card_column(nid)
            if actual != col:
                raise StackError(f"card {nid!r} is in the {actual!r} column, "
                                 f"not {col!r} — a stack holds cards from one column")
        return seen

    def _prune(self, stacks: list[dict]) -> tuple[list[dict], bool]:
        """Drop members that vanished or changed column, and dissolve piles that
        fell below two cards. Returns (stacks, changed)."""
        by_id = {n["id"]: n for n in self.cards.list()}
        seen: set[str] = set()
        out: list[dict] = []
        for s in stacks:
            ids = []
            for nid in s["ids"]:
                card = by_id.get(nid)
                if card is None or nid in seen:
                    continue
                if COLUMN_OF_STATUS.get(card.get("status")) != s["col"]:
                    continue
                seen.add(nid)
                ids.append(nid)
            if len(ids) >= MIN_MEMBERS:
                out.append(dict(s, ids=ids))
        changed = json.dumps(out, sort_keys=True) != json.dumps(stacks, sort_keys=True)
        return out, changed

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    def list(self) -> list[dict]:
        with self._lock:
            stacks = self._load()
            pruned, changed = self._prune(stacks)
            if changed:
                self._save(pruned)
            return pruned

    def get(self, stack_id: str) -> dict:
        for s in self.list():
            if s["id"] == stack_id:
                return s
        raise StackNotFound(f"stack {stack_id!r} not found")

    def find_for_card(self, card_id: str) -> dict | None:
        for s in self.list():
            if card_id in s["ids"]:
                return s
        return None

    def column_of(self, card_id: str) -> str:
        """The board column a card renders in (raises StackError if unknown)."""
        return self._card_column(card_id)

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    def create(self, col: str, title: str, card_ids: list[str]) -> dict:
        if col not in COLUMNS:
            raise StackError(f"unknown column {col!r} (expected one of {', '.join(COLUMNS)})")
        ids = self._check_members(card_ids, col)
        with self._lock:
            stacks = self._load()
            # A card belongs to one stack only: take the new members out of
            # whatever pile they were in (dissolving piles that fall below two).
            stacks = self._detach(stacks, set(ids))
            now = _now()
            stack = {"id": _new_id(), "col": col, "title": (title or "").strip(),
                     "ids": ids, "created_at": now, "updated_at": now}
            stacks.append(stack)
            self._save(stacks)
            return stack

    def update(self, stack_id: str, *, title: str | None = None,
               card_ids: list[str] | None = None, col: str | None = None) -> dict:
        with self._lock:
            stacks = self._load()
            stack = next((s for s in stacks if s["id"] == stack_id), None)
            if stack is None:
                raise StackNotFound(f"stack {stack_id!r} not found")
            if col is not None and col != stack["col"]:
                if col not in COLUMNS:
                    raise StackError(f"unknown column {col!r}")
                stack["col"] = col
            if card_ids is not None:
                stack["ids"] = self._check_members(card_ids, stack["col"])
                stacks = self._detach(stacks, set(stack["ids"]), keep=stack_id)
                stack = next(s for s in stacks if s["id"] == stack_id)
            if title is not None:
                stack["title"] = title.strip()
            stack["updated_at"] = _now()
            self._save(stacks)
            return stack

    def delete(self, stack_id: str) -> dict:
        with self._lock:
            stacks = self._load()
            stack = next((s for s in stacks if s["id"] == stack_id), None)
            if stack is None:
                raise StackNotFound(f"stack {stack_id!r} not found")
            self._save([s for s in stacks if s["id"] != stack_id])
            return {"deleted": stack_id, "cards": stack["ids"]}

    def add_cards(self, stack_id: str, card_ids: list[str]) -> dict:
        with self._lock:
            stacks = self._load()
            stack = next((s for s in stacks if s["id"] == stack_id), None)
            if stack is None:
                raise StackNotFound(f"stack {stack_id!r} not found")
            for nid in card_ids:
                if not isinstance(nid, str):
                    raise StackError("card ids must be strings")
                actual = self._card_column(nid)
                if actual != stack["col"]:
                    raise StackError(f"card {nid!r} is in the {actual!r} column, "
                                     f"not {stack['col']!r}")
            stacks = self._detach(stacks, set(card_ids), keep=stack_id)
            stack = next(s for s in stacks if s["id"] == stack_id)
            for nid in card_ids:
                if nid not in stack["ids"]:
                    stack["ids"].append(nid)
            stack["updated_at"] = _now()
            self._save(stacks)
            return stack

    def remove_card(self, card_id: str) -> dict:
        """Pull one card out of its stack. Dissolves a pile that falls below two."""
        with self._lock:
            stacks = self._load()
            stack = next((s for s in stacks if card_id in s["ids"]), None)
            if stack is None:
                raise StackNotFound(f"card {card_id!r} is not in a stack")
            stack["ids"] = [i for i in stack["ids"] if i != card_id]
            stack["updated_at"] = _now()
            dissolved = len(stack["ids"]) < MIN_MEMBERS
            if dissolved:
                stacks = [s for s in stacks if s["id"] != stack["id"]]
            self._save(stacks)
            return {"stack": None if dissolved else stack,
                    "dissolved": dissolved, "card_id": card_id}

    def stack_cards(self, card_ids: list[str], *, stack_id: str | None = None,
                    title: str | None = None, col: str | None = None) -> dict:
        """Put cards into a stack, creating one when no stack_id is given.

        This is the "move cards around" entry point: a card added to a stack
        leaves whatever pile it was in.
        """
        if stack_id:
            return self.add_cards(stack_id, card_ids)
        if not card_ids:
            raise StackError("card_ids is required when creating a stack")
        target_col = col or self._card_column(card_ids[0])
        return self.create(target_col, title or "", card_ids)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _detach(self, stacks: list[dict], ids: set[str], keep: str | None = None) -> list[dict]:
        """Remove `ids` from every stack except `keep`; dissolve emptied piles."""
        out = []
        for s in stacks:
            if s["id"] == keep:
                out.append(s)
                continue
            remaining = [i for i in s["ids"] if i not in ids]
            if remaining == s["ids"]:
                out.append(s)
            elif len(remaining) >= MIN_MEMBERS:
                out.append(dict(s, ids=remaining, updated_at=_now()))
            # else: the pile fell below two cards — drop it
        return out
