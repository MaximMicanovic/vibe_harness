"""CardStore: CRUD over markdown files with YAML frontmatter."""
from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

STATUSES = ("draft", "running", "reviewing", "done", "failed", "stopped", "accepted")

# Allowed status transitions. Anything not listed here is
# rejected by set_status() with InvalidTransition. The review loop drives
# running -> reviewing -> done (approved) or -> running (rejected, fixer).
TRANSITIONS = {
    "draft": {"running"},
    # "running" -> "running": a test-gate rejection spawns the next fixer
    # round while the card is still in the running state.
    "running": {"running", "done", "failed", "stopped", "reviewing"},
    "reviewing": {"done", "running", "failed", "stopped"},
    "done": {"accepted"},
    "failed": set(),
    "stopped": set(),
    # accepted -> draft: reverting an accepted card reopens it (the merge
    # commit is undone and the card goes back to the board as a draft).
    "accepted": {"draft"},
}

# Editing a card in one of these statuses resets it to draft (a re-run starts
# fresh: session cleared, accepted cleared).
FINISHED_STATUSES = ("done", "failed", "stopped", "accepted")

# A card in one of these statuses has a live vibe session: it cannot be edited,
# deleted, or reset.
LIVE_STATUSES = ("running", "reviewing")

# Statuses that satisfy a dependency: a card waiting on another card (via
# `depends_on`) is run automatically once every dependency is MERGED
# ("accepted"). "done" — approved by the reviewer but not yet merged — does
# NOT satisfy a dependency: the dependent must build on the merged result, not
# on a branch that may still be rejected or reverted. failed / stopped do not
# satisfy a dependency either — the dependent stays draft.
COMPLETED_STATUSES = ("accepted",)

# Keys of the session dict. A missing session is materialized with these keys
# set to None before a session_patch is merged in.
SESSION_KEYS = (
    "pid", "started_at", "finished_at", "exit_code", "verified",
    "model", "tokens", "cost_usd", "stop_reason", "workdir", "log", "export_dir",
    "review", "reviews", "review_rounds", "git", "resolver", "test_output",
    "mailbox", "worker_session_id", "last_reviewed_commit",
    # The growth signal: lines added / removed by the card branch against its
    # base, recorded when a run finalizes ({"files", "added", "removed"}).
    "diff_stat",
    # Revert bookkeeping: when an accepted card's merge commit was undone and
    # which commit undid it (the card is a draft again, the session is kept).
    "reverted_at", "revert_commit",
    # PR accept (accept_mode "pr"): the pull request the card shipped as.
    "pr_url", "pr_number", "pr_opened_at",
)


class CardError(Exception): ...
class CardNotFound(CardError): ...      # maps to HTTP 404
class CardRunning(CardError): ...       # maps to HTTP 409
class InvalidTransition(CardError): ... # maps to HTTP 409
class DependencyError(CardError): ...   # maps to HTTP 409 (cycle / self-link)
class CardConflict(CardError): ...      # maps to HTTP 409 (stale edit)


