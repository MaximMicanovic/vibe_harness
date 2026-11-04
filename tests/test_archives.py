"""Tests for the archive: app.archives.ArchiveStore + the /api/archives routes.

The archive is a folder structure: archives/<slug>-<id>/ holds a manifest
(archive.md, YAML frontmatter) and a cards/ subfolder with the archived card
files (their original markdown, frontmatter gains archived/archive/archived_at).
Archives have a free-form `type` ("cards", "deck", "project", …) so the user can
keep different kinds of archives side by side.

Archiving cards requires status "accepted" (409 otherwise). Archiving a deck
(project) moves all of its non-live cards and unregisters the project.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import mdstore
from app.archives import (
    ArchiveError,
    ArchiveExists,
    ArchiveNotFound,
    ArchiveStore,
    CardNotArchived,
)
from app.config import Config
from app.main import create_app
from app.cards import InvalidTransition, CardNotFound, CardRunning, CardStore
from app.projects import ProjectNotFound, ProjectStore
from app.runner import SessionRunner
from app.stacks import StackNotFound, StackStore

FAKE_VIBE = """#!/bin/sh
workdir=""
outdir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2;;
    --output-dir) outdir="$2"; shift 2;;
    *) shift;;
  esac
done
mkdir -p "$workdir" "$outdir"
cat > "$outdir/export.json" <<'EOF'
{"usage": {"total_tokens": 42}, "config": {"active_model": "fake-model"}, "stop_reason": "end_turn", "outcome": "success"}
EOF
echo "VERDICT: APPROVED"
exit 0
"""


@pytest.fixture
def stores(tmp_path) -> SimpleNamespace:
    cards = CardStore(tmp_path / "cards")
    projects = ProjectStore(tmp_path / "projects.json")
    stacks = StackStore(tmp_path / "stacks.json", cards)
    archives = ArchiveStore(tmp_path / "archives", cards, projects, stacks=stacks)
    return SimpleNamespace(cards=cards, projects=projects, stacks=stacks,
                           archives=archives, tmp_path=tmp_path)


def accepted_card(store: CardStore, title="Accepted card", **kw) -> dict:
    """Create a card and walk it to accepted (approved review on record)."""
    card = store.create(title, "body", **kw)
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done",
                     session_patch={"review": {"approved": True, "round": 1}})
    return store.accept(card["id"])


def done_card(store: CardStore, title="Done card") -> dict:
    """Create a card and walk it to done (approved review, not accepted)."""
    card = store.create(title, "body")
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done",
                     session_patch={"review": {"approved": True, "round": 1}})
    return store.get(card["id"])


# ----------------------------------------------------------------------
# create / list / get
# ----------------------------------------------------------------------

def test_create_archive_folder_structure(stores):
    a = stores.archives.create("Sprint 12", type="deck", description="done cards")
    assert a["id"] and len(a["id"]) == 6
    assert a["name"] == "Sprint 12"
    assert a["type"] == "deck"
    assert a["description"] == "done cards"
    assert a["archived_projects"] == []
    assert a["item_count"] == 0
    root = stores.tmp_path / "archives" / f"sprint-12-{a['id']}"
    assert (root / "archive.md").exists()
    assert (root / "cards").is_dir()
    md, _ = mdstore.load(root / "archive.md")
    assert md["id"] == a["id"] and md["type"] == "deck"


def test_create_archive_default_type_and_slug(stores):
    a = stores.archives.create("My Cards!", )
    assert a["type"] == "cards"
    assert (stores.tmp_path / "archives" / f"my-cards-{a['id']}").is_dir()


def test_archive_belongs_to_a_project(stores):
    # The board's archive browser groups archives under a project header, so
    # the archive itself carries the project it was filed under.
    a = stores.archives.create("Sprint 12", type="deck", project="Web")
    assert a["project"] == "Web"
    assert stores.archives.get(a["id"])["project"] == "Web"
    # ...and an archive created without one is simply unfiled.
    assert stores.archives.create("Loose")["project"] == ""


def test_set_archive_project_moves_it_between_headers(stores):
    a = stores.archives.create("Pile")
    assert stores.archives.set_project(a["id"], "Web")["project"] == "Web"
    assert stores.archives.set_project(a["id"], "")["project"] == ""
    with pytest.raises(ArchiveNotFound):
        stores.archives.set_project("ffffff", "Web")


def test_create_archive_duplicate_name_rejected(stores):
    stores.archives.create("Done pile")
    with pytest.raises(ArchiveExists):
        stores.archives.create("Done pile", type="deck")


def test_create_archive_validates_name_and_type(stores):
    with pytest.raises(ArchiveError):
        stores.archives.create("   ")
    with pytest.raises(ArchiveError):
        stores.archives.create("ok", type="not a slug!")


def test_list_and_get_archives(stores):
    a1 = stores.archives.create("Alpha")
    a2 = stores.archives.create("Beta", type="deck")
    listed = stores.archives.list()
    assert {a["id"] for a in listed} == {a1["id"], a2["id"]}
    by_id = {a["id"]: a for a in listed}
    assert by_id[a2["id"]]["type"] == "deck"
    assert by_id[a1["id"]]["item_count"] == 0
    got = stores.archives.get(a1["id"])
    assert got["name"] == "Alpha"
    assert got["items"] == []
    with pytest.raises(ArchiveNotFound):
        stores.archives.get("ffffff")


# ----------------------------------------------------------------------
# archive cards
# ----------------------------------------------------------------------

def test_archive_accepted_card_moves_file_and_marks_frontmatter(stores):
    card = accepted_card(stores.cards, "Ship it")
    a = stores.archives.create("Done pile")
    out = stores.archives.archive_card(card["id"], a["id"])

    # gone from the board, present in the archive
    with pytest.raises(CardNotFound):
        stores.cards.get(card["id"])
    assert card["id"] not in [n["id"] for n in stores.cards.list()]
    dest = stores.tmp_path / "archives" / f"done-pile-{a['id']}" / "cards" / f"ship-it-{card['id']}.md"
    assert dest.exists()
    md, _ = mdstore.load(dest)
    assert md["archived"] is True
    assert md["archive"] == a["id"]
    assert md["archived_at"]
    assert md["status"] == "accepted"  # status preserved

    items = stores.archives.get(a["id"])["items"]
    assert [n["id"] for n in items] == [card["id"]]
    assert items[0]["archived"] is True
    assert out["archive"]["item_count"] == 1
    assert [n["id"] for n in out["cards"]] == [card["id"]]


def test_archive_card_requires_accepted(stores):
    a = stores.archives.create("Pile")
    draft = stores.cards.create("Draft card", "b")
    with pytest.raises(InvalidTransition):
        stores.archives.archive_card(draft["id"], a["id"])
    running = stores.cards.create("Live card", "b")
    stores.cards.set_status(running["id"], "running")
    with pytest.raises(CardRunning):
        stores.archives.archive_card(running["id"], a["id"])
    # still on the board, archive empty
    assert stores.cards.get(draft["id"])["status"] == "draft"
    assert stores.archives.get(a["id"])["items"] == []


def test_archive_unknown_card_or_archive(stores):
    a = stores.archives.create("Pile")
    with pytest.raises(CardNotFound):
        stores.archives.archive_card("abcdef", a["id"])
    card = accepted_card(stores.cards)
    with pytest.raises(ArchiveNotFound):
        stores.archives.archive_card(card["id"], "abcdef")


def test_move_card_between_archives(stores):
    card = accepted_card(stores.cards, "Mover")
    a1 = stores.archives.create("One")
    a2 = stores.archives.create("Two", type="deck")
    stores.archives.archive_card(card["id"], a1["id"])
    stores.archives.archive_card(card["id"], a2["id"])  # move, no status gate (already archived)
    assert stores.archives.get(a1["id"])["items"] == []
    items = stores.archives.get(a2["id"])["items"]
    assert [n["id"] for n in items] == [card["id"]]
    assert items[0]["archive"] == a2["id"]
    # exactly one file on disk, in the second archive
    files = list((stores.tmp_path / "archives").glob("*/cards/*.md"))
    assert len(files) == 1
    assert a2["id"] in str(files[0])


def test_archive_cards_batch(stores):
    n1 = accepted_card(stores.cards, "One")
    n2 = accepted_card(stores.cards, "Two")
    a = stores.archives.create("Deck", type="deck")
    out = stores.archives.archive_cards([n1["id"], n2["id"]], a["id"])
    assert {n["id"] for n in out["cards"]} == {n1["id"], n2["id"]}
    assert len(stores.archives.get(a["id"])["items"]) == 2
    assert stores.cards.list() == []
    # batch with one non-accepted card fails atomically-ish: the accepted one
    # is rejected together with the draft (409), nothing moves
    n3 = accepted_card(stores.cards, "Three")
    draft = stores.cards.create("Draft", "b")
    with pytest.raises(InvalidTransition):
        stores.archives.archive_cards([n3["id"], draft["id"]], a["id"])
    assert stores.cards.get(n3["id"])["status"] == "accepted"


def test_archiving_drops_dependency_refs_on_board(stores):
    dep = accepted_card(stores.cards, "Base")
    waiter = stores.cards.create("Waiter", "b")
    stores.cards.add_dependency(waiter["id"], dep["id"])
    a = stores.archives.create("Pile")
    stores.archives.archive_card(dep["id"], a["id"])
    assert stores.cards.get(waiter["id"])["depends_on"] == []


# ----------------------------------------------------------------------
# restore
# ----------------------------------------------------------------------

def test_restore_card_back_to_board(stores):
    card = accepted_card(stores.cards, "Boomerang")
    a = stores.archives.create("Pile")
    stores.archives.archive_card(card["id"], a["id"])
    back = stores.archives.restore_card(card["id"])
    assert back["id"] == card["id"]
    assert back["status"] == "accepted"
    assert back["archived"] is False
    assert back["archive"] is None
    assert stores.cards.get(card["id"])["id"] == card["id"]
    assert stores.archives.get(a["id"])["items"] == []
    # file is back in cards/
    files = list((stores.tmp_path / "cards").glob("*.md"))
    assert len(files) == 1


def test_restore_with_wrong_archive_id_rejected(stores):
    card = accepted_card(stores.cards)
    a1 = stores.archives.create("One")
    a2 = stores.archives.create("Two")
    stores.archives.archive_card(card["id"], a1["id"])
    with pytest.raises(ArchiveError):
        stores.archives.restore_card(card["id"], archive_id=a2["id"])
    # the correct id works
    assert stores.archives.restore_card(card["id"], archive_id=a1["id"])["id"] == card["id"]


def test_restore_unknown_card(stores):
    stores.archives.create("Pile")
    with pytest.raises(CardNotArchived):
        stores.archives.restore_card("abcdef")
    board = stores.cards.create("On board", "b")  # not archived at all
    with pytest.raises(CardNotArchived):
        stores.archives.restore_card(board["id"])


# ----------------------------------------------------------------------
# archive a whole deck (project)
# ----------------------------------------------------------------------

def test_archive_project_moves_all_its_cards_and_unregisters(stores):
    stores.projects.create("web", str(stores.tmp_path / "web"))
    n1 = accepted_card(stores.cards, "Card A", project="web")
    n2 = stores.cards.create("Card B", "b", project="web")  # draft, non-live: moves too (whole deck)
    other = accepted_card(stores.cards, "Card C", project=None)
    a = stores.archives.create("Retired decks", type="project")
    out = stores.archives.archive_project("web", a["id"])

    assert out["project"] == "web"
    assert {n["id"] for n in out["cards"]} == {n1["id"], n2["id"]}
    assert stores.projects.get("web") is None          # unregistered
    assert stores.cards.get(other["id"])["project"] is None  # untouched
    items = {n["id"]: n for n in stores.archives.get(a["id"])["items"]}
    assert set(items) == {n1["id"], n2["id"]}
    assert items[n1["id"]]["project"] == "web"          # deck membership kept in frontmatter
    shelved = stores.archives.get(a["id"])["archived_projects"]
    assert shelved and shelved[0]["name"] == "web" and shelved[0]["archived_at"]


def test_archive_project_rejects_live_cards_and_unknown_project(stores):
    stores.projects.create("web", str(stores.tmp_path / "web"))
    live = stores.cards.create("Live", "b", project="web")
    stores.cards.set_status(live["id"], "running")
    a = stores.archives.create("Pile")
    with pytest.raises(CardRunning):
        stores.archives.archive_project("web", a["id"])
    assert stores.projects.get("web") is not None       # not unregistered
    with pytest.raises(ProjectNotFound):
        stores.archives.archive_project("nope", a["id"])


# ----------------------------------------------------------------------
# HTTP API
# ----------------------------------------------------------------------

def make_env(tmp_path: Path) -> SimpleNamespace:
    store = CardStore(tmp_path / "cards")
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(FAKE_VIBE)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30)
    projects = ProjectStore(tmp_path / "projects.json")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects)
    archives = ArchiveStore(tmp_path / "archives", store, projects,
                            stacks=StackStore(tmp_path / "stacks.json", store))
    app = create_app(config=config, store=store, runner=runner, projects=projects,
                     archives=archives)
    client = TestClient(app)
    return SimpleNamespace(client=client, store=store, runner=runner,
                           projects=projects, archives=archives, tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def api_accepted_card(env, title="Card", **extra) -> dict:
    r = env.client.post("/api/cards", json={"title": title, "body": "b", **extra})
    assert r.status_code == 201, r.text
    card = r.json()
    env.store.set_status(card["id"], "running")
    env.store.set_status(card["id"], "done",
                         session_patch={"review": {"approved": True, "round": 1}})
    r = env.client.post(f"/api/cards/{card['id']}/accept")
    assert r.status_code == 200, r.text
    return r.json()


def test_api_archive_crud(env):
    r = env.client.post("/api/archives", json={"name": "Done pile", "type": "cards"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["type"] == "cards" and a["item_count"] == 0

    r = env.client.post("/api/archives", json={"name": "Done pile"})
    assert r.status_code == 409
    r = env.client.post("/api/archives", json={"type": "cards"})
    assert r.status_code == 422

    r = env.client.get("/api/archives")
    assert [x["id"] for x in r.json()] == [a["id"]]
    r = env.client.get(f"/api/archives/{a['id']}")
    assert r.json()["name"] == "Done pile"
    assert r.json()["items"] == []
    assert env.client.get("/api/archives/ffffff").status_code == 404


def test_api_archive_project_header(env):
    # The archive browser's top level: an archive is filed under a project.
    r = env.client.post("/api/archives", json={"name": "Sprint 12", "project": "Web"})
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["project"] == "Web"

    r = env.client.patch(f"/api/archives/{a['id']}", json={"project": "Mobile"})
    assert r.status_code == 200, r.text
    assert r.json()["project"] == "Mobile"
    assert env.client.get("/api/archives").json()[0]["project"] == "Mobile"

    # "" unfiles it again; a missing project leaves it alone.
    assert env.client.patch(f"/api/archives/{a['id']}",
                            json={"project": ""}).json()["project"] == ""
    assert env.client.patch(f"/api/archives/{a['id']}",
                            json={}).json()["project"] == ""
    assert env.client.patch("/api/archives/ffffff",
                            json={"project": "Web"}).status_code == 404


def test_api_archive_and_restore_card(env):
    card = api_accepted_card(env, "Ship it")
    a = env.client.post("/api/archives", json={"name": "Pile"}).json()

    r = env.client.post(f"/api/archives/{a['id']}/cards/{card['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["archive"]["item_count"] == 1
    # off the board
    assert env.client.get(f"/api/cards/{card['id']}").status_code == 404
    assert all(n["id"] != card["id"] for n in env.client.get("/api/cards").json())
    # listed inside the archive
    items = env.client.get(f"/api/archives/{a['id']}").json()["items"]
    assert [n["id"] for n in items] == [card["id"]]
    assert items[0]["archived"] is True

    # restore via the card endpoint (used by the MCP restore_card tool)
    r = env.client.post(f"/api/cards/{card['id']}/restore")
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is False
    assert env.client.get(f"/api/cards/{card['id']}").status_code == 200
    # ...and via the archive-scoped DELETE
    env.client.post(f"/api/archives/{a['id']}/cards/{card['id']}")
    r = env.client.delete(f"/api/archives/{a['id']}/cards/{card['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["archived"] is False
    # wrong archive -> 409, unknown card -> 404
    env.client.post(f"/api/archives/{a['id']}/cards/{card['id']}")
    other = env.client.post("/api/archives", json={"name": "Other"}).json()
    assert env.client.delete(f"/api/archives/{other['id']}/cards/{card['id']}").status_code == 409
    assert env.client.delete(f"/api/archives/{a['id']}/cards/abcdef").status_code == 404
    assert env.client.post("/api/cards/abcdef/restore").status_code == 404


def test_api_archive_card_gates(env):
    a = env.client.post("/api/archives", json={"name": "Pile"}).json()
    draft = env.client.post("/api/cards", json={"title": "D", "body": "b"}).json()
    r = env.client.post(f"/api/archives/{a['id']}/cards/{draft['id']}")
    assert r.status_code == 409
    assert "accepted" in r.json()["detail"]
    assert env.client.post(f"/api/archives/ffffff/cards/{draft['id']}").status_code == 404
    assert env.client.post(f"/api/archives/{a['id']}/cards/abcdef").status_code == 404


def test_api_archive_cards_batch_and_project(env):
    n1 = api_accepted_card(env, "A")
    n2 = api_accepted_card(env, "B")
    a = env.client.post("/api/archives", json={"name": "Deck", "type": "deck"}).json()

    r = env.client.post(f"/api/archives/{a['id']}/cards",
                        json={"card_ids": [n1["id"], n2["id"]]})
    assert r.status_code == 200, r.text
    assert len(r.json()["cards"]) == 2
    assert env.client.get("/api/cards").json() == []

    # whole deck (project): register a project with two cards, archive it
    env.projects.create("web", str(env.tmp_path / "web"))
    api_accepted_card(env, "W1", project="web")
    env.client.post("/api/cards", json={"title": "W2", "body": "b", "project": "web"})
    r = env.client.post(f"/api/archives/{a['id']}/projects/web")
    assert r.status_code == 200, r.text
    assert r.json()["project"] == "web"
    assert len(r.json()["cards"]) == 2
    assert all(p["name"] != "web" for p in env.client.get("/api/projects").json())
    got = env.client.get(f"/api/archives/{a['id']}").json()
    assert len(got["items"]) == 4
    assert got["archived_projects"][0]["name"] == "web"
    # unknown project -> 404, unknown archive -> 404
    assert env.client.post(f"/api/archives/{a['id']}/projects/nope").status_code == 404
    assert env.client.post("/api/archives/ffffff/projects/web").status_code == 404


def test_archive_cards_dedupes_duplicate_ids(stores):
    """Reviewer find: archive_cards([id, id]) must not crash in pass 2 (the
    source file is unlinked twice) — duplicate ids are deduped in pass 1."""
    n = accepted_card(stores.cards, "Dup")
    a = stores.archives.create("Pile")
    out = stores.archives.archive_cards([n["id"], n["id"], n["id"]], a["id"])
    assert [x["id"] for x in out["cards"]] == [n["id"]]
    assert stores.archives.get(a["id"])["item_count"] == 1
    assert len(list((stores.tmp_path / "archives").glob("*/cards/*.md"))) == 1
    with pytest.raises(CardNotFound):
        stores.cards.get(n["id"])


def test_api_archive_cards_duplicate_ids_no_500(env):
    """Schema-valid input with a duplicate id must return 200, not HTTP 500."""
    card = api_accepted_card(env, "Dup card")
    a = env.client.post("/api/archives", json={"name": "Pile"}).json()
    r = env.client.post(f"/api/archives/{a['id']}/cards",
                        json={"card_ids": [card["id"], card["id"]]})
    assert r.status_code == 200, r.text
    assert [n["id"] for n in r.json()["cards"]] == [card["id"]]
    assert env.client.get(f"/api/archives/{a['id']}").json()["item_count"] == 1
    assert env.client.get(f"/api/cards/{card['id']}").status_code == 404


# ----------------------------------------------------------------------
# legacy archives: items written before the notes -> cards rename
# ----------------------------------------------------------------------

def _legacy_archive(stores, name="Old pile", type="deck"):
    """An archive laid out the pre-rename way: items in notes/, no cards/."""
    a = stores.archives.create(name, type=type)
    root = stores.tmp_path / "archives" / f"{name.lower().replace(' ', '-')}-{a['id']}"
    (root / "cards").rmdir()
    (root / "notes").mkdir()
    return a, root / "notes"


def test_legacy_notes_folder_items_are_listed(stores):
    """Archives created before the rename keep their cards in notes/; they must
    still count and list (the rename only changed where NEW items are written)."""
    card = accepted_card(stores.cards, "Old work")
    a, notes_dir = _legacy_archive(stores)
    stores.archives.archive_card(card["id"], a["id"])
    (stores.tmp_path / "archives" / f"old-pile-{a['id']}" / "cards").rename(notes_dir)

    listed = {x["id"]: x for x in stores.archives.list()}
    assert listed[a["id"]]["item_count"] == 1
    got = stores.archives.get(a["id"])
    assert [n["id"] for n in got["items"]] == [card["id"]]
    assert got["items"][0]["archived"] is True


def test_legacy_notes_folder_card_can_be_restored(stores):
    card = accepted_card(stores.cards, "Old work")
    a, notes_dir = _legacy_archive(stores)
    stores.archives.archive_card(card["id"], a["id"])
    (stores.tmp_path / "archives" / f"old-pile-{a['id']}" / "cards").rename(notes_dir)

    back = stores.archives.restore_card(card["id"], archive_id=a["id"])
    assert back["id"] == card["id"]
    assert back["archived"] is False
    assert stores.cards.get(card["id"])["status"] == "accepted"
    assert stores.archives.get(a["id"])["items"] == []


def test_legacy_notes_folder_card_moves_to_a_new_archive(stores):
    """Moving a legacy card between archives lands it in the new cards/ folder."""
    card = accepted_card(stores.cards, "Old work")
    a, notes_dir = _legacy_archive(stores)
    stores.archives.archive_card(card["id"], a["id"])
    (stores.tmp_path / "archives" / f"old-pile-{a['id']}" / "cards").rename(notes_dir)

    b = stores.archives.create("New pile")
    stores.archives.archive_card(card["id"], b["id"])
    assert stores.archives.get(a["id"])["items"] == []
    assert [n["id"] for n in stores.archives.get(b["id"])["items"]] == [card["id"]]
    assert list((stores.tmp_path / "archives").glob("*/notes/*.md")) == []
    assert len(list((stores.tmp_path / "archives").glob("*/cards/*.md"))) == 1


def test_new_archives_still_write_to_cards_folder(stores):
    """The legacy fallback must not change where new items go."""
    card = accepted_card(stores.cards, "Fresh")
    a = stores.archives.create("Fresh pile")
    stores.archives.archive_card(card["id"], a["id"])
    root = stores.tmp_path / "archives" / f"fresh-pile-{a['id']}"
    assert list((root / "cards").glob("*.md"))
    assert not (root / "notes").exists()


# ----------------------------------------------------------------------
# archiving a whole deck (stack) as one unit
#
# The board's decks are the structure its finished work is organized in, so
# retiring a deck is one action: its cards move into one archive, the deck is
# recorded in the manifest (title + card order) and dissolved from the board.
# ----------------------------------------------------------------------

def deck(stores, titles, title="Deck feature", status="accepted"):
    """Build a deck of cards in one column; returns (stack, cards)."""
    cards = [accepted_card(stores.cards, t) if status == "accepted"
             else done_card(stores.cards, t) for t in titles]
    stack = stores.stacks.create(status, title, [n["id"] for n in cards])
    return stack, cards


def test_archive_stack_moves_the_whole_deck_and_records_it(stores):
    stack, cards = deck(stores, ["Deck feature", "Deck bug", "Deck button"])
    a = stores.archives.create("Retired decks", type="deck")

    out = stores.archives.archive_stack(stack["id"], a["id"])

    assert out["stack"]["id"] == stack["id"]
    assert [n["id"] for n in out["cards"]] == [n["id"] for n in cards]
    got = stores.archives.get(a["id"])
    # the archive's item list is sorted by file name (as for every archive);
    # the deck's own order lives in the manifest below
    assert {n["id"] for n in got["items"]} == {n["id"] for n in cards}
    # the deck survives the move as structure: title + card order in the manifest
    assert len(got["archived_stacks"]) == 1
    shelved = got["archived_stacks"][0]
    assert shelved["id"] == stack["id"]
    assert shelved["title"] == "Deck feature"
    assert shelved["cards"] == [n["id"] for n in cards]
    assert shelved["archived_at"]
    # the deck and its cards are off the board
    assert stores.stacks.list() == []
    assert stores.cards.list() == []


def test_archive_stack_without_archive_creates_one_named_after_the_deck(stores):
    stack, _ = deck(stores, ["Token count", "Token types"], title="Token accounting")

    out = stores.archives.archive_stack(stack["id"])

    a = out["archive"]
    assert a["name"] == "Token accounting"
    assert a["type"] == "deck"
    assert a["item_count"] == 2
    assert [s["id"] for s in a["archived_stacks"]] == [stack["id"]]
    # a second deck with the same title reuses that archive instead of failing
    stack2, _ = deck(stores, ["More tokens", "Even more"], title="Token accounting")
    out2 = stores.archives.archive_stack(stack2["id"])
    assert out2["archive"]["id"] == a["id"]
    assert out2["archive"]["item_count"] == 4
    assert [s["id"] for s in out2["archive"]["archived_stacks"]] == [stack["id"], stack2["id"]]


def test_archive_stack_rejects_a_deck_that_is_not_accepted(stores):
    """A deck of done-but-unaccepted cards must not leave the board, and a
    rejected deck moves nothing (validation before any file moves)."""
    stack, cards = deck(stores, ["One", "Two"], title="Waiting", status="done")
    a = stores.archives.create("Pile")

    with pytest.raises(InvalidTransition):
        stores.archives.archive_stack(stack["id"], a["id"])

    assert stores.archives.get(a["id"])["items"] == []
    assert stores.stacks.get(stack["id"])["ids"] == [n["id"] for n in cards]
    assert {n["id"] for n in stores.cards.list()} == {n["id"] for n in cards}


def test_archive_stack_unknown_deck_and_missing_stack_store(stores):
    a = stores.archives.create("Pile")
    with pytest.raises(StackNotFound):
        stores.archives.archive_stack("st-nope", a["id"])
    bare = ArchiveStore(stores.tmp_path / "archives2", stores.cards)
    with pytest.raises(ArchiveError):
        bare.archive_stack("st-nope")


def test_api_archive_stack(env):
    ids = [api_accepted_card(env, f"Deck card {i}")["id"] for i in range(2)]
    stack = env.client.post("/api/stacks",
                            json={"title": "Deck feature", "card_ids": ids}).json()
    a = env.client.post("/api/archives",
                        json={"name": "Retired", "type": "deck"}).json()

    r = env.client.post(f"/api/archives/{a['id']}/stacks/{stack['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["archive"]["item_count"] == 2
    assert env.client.get("/api/stacks").json() == []
    assert env.client.get("/api/cards").json() == []

    # without an archive id, one named after the deck is created
    ids2 = [api_accepted_card(env, f"Token card {i}")["id"] for i in range(2)]
    stack2 = env.client.post("/api/stacks",
                             json={"title": "Token accounting", "card_ids": ids2}).json()
    r = env.client.post(f"/api/archives/stacks/{stack2['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["archive"]["name"] == "Token accounting"
    assert r.json()["archive"]["type"] == "deck"
    assert env.client.post("/api/archives/stacks/st-nope").status_code == 404

# project headers: the archive tree's third level
# ----------------------------------------------------------------------

def test_get_archive_groups_cards_under_project_headers(stores):
    """Spec (card f0d199): "Make one more level to the drop down menu in the
    archive so we can have a project header. Let the archives be project
    related." An archive's cards are grouped under one header per project
    (the card's `project` frontmatter), unfiled cards last under name None."""
    a = stores.archives.create("Project pile", type="project")
    a1 = accepted_card(stores.cards, "Alpha one", project="Alpha")
    a2 = accepted_card(stores.cards, "Alpha two", project="Alpha")
    b1 = accepted_card(stores.cards, "Beta one", project="Beta")
    loose = accepted_card(stores.cards, "Loose")
    stores.archives.archive_cards([a1["id"], a2["id"], b1["id"], loose["id"]], a["id"])

    got = stores.archives.get(a["id"])
    groups = got["projects"]
    assert [g["name"] for g in groups] == ["Alpha", "Beta", None]
    by = {g["name"]: g for g in groups}
    assert by["Alpha"]["count"] == 2
    assert {n["id"] for n in by["Alpha"]["cards"]} == {a1["id"], a2["id"]}
    assert by["Beta"]["count"] == 1
    assert [n["id"] for n in by["Beta"]["cards"]] == [b1["id"]]
    assert by[None]["count"] == 1
    assert [n["id"] for n in by[None]["cards"]] == [loose["id"]]
    # the flat item list stays for the existing consumers (modal, MCP)
    assert len(got["items"]) == 4


def test_archive_projects_grouping_is_empty_for_an_empty_archive(stores):
    a = stores.archives.create("Nothing yet")
    assert stores.archives.get(a["id"])["projects"] == []


def test_archived_deck_keeps_its_project_header_without_cards(stores):
    """A deck archived wholesale keeps its header even when it holds no cards,
    so the archive stays project-related after its cards are restored."""
    stores.projects.create("Empty deck", str(stores.tmp_path / "empty-deck"))
    a = stores.archives.create("Deck pile", type="deck")
    stores.archives.archive_project("Empty deck", a["id"])

    groups = stores.archives.get(a["id"])["projects"]
    assert [g["name"] for g in groups] == ["Empty deck"]
    assert groups[0]["count"] == 0
    assert groups[0]["cards"] == []
    assert groups[0]["archived_at"]


def test_archived_deck_header_merges_with_its_cards(stores):
    """The manifest's archived deck and the cards' own `project` field name the
    same project: one header, not two."""
    stores.projects.create("web", str(stores.tmp_path / "web"))
    card = accepted_card(stores.cards, "W1", project="web")
    a = stores.archives.create("Deck pile", type="deck")
    stores.archives.archive_project("web", a["id"])

    groups = stores.archives.get(a["id"])["projects"]
    assert [g["name"] for g in groups] == ["web"]
    assert groups[0]["count"] == 1
    assert [n["id"] for n in groups[0]["cards"]] == [card["id"]]


def test_api_archive_get_exposes_project_headers(env):
    a = env.client.post("/api/archives", json={"name": "Pile", "type": "project"}).json()
    one = api_accepted_card(env, "One", project="Alpha")
    two = api_accepted_card(env, "Two")
    env.client.post(f"/api/archives/{a['id']}/cards",
                    json={"card_ids": [one["id"], two["id"]]})

    got = env.client.get(f"/api/archives/{a['id']}").json()
    assert [g["name"] for g in got["projects"]] == ["Alpha", None]
    assert got["projects"][0]["count"] == 1
    assert got["projects"][0]["cards"][0]["id"] == one["id"]
    assert got["projects"][1]["cards"][0]["id"] == two["id"]
