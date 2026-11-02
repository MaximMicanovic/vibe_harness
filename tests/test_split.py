"""Tests for the split tree: a card's parent, its children, and the report.

A worker that breaks its spec into sub-cards is decomposing the card it is
working on, so those sub-cards become its children (`parent_id`). The board
shows the tree; `/api/split-report` answers the only question that decides
whether an orchestrator earns its place — do split cards cost less per accepted
outcome than unsplit ones?
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.cards import DependencyError, CardStore
from app.projects import ProjectStore
from app.runner import SessionRunner

FAKE_VIBE = "#!/bin/sh\nexit 0\n"


@pytest.fixture
def store(tmp_path) -> CardStore:
    return CardStore(tmp_path / "cards")


# ----------------------------------------------------------------------
# parent_id on the store
# ----------------------------------------------------------------------

def test_a_card_starts_without_a_parent(store):
    card = store.create("Top level", "spec")
    assert card["parent_id"] is None


def children_of(store, card_id):
    """The cards split out of `card_id`, oldest first — what the board reads
    off `parent_id` (the store keeps no helper for it)."""
    kids = [n for n in store.list() if n.get("parent_id") == card_id]
    kids.sort(key=lambda n: n.get("created_at") or "")
    return kids


def test_create_with_a_parent(store):
    parent = store.create("Parent", "spec")
    child = store.create("Child", "sub", parent_id=parent["id"])
    assert child["parent_id"] == parent["id"]
    assert [n["id"] for n in children_of(store, parent["id"])] == [child["id"]]
    assert children_of(store, child["id"]) == []


def test_children_are_oldest_first(store):
    parent = store.create("Parent", "spec")
    kids = [store.create("Child " + str(i), "sub", parent_id=parent["id"]) for i in range(3)]
    assert [n["id"] for n in children_of(store, parent["id"])] == [k["id"] for k in kids]


def test_unknown_parent_is_rejected(store):
    with pytest.raises(DependencyError):
        store.create("Child", "sub", parent_id="nope")


def test_a_card_cannot_be_its_own_parent(store):
    card = store.create("Card", "spec")
    with pytest.raises(DependencyError):
        store.update(card["id"], parent_id=card["id"])


def test_a_cycle_is_rejected(store):
    a = store.create("A", "spec")
    b = store.create("B", "sub", parent_id=a["id"])
    c = store.create("C", "sub", parent_id=b["id"])
    # Making A a child of its own grandchild would close the loop.
    with pytest.raises(DependencyError):
        store.update(a["id"], parent_id=c["id"])
    # ...and the tree is untouched.
    assert store.get(a["id"])["parent_id"] is None


def test_reparenting_and_detaching(store):
    a = store.create("A", "spec")
    b = store.create("B", "spec")
    child = store.create("Child", "sub", parent_id=a["id"])
    store.update(child["id"], parent_id=b["id"])
    assert store.get(child["id"])["parent_id"] == b["id"]
    assert children_of(store, a["id"]) == []
    # "" detaches it again.
    store.update(child["id"], parent_id="")
    assert store.get(child["id"])["parent_id"] is None


def test_parent_id_survives_a_status_change(store):
    parent = store.create("Parent", "spec")
    child = store.create("Child", "sub", parent_id=parent["id"])
    store.set_status(child["id"], "running")
    assert store.get(child["id"])["parent_id"] == parent["id"]


# ----------------------------------------------------------------------
# the report
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
    app = create_app(config=config, store=store, runner=runner, projects=projects)
    return SimpleNamespace(client=TestClient(app), store=store, tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def _accept(store: CardStore, card_id: str, tokens: int) -> None:
    """Walk a card to accepted with a token count on its session."""
    store.set_status(card_id, "running")
    store.set_status(card_id, "done",
                     session_patch={"review": {"approved": True, "round": 1},
                                    "tokens": tokens})
    store.accept(card_id)


def test_report_is_empty_on_a_fresh_board(env):
    report = env.client.get("/api/split-report").json()
    assert report["split"]["cards"] == 0
    assert report["unsplit"]["cards"] == 0
    assert report["trees"] == []


def test_report_separates_split_from_unsplit(env):
    plain = env.store.create("Plain", "spec")
    _accept(env.store, plain["id"], 1000)
    parent = env.store.create("Parent", "spec")
    kid = env.store.create("Kid", "sub", parent_id=parent["id"])
    _accept(env.store, parent["id"], 400)
    _accept(env.store, kid["id"], 600)

    report = env.client.get("/api/split-report").json()
    assert report["unsplit"]["cards"] == 1
    assert report["unsplit"]["accepted"] == 1
    assert report["unsplit"]["tokens"] == 1000
    assert report["unsplit"]["tokens_per_accepted"] == 1000

    assert report["split"]["cards"] == 1
    assert report["split"]["accepted"] == 1
    # The split's cost is the whole subtree: 400 + 600.
    assert report["split"]["tokens"] == 1000
    assert report["split"]["tokens_per_accepted"] == 1000


def test_report_does_not_double_count_children(env):
    parent = env.store.create("Parent", "spec")
    kids = [env.store.create("Kid " + str(i), "sub", parent_id=parent["id"]) for i in range(2)]
    _accept(env.store, parent["id"], 100)
    for k in kids:
        _accept(env.store, k["id"], 50)
    report = env.client.get("/api/split-report").json()
    # 100 + 50 + 50, counted once — children are not separate outcomes.
    assert report["split"]["tokens"] == 200
    assert report["split"]["cards"] == 1
    assert report["unsplit"]["cards"] == 0


def test_report_lists_the_trees(env):
    parent = env.store.create("Parent", "spec")
    env.store.create("Kid", "sub", parent_id=parent["id"])
    report = env.client.get("/api/split-report").json()
    assert len(report["trees"]) == 1
    tree = report["trees"][0]
    assert tree["id"] == parent["id"]
    assert tree["children"] == 1
    assert tree["title"] == "Parent"


def test_report_has_no_ratio_without_accepted_outcomes(env):
    parent = env.store.create("Parent", "spec")
    env.store.create("Kid", "sub", parent_id=parent["id"])
    report = env.client.get("/api/split-report").json()
    assert report["split"]["accepted"] == 0
    assert report["split"]["tokens_per_accepted"] is None


# ----------------------------------------------------------------------
# the API carries parent_id
# ----------------------------------------------------------------------

def test_api_creates_a_child(env):
    parent = env.client.post("/api/cards", json={"title": "Parent", "body": "spec"}).json()
    r = env.client.post("/api/cards", json={"title": "Child", "body": "sub",
                                            "parent_id": parent["id"]})
    assert r.status_code == 201, r.text
    assert r.json()["parent_id"] == parent["id"]


def test_api_rejects_a_cycle(env):
    a = env.client.post("/api/cards", json={"title": "A", "body": "x"}).json()
    b = env.client.post("/api/cards", json={"title": "B", "body": "x",
                                            "parent_id": a["id"]}).json()
    r = env.client.put(f"/api/cards/{a['id']}", json={"parent_id": b["id"]})
    assert r.status_code == 409
    assert "cycle" in r.json()["detail"]


def test_api_rejects_an_unknown_parent(env):
    r = env.client.post("/api/cards", json={"title": "Child", "body": "x",
                                            "parent_id": "nope"})
    assert r.status_code == 409
