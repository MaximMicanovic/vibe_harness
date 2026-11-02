"""The steering channel: user -> worker messages over the card's mailbox.

The chat popup's footer posts guidance with
``POST /api/cards/<id>/messages`` and reads the conversation back with
``GET /api/cards/<id>/messages``. The message lands in the same per-card
mailbox the worker's MCP server reads, so the worker's next
``wait_for_message(timeout_seconds=0)`` returns it — and the worker's reply
(``post_message(to="user")``) shows up in the popup.
"""
from __future__ import annotations

import pytest

from app import mailbox
from test_api import create_card, make_env
from test_mailbox import Rpc


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def _mdir(env, card_id: str):
    return mailbox.mailbox_dir(env.sessions_dir, card_id)


def test_post_message_appends_a_user_message_for_the_worker(env):
    card = create_card(env.client)
    r = env.client.post(f"/api/cards/{card['id']}/messages",
                        json={"text": "focus on the tests first"})
    assert r.status_code == 201, r.text
    msg = r.json()
    assert msg["from"] == "user" and msg["to"] == "worker"
    assert msg["text"] == "focus on the tests first"
    assert msg["seq"] == 1 and msg["ts"]

    # Stored in the card's mailbox, exactly where the worker's MCP server reads.
    assert [m["text"] for m in mailbox.list_messages(_mdir(env, card["id"]))] == [
        "focus on the tests first"]


def test_the_workers_next_wait_returns_the_posted_message(env):
    """Acceptance: sending from the popup appends a message the worker's next
    `wait_for_message` returns."""
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/messages",
                    json={"text": "use the existing helper"})

    worker = Rpc(_mdir(env, card["id"]), "worker")
    try:
        out = worker.tool("wait_for_message", {"timeout_seconds": 0})
        got = out["structuredContent"]["message"]
        assert got["text"] == "use the existing helper"
        assert got["from"] == "user" and got["to"] == "worker"
    finally:
        worker.close()


def test_list_messages_shows_both_directions(env):
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/messages", json={"text": "hello"})
    # The worker replies to the person watching.
    mailbox.post_message(_mdir(env, card["id"]), "worker", "user", "on it")

    r = env.client.get(f"/api/cards/{card['id']}/messages")
    assert r.status_code == 200, r.text
    msgs = r.json()["messages"]
    assert [(m["from"], m["to"], m["text"]) for m in msgs] == [
        ("user", "worker", "hello"), ("worker", "user", "on it")]


def test_messages_are_empty_for_a_card_with_no_conversation(env):
    card = create_card(env.client)
    r = env.client.get(f"/api/cards/{card['id']}/messages")
    assert r.status_code == 200
    assert r.json()["messages"] == []


def test_post_message_rejects_empty_text(env):
    card = create_card(env.client)
    r = env.client.post(f"/api/cards/{card['id']}/messages", json={"text": "   "})
    assert r.status_code == 400, r.text
    assert env.client.get(f"/api/cards/{card['id']}/messages").json()["messages"] == []


def test_messages_unknown_card_is_404(env):
    assert env.client.get("/api/cards/nope/messages").status_code == 404
    assert env.client.post("/api/cards/nope/messages",
                           json={"text": "hi"}).status_code == 404


def test_messages_survive_a_new_run(env):
    """The mailbox is per card, not per round: guidance posted between runs is
    still there for the next session."""
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/messages", json={"text": "round 1"})
    env.client.post(f"/api/cards/{card['id']}/messages", json={"text": "round 2"})
    msgs = env.client.get(f"/api/cards/{card['id']}/messages").json()["messages"]
    assert [m["seq"] for m in msgs] == [1, 2]
