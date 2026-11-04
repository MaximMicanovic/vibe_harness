"""ArchiveStore: the archive is a plain folder structure.

Layout:

    archives/
      <slug>-<id>/            one folder per archive
        archive.md            manifest (YAML frontmatter: id, name, type, ...)
        cards/                archived cards — their original .md files, with
                              `archived: true`, `archive: <id>`, `archived_at`
                              added to the frontmatter
        notes/                the same, for archives created before the
                              notes -> cards rename; read, never written

Each archive has a free-form `type` ("cards", "deck", "project", …) so the
user can keep different kinds of archives side by side; the type is just a
label on the folder's manifest (presets suggested, any slug allowed).

Archiving a card requires status "accepted" (409 otherwise) and moves the
file out of cards/ — the card leaves the board but keeps its full history
(frontmatter + sessions/<id>/ dir). Archiving a deck (project) moves all of
the project's non-live cards and unregisters the project; archiving a deck
(stack) moves its cards into one archive, records the deck in the manifest
(`archived_stacks`: title + card order) and dissolves it. Cards can be moved
between archives and restored to the board.
"""
from __future__ import annotations

import os
import re
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from app import mdstore
from app.cards import (
    InvalidTransition,
    LIVE_STATUSES,
    CardNotFound,
    CardRunning,
    CardStore,
    _slugify,
    card_from_md,
)
from app.projects import ProjectNotFound, ProjectStore
from app.stacks import StackStore

# Where an archive keeps its card files. New items always go to ITEMS_DIR;
# archives created before the notes -> cards rename still hold theirs in
# LEGACY_ITEMS_DIR, so every read looks in both.
ITEMS_DIR = "cards"
LEGACY_ITEMS_DIR = "notes"


class ArchiveError(Exception): ...        # maps to HTTP 400
class ArchiveNotFound(ArchiveError): ...  # maps to HTTP 404
class ArchiveExists(ArchiveError): ...   # maps to HTTP 409
class CardNotArchived(ArchiveError): ...  # maps to HTTP 404
class CardInOtherArchive(ArchiveError): ...  # maps to HTTP 409


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write(path: Path, body: str, metadata: dict) -> None:
    # Atomic write (temp + os.replace), same as CardStore._write.
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(mdstore.dumps(metadata, body), encoding="utf-8")
    os.replace(tmp, path)