def _slugify(title: str) -> str:
    """URL-safe slug: lowercase, runs of non-alphanumeric collapsed to '-'."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug or "card"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def card_from_post(post: frontmatter.Post) -> dict:
    """Serialize a card file (frontmatter + body) to its dict representation.
    Shared by CardStore (board cards) and ArchiveStore (archived cards)."""
    md = post.metadata
    return {
        "id": md["id"],
        "title": md["title"],
        "status": md["status"],
        "created_at": md["created_at"],
        "updated_at": md["updated_at"],
        "template": md.get("template"),
        "auto_run": bool(md.get("auto_run", False)),
        "auto_accept": bool(md.get("auto_accept", False)),
        "accepted": bool(md.get("accepted", False)),
        "project": md.get("project"),
        "workdir": md.get("workdir"),
        "depends_on": [str(d) for d in (md.get("depends_on") or [])],
        # The card this one was split out of (None for a top-level card). A
        # worker that splits its spec into sub-cards makes them children of the
        # card it is working on, so the board can show the tree.
        "parent_id": (str(md["parent_id"]) if md.get("parent_id") else None),
        # Per-card model override: this card's own sessions run on it instead
        # of the role's model. None = inherit the role (or the user's own).
        "model": (str(md["model"]) if md.get("model") else None),
        "session": md.get("session"),
        # archive bookkeeping (set when the card lives in an archive folder)
        "archived": bool(md.get("archived", False)),
        "archive": md.get("archive"),
        "archived_at": md.get("archived_at"),
        "body": post.content,
    }


class CardStore:
    def __init__(self, cards_dir: Path):
        self.cards_dir = Path(cards_dir)
        self._migrate_legacy_dir()
        self.cards_dir.mkdir(parents=True, exist_ok=True)

    def _migrate_legacy_dir(self) -> None:
        """One-time: the board's cards used to live in `notes/`.

        Moving the files keeps every card, its id and its session history — the
        ids live in the frontmatter, not in the path. Idempotent, and a no-op
        for a fresh board (and for tests, whose temp dirs have no `notes`
        sibling). An empty `cards/` left behind by an earlier start is filled
        rather than treated as "already migrated".
        """
        legacy = self.cards_dir.parent / "notes"
        if not legacy.is_dir():
            return
        if self.cards_dir.is_dir() and any(self.cards_dir.glob("*.md")):
            return
        self.cards_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(legacy.glob("*.md")):
            try:
                os.replace(path, self.cards_dir / path.name)
            except OSError:
                shutil.copy2(path, self.cards_dir / path.name)

    # ------------------------------------------------------------------
    # serialization helpers
    # ------------------------------------------------------------------

    def _card_from_post(self, post: frontmatter.Post) -> dict:
        return card_from_post(post)

    def _write(self, path: Path, body: str, metadata: dict) -> None:
        # Atomic write (temp file + os.replace): the runner's finalize threads
        # write from multiple threads while readers poll — a torn read would
        # make the card look missing (CardNotFound). The temp name does not
        # match the *.md glob used by _find_path/list.
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
        tmp.write_text(frontmatter.dumps(frontmatter.Post(body, **metadata)),
                       encoding="utf-8")
        os.replace(tmp, path)

    def _find_path(self, card_id: str) -> Path | None:
        """Locate a card's file by the id in its frontmatter (not the filename)."""
        for path in sorted(self.cards_dir.glob("*.md")):
            try:
                post = frontmatter.load(path)
            except Exception:
                continue
            if str(post.metadata.get("id")) == card_id:
                return path
        return None

    def _get_post(self, card_id: str) -> frontmatter.Post:
        path = self._find_path(card_id)
        if path is None:
            raise CardNotFound(f"no card with id {card_id!r}")
        return frontmatter.load(path)

    # ------------------------------------------------------------------
    # public interface
    # ------------------------------------------------------------------

    def list(self) -> list[dict]:
        """All cards, most recently updated first. Unparseable files are skipped."""
        cards = []
        for path in self.cards_dir.glob("*.md"):
            try:
                cards.append(self._card_from_post(frontmatter.load(path)))
            except Exception:
                continue
        cards.sort(key=lambda n: n["updated_at"], reverse=True)
        return cards

    def get(self, card_id: str) -> dict:
        """Return one card; raises CardNotFound if absent."""
        return self._card_from_post(self._get_post(card_id))

    def create(self, title: str, body: str, template: str | None = None,
               auto_run: bool = False, auto_accept: bool = False,
               project: str | None = None,
               workdir: str | None = None,
               parent_id: str | None = None,
               model: str | None = None) -> dict:
        card_id = secrets.token_hex(3)
        now = _now()
        if parent_id:
            self._check_parent(card_id, parent_id)
        metadata = {
            "id": card_id,
            "title": title,
            "status": "draft",
            "created_at": now,
            "updated_at": now,
            "template": template,
            "auto_run": auto_run,
            "auto_accept": auto_accept,
            "accepted": False,
            "project": project,
            "workdir": workdir,
            "depends_on": [],
            "parent_id": parent_id,
            "model": (model or None),
            "session": None,
        }
        path = self.cards_dir / f"{_slugify(title)}-{card_id}.md"
        self._write(path, body, metadata)
        return self.get(card_id)

    def update(self, card_id: str, *, title: str | None = None,
               body: str | None = None, auto_run: bool | None = None,
               auto_accept: bool | None = None,
               project: str | None = None, workdir: str | None = None,
               parent_id: str | None = None,
               model: str | None = None,
               expected_updated_at: str | None = None) -> dict:
        post = self._get_post(card_id)
        card = self._card_from_post(post)
        if card["status"] in LIVE_STATUSES:
            raise CardRunning(f"card {card_id!r} is running and cannot be edited")
        # Optimistic concurrency: a caller that names the version it loaded
        # (the editor does) is refused when somebody else wrote the card in
        # between, instead of silently overwriting their edit. Callers that
        # send nothing (MCP tools, scripts) keep last-write-wins.
        if expected_updated_at is not None and \
                expected_updated_at != card["updated_at"]:
            raise CardConflict(
                f"this card changed since you opened it "
                f"(card {card_id!r} was updated at {card['updated_at']}, "
                f"you opened {expected_updated_at})")

        md = dict(post.metadata)
        # Only content edits (title/body) reset a finished card; changing
        # settings (auto_run/auto_accept/project/workdir) does not.
        changed = (title is not None and title != card["title"]) or \
                  (body is not None and body != card["body"])
        if title is not None:
            md["title"] = title
        if auto_run is not None:
            md["auto_run"] = auto_run
        if auto_accept is not None:
            md["auto_accept"] = auto_accept
        if project is not None:
            md["project"] = project
        if workdir is not None:
            md["workdir"] = workdir
        if parent_id is not None:
            # "" detaches the card from its parent; an id re-parents it.
            if parent_id:
                self._check_parent(card_id, parent_id)
                md["parent_id"] = parent_id
            else:
                md.pop("parent_id", None)
        if model is not None:
            # "" clears the override: the card goes back to its role's model.
            if model:
                md["model"] = model
            else:
                md.pop("model", None)
        if changed and card["status"] in FINISHED_STATUSES:
            # Editing a finished card resets it: a re-run starts fresh.
            md["status"] = "draft"
            md["session"] = None
            md["accepted"] = False
        md["updated_at"] = _now()

        new_body = card["body"] if body is None else body
        self._write(self._find_path(card_id), new_body, md)
        return self.get(card_id)

    def _check_parent(self, card_id: str, parent_id: str) -> None:
        """A card's parent must exist and must not be the card or its descendant.

        Splitting is a tree, so a cycle would make the board's tree view
        infinite and the "is this subtree done?" question unanswerable.
        """
        if parent_id == card_id:
            raise DependencyError("a card cannot be its own parent")
        try:
            self.get(parent_id)
        except CardNotFound:
            raise DependencyError(f"parent card {parent_id!r} does not exist")
        # Walk up from the proposed parent: if we reach this card, it is a cycle.
        seen = set()
        current = parent_id
        while current and current not in seen:
            seen.add(current)
            if current == card_id:
                raise DependencyError(
                    f"card {parent_id!r} is a descendant of {card_id!r} — "
                    "that would make a cycle")
            try:
                current = self.get(current).get("parent_id")
            except CardNotFound:
                break

    def drop_dependency_refs(self, card_id: str) -> None:
        """Drop references to card_id from every other card's depends_on, so
        dependents do not wait on a card that no longer exists on the board
        (a missing dependency counts as satisfied anyway). Used by delete()
        and by archiving."""
        for other in self.list():
            if card_id in (other.get("depends_on") or []):
                opost = self._get_post(other["id"])
                omd = dict(opost.metadata)
                omd["depends_on"] = [d for d in (omd.get("depends_on") or [])
                                     if d != card_id]
                omd["updated_at"] = _now()
                self._write(self._find_path(other["id"]), other["body"], omd)

    def delete(self, card_id: str) -> None:
        post = self._get_post(card_id)
        card = self._card_from_post(post)
        if card["status"] in LIVE_STATUSES:
            raise CardRunning(f"card {card_id!r} is running and cannot be deleted")
        self._find_path(card_id).unlink()
        self.drop_dependency_refs(card_id)

    def set_status(self, card_id: str, status: str,
                   session_patch: dict | None = None) -> dict:
        if status not in STATUSES:
            raise InvalidTransition(f"unknown status {status!r}")
        post = self._get_post(card_id)
        card = self._card_from_post(post)
        if status not in TRANSITIONS.get(card["status"], set()):
            raise InvalidTransition(
                f"cannot transition {card['status']!r} -> {status!r}")

        md = dict(post.metadata)
        md["status"] = status
        # The flag follows the status: only an accepted card is `accepted`.
        md["accepted"] = status == "accepted"
        session = dict(card["session"]) if card["session"] else dict.fromkeys(SESSION_KEYS)
        if session_patch:
            session.update(session_patch)
        md["session"] = session
        md["updated_at"] = _now()

        self._write(self._find_path(card_id), card["body"], md)
        return self.get(card_id)

    def check_accept(self, card_id: str) -> dict:
        """Validate the accept gate without mutating; returns the card.

        Raises InvalidTransition when the card cannot be accepted.
        """
        card = self._card_from_post(self._get_post(card_id))
        if card["status"] != "done":
            raise InvalidTransition(
                f"cannot accept card in status {card['status']!r}; must be 'done'")
        review = (card.get("session") or {}).get("review") or {}
        # Normally the reviewer must have approved. Exception: when the
        # review/fix loop exhausted its rounds (review.exhausted), the card is
        # done but unapproved — the user accepts it manually.
        if review.get("approved") is not True and review.get("exhausted") is not True:
            raise InvalidTransition(
                "card has no approved review — it must be reviewed before it "
                "can be accepted")
        return card

    def accept(self, card_id: str) -> dict:
        """done -> accepted, but only with an approved review on record.

        A card can only be accepted after the reviewer has approved it; the
        review loop normally guarantees this, and this gate also covers cards
        that finished before their last review. When the review/fix loop
        exhausted its rounds (review.exhausted), acceptance is the user's
        manual call.
        """
        card = self.check_accept(card_id)
        post = self._get_post(card_id)
        md = dict(post.metadata)
        md["status"] = "accepted"
        md["accepted"] = True
        md["updated_at"] = _now()

        self._write(self._find_path(card_id), card["body"], md)
        return self.get(card_id)

    def patch_session(self, card_id: str, patch: dict) -> dict:
        """Merge keys into the card's session dict without a status change."""
        post = self._get_post(card_id)
        card = self._card_from_post(post)
        if card["status"] in LIVE_STATUSES:
            raise CardRunning(f"card {card_id!r} is running and cannot be patched")
        md = dict(post.metadata)
        session = dict(card["session"]) if card["session"] else dict.fromkeys(SESSION_KEYS)
        session.update(patch)
        md["session"] = session
        md["updated_at"] = _now()
        self._write(self._find_path(card_id), card["body"], md)
        return self.get(card_id)

    def reset_to_draft(self, card_id: str) -> dict:
        """Reset a finished card to draft for a fresh run (clears session/accepted)."""
        post = self._get_post(card_id)
        card = self._card_from_post(post)
        if card["status"] in LIVE_STATUSES:
            raise CardRunning(f"card {card_id!r} is running and cannot be reset")
        md = dict(post.metadata)
        md["status"] = "draft"
        md["session"] = None
        md["accepted"] = False
        md["updated_at"] = _now()
        self._write(self._find_path(card_id), card["body"], md)
        return self.get(card_id)

    def reopen_after_revert(self, card_id: str,
                            session_patch: dict | None = None) -> dict:
        """accepted -> draft after a revert: the card is back on the board as a
        draft and `accepted` is cleared, but the SESSION IS KEPT (tokens, logs,
        reviews, git) so the cost record of the accepted run survives. The
        revert bookkeeping (`reverted_at`, `revert_commit`) is merged in."""
        return self.set_status(card_id, "draft", session_patch=session_patch)

    # ------------------------------------------------------------------
    # dependencies ("runs after" links between cards)
    # ------------------------------------------------------------------

    def _would_cycle(self, card_id: str, dep_id: str) -> bool:
        """True when adding the edge card_id -> dep_id (card_id depends on
        dep_id) would create a cycle, i.e. card_id is reachable from dep_id
        by following depends_on edges. Missing cards along the way are
        treated as leaves."""
        seen: set[str] = set()
        stack = [dep_id]
        while stack:
            cur = stack.pop()
            if cur == card_id:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            try:
                n = self.get(cur)
            except CardNotFound:
                continue
            stack.extend(n.get("depends_on") or [])
        return False

    def add_dependency(self, card_id: str, dep_id: str) -> dict:
        """Make card_id depend on dep_id: card_id is run automatically once
        dep_id completes (done/accepted). Idempotent; rejects self-links,
        cycles, unknown cards, and edits while the card is live."""
        if card_id == dep_id:
            raise DependencyError("a card cannot depend on itself")
        post = self._get_post(card_id)          # CardNotFound -> 404
        card = self._card_from_post(post)
        if card["status"] in LIVE_STATUSES:
            raise CardRunning(
                f"card {card_id!r} is running and its dependencies cannot be changed")
        self._get_post(dep_id)                  # the dependency must exist
        deps = list(card.get("depends_on") or [])
        if dep_id in deps:
            return card                         # already linked — idempotent
        if self._would_cycle(card_id, dep_id):
            raise DependencyError(
                f"dependency {card_id!r} -> {dep_id!r} would create a cycle")
        md = dict(post.metadata)
        md["depends_on"] = deps + [dep_id]
        md["updated_at"] = _now()
        self._write(self._find_path(card_id), card["body"], md)
        return self.get(card_id)

    def remove_dependency(self, card_id: str, dep_id: str) -> dict:
        """Remove the card_id -> dep_id edge. Idempotent (removing an edge
        that does not exist is a no-op)."""
        post = self._get_post(card_id)
        card = self._card_from_post(post)
        if card["status"] in LIVE_STATUSES:
            raise CardRunning(
                f"card {card_id!r} is running and its dependencies cannot be changed")
        md = dict(post.metadata)
        md["depends_on"] = [d for d in (md.get("depends_on") or []) if d != dep_id]
        md["updated_at"] = _now()
        self._write(self._find_path(card_id), card["body"], md)
        return self.get(card_id)

    def dependencies(self, card_id: str) -> dict:
        """Dependency view of one card: depends_on (what it waits for),
        blocks (cards that wait for it), unmet (dependencies not yet merged;
        missing cards count as satisfied)."""
        card = self.get(card_id)
        deps = card.get("depends_on") or []
        blocks = [n["id"] for n in self.list()
                  if card_id in (n.get("depends_on") or [])]
        unmet = []
        for dep in deps:
            try:
                dep_card = self.get(dep)
            except CardNotFound:
                continue  # deleted dependency counts as satisfied
            if dep_card["status"] not in COMPLETED_STATUSES:
                unmet.append(dep)
        return {"card_id": card_id, "depends_on": deps,
                "blocks": blocks, "unmet": unmet}
