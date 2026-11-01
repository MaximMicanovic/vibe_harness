"""Tests for card stacks: app.stacks.StackStore + the /api/stacks routes.

A stack ("pile") groups cards that sit in the same board column and carries an
optional header (title). Stacks used to live in the browser's localStorage,
which meant an LLM could neither see nor change them; they are server state now
(stacks.json) so the MCP tools can build piles and move cards between them.

Invariants under test:

- every member renders in the stack's column (400 otherwise);
- a card is in at most one stack (adding it elsewhere detaches it);
- a stack needs at least two members — dropping below that dissolves it;
- members that vanish or change column are pruned on read.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.cards import CardStore
from app.projects import ProjectStore
from app.runner import SessionRunner
from app.stacks import StackError, StackNotFound, StackStore

FAKE_VIBE = "#!/bin/sh\nexit 0\n"


@pytest.fixture
def stores(tmp_path) -> SimpleNamespace:
    cards = CardStore(tmp_path / "cards")
    stacks = StackStore(tmp_path / "stacks.json", cards)
    return SimpleNamespace(cards=cards, stacks=stacks, tmp_path=tmp_path)


def card(store: CardStore, title: str, status: str = "done") -> dict:
    """Create a card and walk it to `status` (done/accepted need a review)."""
    card = store.create(title, "body")
    if status == "draft":
        return card
    store.set_status(card["id"], "running")
    if status == "running":
        return store.get(card["id"])
    store.set_status(card["id"], "done",
                     session_patch={"review": {"approved": True, "round": 1}})
    if status == "accepted":
        return store.accept(card["id"])
    return store.get(card["id"])


# ----------------------------------------------------------------------
# store: create / read
# ----------------------------------------------------------------------

def test_create_stack_keeps_member_order_and_title(stores):
    a = card(stores.cards, "a")
    b = card(stores.cards, "b")
    stack = stores.stacks.create("done", "Merge queue", [a["id"], b["id"]])
    assert stack["id"].startswith("st-")
    assert stack["col"] == "done"
    assert stack["title"] == "Merge queue"
    # ids are bottom -> top: the drop target (last) is painted on top.
    assert stack["ids"] == [a["id"], b["id"]]
    assert stores.stacks.get(stack["id"])["title"] == "Merge queue"
    assert stores.stacks.find_for_card(b["id"])["id"] == stack["id"]


def test_stack_needs_two_cards(stores):
    a = card(stores.cards, "a")
    with pytest.raises(StackError):
        stores.stacks.create("done", "", [a["id"]])


def test_stack_rejects_cards_from_another_column(stores):
    a = card(stores.cards, "a")
    draft = card(stores.cards, "draft card", status="draft")
    with pytest.raises(StackError):
        stores.stacks.create("done", "", [a["id"], draft["id"]])


def test_stack_rejects_unknown_card_and_column(stores):
    a = card(stores.cards, "a")
    b = card(stores.cards, "b")
    with pytest.raises(StackError):
        stores.stacks.create("done", "", [a["id"], "nope"])
    with pytest.raises(StackError):
        stores.stacks.create("not-a-column", "", [a["id"], b["id"]])


def test_stopped_cards_stack_in_the_failed_column(stores):
    a = card(stores.cards, "a", status="running")
    b = card(stores.cards, "b", status="running")
    stores.cards.set_status(a["id"], "stopped")
    stores.cards.set_status(b["id"], "stopped")
    stack = stores.stacks.create("failed", "", [a["id"], b["id"]])
    assert stack["col"] == "failed"


def test_stacks_persist_to_disk(stores):
    a = card(stores.cards, "a")
    b = card(stores.cards, "b")
    stack = stores.stacks.create("done", "Pile", [a["id"], b["id"]])
    assert (stores.tmp_path / "stacks.json").exists()
    # A fresh store over the same file sees the same pile.
    again = StackStore(stores.tmp_path / "stacks.json", stores.cards)
    assert again.get(stack["id"])["title"] == "Pile"


# ----------------------------------------------------------------------
# store: rename / move cards / dissolve
# ----------------------------------------------------------------------

def test_rename_stack(stores):
    a = card(stores.cards, "a")
    b = card(stores.cards, "b")
    stack = stores.stacks.create("done", "", [a["id"], b["id"]])
    renamed = stores.stacks.update(stack["id"], title="  Sprint 12  ")
    assert renamed["title"] == "Sprint 12"
    # An empty title clears the header (the board shows a placeholder).
    assert stores.stacks.update(stack["id"], title="")["title"] == ""


def test_add_cards_moves_a_card_out_of_its_old_stack(stores):
    a = card(stores.cards, "a")
    b = card(stores.cards, "b")
    c = card(stores.cards, "c")
    first = stores.stacks.create("done", "", [a["id"], b["id"]])
    second = stores.stacks.create("done", "", [c["id"], card(stores.cards, "d")["id"]])
    stores.stacks.add_cards(second["id"], [a["id"]])
    # a left the first pile, which keeps b (still >= 2? no: it drops to one and
    # is dissolved) — so only the second pile survives.
    assert stores.stacks.find_for_card(a["id"])["id"] == second["id"]
    assert stores.stacks.find_for_card(b["id"]) is None
    with pytest.raises(StackNotFound):
        stores.stacks.get(first["id"])


def test_remove_card_dissolves_a_pile_below_two(stores):
    a = card(stores.cards, "a")
    b = card(stores.cards, "b")
    stack = stores.stacks.create("done", "", [a["id"], b["id"]])
    result = stores.stacks.remove_card(a["id"])
    assert result["dissolved"] is True
    assert result["stack"] is None
    assert stores.stacks.list() == []
    with pytest.raises(StackNotFound):
        stores.stacks.get(stack["id"])


def test_remove_card_keeps_a_pile_of_three(stores):
    ids = [card(stores.cards, t)["id"] for t in "abc"]
    stack = stores.stacks.create("done", "", ids)
    result = stores.stacks.remove_card(ids[0])
    assert result["dissolved"] is False
    assert result["stack"]["ids"] == ids[1:]
    assert stores.stacks.get(stack["id"])["ids"] == ids[1:]


def test_delete_stack_leaves_cards_on_the_board(stores):
    a = card(stores.cards, "a")
    b = card(stores.cards, "b")
    stack = stores.stacks.create("done", "", [a["id"], b["id"]])
    assert stores.stacks.delete(stack["id"])["cards"] == [a["id"], b["id"]]
    assert stores.stacks.list() == []
    assert stores.cards.get(a["id"])["status"] == "done"


def test_unknown_stack_raises_not_found(stores):
    with pytest.raises(StackNotFound):
        stores.stacks.get("st-nope")
    with pytest.raises(StackNotFound):
        stores.stacks.update("st-nope", title="x")
    with pytest.raises(StackNotFound):
        stores.stacks.delete("st-nope")
    with pytest.raises(StackNotFound):
        stores.stacks.remove_card("nope")


def test_prune_drops_members_that_changed_column(stores):
    # Draft cards can still move (draft -> running); done cards are terminal.
    a = card(stores.cards, "a", status="draft")
    b = card(stores.cards, "b", status="draft")
    c = card(stores.cards, "c", status="draft")
    stack = stores.stacks.create("draft", "", [a["id"], b["id"], c["id"]])
    # b starts running: it no longer renders in the draft column.
    stores.cards.set_status(b["id"], "running")
    assert stores.stacks.get(stack["id"])["ids"] == [a["id"], c["id"]]


def test_prune_dissolves_a_pile_that_lost_its_members(stores):
    a = card(stores.cards, "a", status="draft")
    b = card(stores.cards, "b", status="draft")
    stores.stacks.create("draft", "", [a["id"], b["id"]])
    stores.cards.set_status(a["id"], "running")
    stores.cards.set_status(b["id"], "running")
    assert stores.stacks.list() == []


def test_stack_cards_entry_point_creates_then_adds(stores):
    a = card(stores.cards, "a")
    b = card(stores.cards, "b")
    c = card(stores.cards, "c")
    created = stores.stacks.stack_cards([a["id"], b["id"]], title="Pile")
    assert created["col"] == "done" and created["title"] == "Pile"
    grown = stores.stacks.stack_cards([c["id"]], stack_id=created["id"])
    assert grown["ids"] == [a["id"], b["id"], c["id"]]
    with pytest.raises(StackError):
        stores.stacks.stack_cards([])


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
    stacks = StackStore(tmp_path / "stacks.json", store)
    app = create_app(config=config, store=store, runner=runner, projects=projects,
                     stacks=stacks)
    client = TestClient(app)
    return SimpleNamespace(client=client, store=store, stacks=stacks,
                           tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def test_api_create_list_rename_and_dissolve(env):
    a = card(env.store, "a")
    b = card(env.store, "b")

    r = env.client.post("/api/stacks", json={"card_ids": [a["id"], b["id"]],
                                             "title": "Merge queue"})
    assert r.status_code == 201, r.text
    stack = r.json()
    assert stack["title"] == "Merge queue" and stack["col"] == "done"

    listed = env.client.get("/api/stacks").json()
    assert [s["id"] for s in listed] == [stack["id"]]

    r = env.client.patch(f"/api/stacks/{stack['id']}", json={"title": "Renamed"})
    assert r.status_code == 200 and r.json()["title"] == "Renamed"

    r = env.client.delete(f"/api/stacks/{stack['id']}")
    assert r.status_code == 200 and r.json()["deleted"] == stack["id"]
    assert env.client.get("/api/stacks").json() == []


def test_api_create_infers_the_column(env):
    a = card(env.store, "a", status="accepted")
    b = card(env.store, "b", status="accepted")
    r = env.client.post("/api/stacks", json={"card_ids": [a["id"], b["id"]]})
    assert r.status_code == 201 and r.json()["col"] == "accepted"


def test_api_rejects_mixed_columns_and_unknown_stacks(env):
    a = card(env.store, "a")
    draft = card(env.store, "draft", status="draft")
    r = env.client.post("/api/stacks", json={"card_ids": [a["id"], draft["id"]],
                                             "col": "done"})
    assert r.status_code == 400
    assert "column" in r.json()["detail"]

    # The schema itself refuses a one-card pile (the store would 400 too).
    r = env.client.post("/api/stacks", json={"card_ids": [a["id"]]})
    assert r.status_code == 422

    assert env.client.get("/api/stacks/st-nope").status_code == 404
    assert env.client.patch("/api/stacks/st-nope", json={"title": "x"}).status_code == 404
    assert env.client.delete("/api/stacks/st-nope").status_code == 404
    assert env.client.post("/api/stacks/st-nope/cards",
                           json={"card_ids": [a["id"]]}).status_code == 404


def test_api_add_and_remove_cards(env):
    a = card(env.store, "a")
    b = card(env.store, "b")
    c = card(env.store, "c")
    stack = env.client.post("/api/stacks",
                            json={"card_ids": [a["id"], b["id"]]}).json()

    r = env.client.post(f"/api/stacks/{stack['id']}/cards", json={"card_ids": [c["id"]]})
    assert r.status_code == 200 and r.json()["ids"] == [a["id"], b["id"], c["id"]]

    r = env.client.delete(f"/api/stacks/{stack['id']}/cards/{c['id']}")
    assert r.status_code == 200 and r.json()["dissolved"] is False
    assert r.json()["stack"]["ids"] == [a["id"], b["id"]]

    # Pulling a card out of whichever pile holds it.
    r = env.client.post(f"/api/cards/{a['id']}/unstack")
    assert r.status_code == 200 and r.json()["dissolved"] is True
    assert env.client.get("/api/stacks").json() == []


def test_api_patch_replaces_members(env):
    a = card(env.store, "a")
    b = card(env.store, "b")
    c = card(env.store, "c")
    stack = env.client.post("/api/stacks",
                            json={"card_ids": [a["id"], b["id"]]}).json()
    r = env.client.patch(f"/api/stacks/{stack['id']}",
                         json={"card_ids": [b["id"], c["id"]]})
    assert r.status_code == 200 and r.json()["ids"] == [b["id"], c["id"]]


def test_api_unstack_unknown_card_is_404(env):
    assert env.client.post("/api/cards/nope/unstack").status_code == 404


def test_app_without_a_stack_store_keeps_it_beside_the_cards(tmp_path):
    """create_app must not fall back to the project's real stacks.json.

    An app built with a temp card store (every test app) would otherwise prune
    the developer's board against an empty card list and save it empty on the
    first GET /api/stacks — which is exactly how the live board lost its decks.
    """
    store = CardStore(tmp_path / "cards")
    a = card(store, "a", status="accepted")
    b = card(store, "b", status="accepted")
    (tmp_path / "stacks.json").write_text(json.dumps({"stacks": [{
        "id": "st-1", "col": "accepted", "title": "Pile",
        "ids": [a["id"], b["id"]],
    }]}))
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(FAKE_VIBE)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30)
    projects = ProjectStore(tmp_path / "projects.json")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects)
    app = create_app(config=config, store=store, runner=runner, projects=projects)
    with TestClient(app) as client:
        assert [s["id"] for s in client.get("/api/stacks").json()] == ["st-1"]