class ArchiveStore:
    def __init__(self, archives_dir: Path, cards: CardStore,
                 projects: ProjectStore | None = None,
                 stacks: StackStore | None = None):
        self.archives_dir = Path(archives_dir)
        self.cards = cards
        self.projects = projects
        self.stacks = stacks
        # The dir is created lazily on first create() so merely constructing a
        # store (e.g. in tests) never touches the real archives/ folder.

    # ------------------------------------------------------------------
    # manifest helpers
    # ------------------------------------------------------------------

    def _find_archive_path(self, archive_id: str) -> Path | None:
        """Locate an archive's folder by the id in its manifest frontmatter."""
        if not self.archives_dir.exists():
            return None
        for path in sorted(self.archives_dir.glob("*/archive.md")):
            try:
                md, _ = mdstore.load(path)
            except Exception:
                continue
            if str(md.get("id")) == archive_id:
                return path.parent
        return None

    def _items_dirs(self, archive_dir: Path) -> list[Path]:
        """The folders holding this archive's cards, current layout first."""
        return [archive_dir / name for name in (ITEMS_DIR, LEGACY_ITEMS_DIR)
                if (archive_dir / name).is_dir()]

    def _manifest(self, archive_dir: Path) -> dict:
        md, _ = mdstore.load(archive_dir / "archive.md")
        return {
            "id": md["id"],
            "name": md["name"],
            "type": md.get("type") or "cards",
            "description": md.get("description") or "",
            "created_at": md["created_at"],
            "updated_at": md["updated_at"],
            # The project this archive belongs to ("" = not tied to one). The
            # board groups the archive browser by it, so archives read as
            # project folders rather than one flat list.
            "project": str(md.get("project") or ""),
            "archived_projects": list(md.get("archived_projects") or []),
            "archived_stacks": list(md.get("archived_stacks") or []),
            "path": str(archive_dir),
            "item_count": sum(len(list(d.glob("*.md")))
                              for d in self._items_dirs(archive_dir)),
        }

    def _get_archive_dir(self, archive_id: str) -> Path:
        path = self._find_archive_path(archive_id)
        if path is None:
            raise ArchiveNotFound(f"no archive with id {archive_id!r}")
        return path

    def set_project(self, archive_id: str, project: str) -> dict:
        """Move an archive under a project header ("" = no project)."""
        archive_dir = self._get_archive_dir(archive_id)
        md, body = mdstore.load(archive_dir / "archive.md")
        md = dict(md)
        md["project"] = (project or "").strip()
        md["updated_at"] = _now()
        _write(archive_dir / "archive.md", body, md)
        return self.get(archive_id)

    def _touch(self, archive_dir: Path) -> None:
        md, body = mdstore.load(archive_dir / "archive.md")
        md = dict(md)
        md["updated_at"] = _now()
        _write(archive_dir / "archive.md", body, md)

    def _items(self, archive_dir: Path) -> list[dict]:
        items = []
        seen: set[str] = set()
        for items_dir in self._items_dirs(archive_dir):
            for path in sorted(items_dir.glob("*.md")):
                try:
                    md, body = mdstore.load(path)
                    card = card_from_md(md, body)
                except Exception:
                    continue
                if card["id"] in seen:
                    continue
                seen.add(card["id"])
                items.append(card)
        return items

    def _project_groups(self, archive_dir: Path, items: list[dict]) -> list[dict]:
        """Group an archive's cards under project headers — the archive tree's
        third level (archive folder -> project header -> cards).

        One group per project, named after the card's `project` frontmatter;
        decks archived wholesale keep their header even when no card is left
        (the manifest's `archived_projects`). Cards without a project land in a
        final group with `name: None` ("No project" in the UI). Groups are
        alphabetical, unfiled last, so the tree is stable between renders.
        """
        md, _ = mdstore.load(archive_dir / "archive.md")
        groups: dict[str | None, dict] = {}
        for deck in md.get("archived_projects") or []:
            name = str(deck.get("name") or "").strip()
            if name:
                groups[name] = {"name": name, "archived_at": deck.get("archived_at"),
                                "cards": []}
        for card in items:
            name = (card.get("project") or "").strip() or None
            group = groups.setdefault(
                name, {"name": name, "archived_at": None, "cards": []})
            group["cards"].append(card)
        named = sorted((g for n, g in groups.items() if n is not None),
                       key=lambda g: g["name"].lower())
        unfiled = groups.get(None)
        if unfiled is not None:
            named.append(unfiled)
        for group in named:
            group["count"] = len(group["cards"])
        return named

    def _find_archived(self, card_id: str) -> tuple[Path, Path] | None:
        """Locate an archived card: (archive_dir, card file path) or None."""
        if not self.archives_dir.exists():
            return None
        for name in (ITEMS_DIR, LEGACY_ITEMS_DIR):
            for cards_dir in sorted(self.archives_dir.glob(f"*/{name}")):
                for path in sorted(cards_dir.glob("*.md")):
                    try:
                        md, _ = mdstore.load(path)
                    except Exception:
                        continue
                    if str(md.get("id")) == card_id:
                        return cards_dir.parent, path
        return None

    # ------------------------------------------------------------------
    # public interface
    # ------------------------------------------------------------------

    def create(self, name: str, type: str = "cards", description: str = "",
               project: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ArchiveError("archive name is required")
        type_slug = (type or "cards").strip().lower()
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", type_slug):
            raise ArchiveError(
                f"archive type must be a slug label (e.g. 'cards', 'deck'), got {type!r}")
        for existing in self.list():
            if existing["name"] == name:
                raise ArchiveExists(f"an archive named {name!r} already exists")
        archive_id = secrets.token_hex(3)
        now = _now()
        archive_dir = self.archives_dir / f"{_slugify(name)}-{archive_id}"
        (archive_dir / ITEMS_DIR).mkdir(parents=True, exist_ok=True)
        metadata = {
            "id": archive_id,
            "name": name,
            "type": type_slug,
            "description": description or "",
            "project": (project or "").strip(),
            "created_at": now,
            "updated_at": now,
            "archived_projects": [],
        }
        _write(archive_dir / "archive.md", description or "", metadata)
        return self.get(archive_id)

    def list(self) -> list[dict]:
        """All archives, most recently updated first."""
        if not self.archives_dir.exists():
            return []
        archives = []
        for path in sorted(self.archives_dir.glob("*/archive.md")):
            try:
                archives.append(self._manifest(path.parent))
            except Exception:
                continue
        archives.sort(key=lambda a: a["updated_at"], reverse=True)
        return archives

    def get(self, archive_id: str) -> dict:
        archive_dir = self._get_archive_dir(archive_id)
        items = self._items(archive_dir)
        return {**self._manifest(archive_dir), "items": items,
                "projects": self._project_groups(archive_dir, items)}

    def scan(self) -> list[tuple[dict, list[dict]]]:
        """Every archive with its cards, in one pass, most recently updated
        first. What search scans: manifest and cards together, so a query does
        not re-read every manifest once per archive. A broken manifest is
        skipped, never fatal."""
        if not self.archives_dir.exists():
            return []
        found = []
        for path in sorted(self.archives_dir.glob("*/archive.md")):
            try:
                found.append((self._manifest(path.parent), self._items(path.parent)))
            except Exception:
                continue
        found.sort(key=lambda pair: pair[0]["updated_at"], reverse=True)
        return found

    def archive_cards(self, card_ids: list[str], archive_id: str,
                      *, gate_accepted: bool = True) -> dict:
        """Move cards into an archive. With gate_accepted (the default), cards
        on the board must be accepted (InvalidTransition otherwise) and live
        cards are rejected (CardRunning); cards already archived can be moved
        freely between archives. Unknown card -> CardNotFound, unknown
        archive -> ArchiveNotFound. Validation happens before any file moves,
        so a rejected batch moves nothing."""
        archive_dir = self._get_archive_dir(archive_id)
        # Pass 1: resolve + validate everything before touching the disk.
        # Duplicate ids are deduped here (order preserved) so pass 2 never
        # tries to move/unlink the same source file twice.
        resolved = []
        for card_id in dict.fromkeys(card_ids):
            board_path = self.cards._find_path(card_id)
            archived = self._find_archived(card_id)
            if board_path is None and archived is None:
                raise CardNotFound(f"no card with id {card_id!r}")
            if board_path is not None:
                md, body = mdstore.load(board_path)
                card = card_from_md(md, body)
                if card["status"] in LIVE_STATUSES:
                    raise CardRunning(
                        f"cannot archive card {card_id!r}: it is running")
                if gate_accepted and card["status"] != "accepted":
                    raise InvalidTransition(
                        f"cannot archive card {card_id!r} in status {card['status']!r}; "
                        "only accepted cards can be archived")
                src_path, from_board = board_path, True
            else:
                md, body = mdstore.load(archived[1])
                card = card_from_md(md, body)
                src_path, from_board = archived[1], False
            resolved.append((card_id, md, body, src_path, from_board))
        # Pass 2: move the files.
        items_dir = archive_dir / ITEMS_DIR
        items_dir.mkdir(parents=True, exist_ok=True)
        moved = []
        for card_id, md, body, src_path, from_board in resolved:
            md = dict(md)
            md["archived"] = True
            md["archive"] = archive_id
            md["archived_at"] = _now()
            md["updated_at"] = _now()
            dest = items_dir / src_path.name
            _write(dest, body, md)
            if src_path != dest:
                src_path.unlink()
            if from_board:
                # The card left the board: drop depends_on references to it so
                # dependents do not wait on a card that is no longer there
                # (a missing dependency counts as satisfied anyway).
                self.cards.drop_dependency_refs(card_id)
            moved.append(card_from_md(*mdstore.load(dest)))
        self._touch(archive_dir)
        return {"archive": self.get(archive_id), "cards": moved}

    def archive_card(self, card_id: str, archive_id: str) -> dict:
        return self.archive_cards([card_id], archive_id)

    def archive_stack(self, stack_id: str, archive_id: str | None = None,
                      *, name: str | None = None) -> dict:
        """Archive a whole deck (stack) as one unit.

        The deck's cards move into `archive_id`; without one, an archive named
        after the deck (type "deck") is created — one archive per retired deck
        is the structure the board's decks map onto — and reused when it
        already exists. The deck is recorded in the manifest
        (`archived_stacks`: title + card ids in board order) so the structure
        survives the move, then dissolved from the board.

        Every card must be accepted (InvalidTransition) and none may be live
        (CardRunning); validation happens before any file moves. Unknown deck
        -> StackNotFound.
        """
        if self.stacks is None:
            raise ArchiveError("no stack store configured")
        stack = self.stacks.get(stack_id)
        if archive_id is None:
            archive_id = self._archive_for_deck(name or stack["title"] or stack_id)
        moved = self.archive_cards(stack["ids"], archive_id)["cards"]
        archive_dir = self._get_archive_dir(archive_id)
        md, body = mdstore.load(archive_dir / "archive.md")
        md = dict(md)
        shelved = list(md.get("archived_stacks") or [])
        shelved.append({"id": stack["id"], "title": stack["title"],
                        "cards": list(stack["ids"]), "archived_at": _now()})
        md["archived_stacks"] = shelved
        md["updated_at"] = _now()
        _write(archive_dir / "archive.md", body, md)
        self.stacks.delete(stack["id"])
        return {"stack": stack, "archive": self.get(archive_id), "cards": moved}

    def _archive_for_deck(self, name: str) -> str:
        """The archive a deck is retired into: an existing one with that name,
        else a new "deck"-typed archive named after it."""
        name = (name or "").strip() or "Retired deck"
        for existing in self.list():
            if existing["name"] == name:
                return existing["id"]
        return self.create(name, type="deck")["id"]

    def restore_card(self, card_id: str, archive_id: str | None = None) -> dict:
        """Move an archived card back to the board (cards/). The archive id,
        when given, must be the one holding the card."""
        found = self._find_archived(card_id)
        if found is None:
            raise CardNotArchived(f"no archived card with id {card_id!r}")
        archive_dir, card_path = found
        if archive_id is not None and str(self._manifest(archive_dir)["id"]) != archive_id:
            raise CardInOtherArchive(
                f"card {card_id!r} is archived in archive "
                f"{self._manifest(archive_dir)['id']!r}, not {archive_id!r}")
        md, body = mdstore.load(card_path)
        md = dict(md)
        md["archived"] = False
        md["archive"] = None
        md["archived_at"] = None
        md["updated_at"] = _now()
        dest = self.cards.cards_dir / card_path.name
        _write(dest, body, md)
        card_path.unlink()
        self._touch(archive_dir)
        return card_from_md(*mdstore.load(dest))

    def archive_project(self, project_name: str, archive_id: str) -> dict:
        """Archive an entire deck (project): all of its non-live cards move
        into the archive (deck membership is kept in each card's `project`
        frontmatter), the deck is recorded in the manifest, and the project
        is unregistered. Live cards -> CardRunning; unknown project ->
        ProjectNotFound."""
        if self.projects is None:
            raise ArchiveError("no project registry configured")
        if self.projects.get(project_name) is None:
            raise ProjectNotFound(f"no project named {project_name!r}")
        archive_dir = self._get_archive_dir(archive_id)
        project_cards = [n for n in self.cards.list()
                         if (n.get("project") or "") == project_name]
        live = [n for n in project_cards if n["status"] in LIVE_STATUSES]
        if live:
            raise CardRunning(
                "cannot archive deck "
                f"{project_name!r}: card(s) still running: "
                + ", ".join(n["id"] for n in live))
        moved = []
        if project_cards:
            # The whole deck moves — not only accepted cards — but never live ones.
            moved = self.archive_cards([n["id"] for n in project_cards], archive_id,
                                       gate_accepted=False)["cards"]
        # record the deck in the manifest, then unregister the project
        md, body = mdstore.load(archive_dir / "archive.md")
        md = dict(md)
        shelved = list(md.get("archived_projects") or [])
        if not any(d.get("name") == project_name for d in shelved):
            shelved.append({"name": project_name, "archived_at": _now()})
        md["archived_projects"] = shelved
        md["updated_at"] = _now()
        _write(archive_dir / "archive.md", body, md)
        self.projects.delete(project_name)
        return {"project": project_name, "archive": self.get(archive_id), "cards": moved}
