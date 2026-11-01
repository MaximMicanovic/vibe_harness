"""Tests for the per-card Q&A (ask_user) feature: app/questions.py (store),
the HTTP API (questions on cards, long-poll wait, answer/cancel), the card
payload enrichment, and the runner cancelling pending questions when a
session ends.

Hermetic: temp dirs + fake vibe binary, like test_api.py / test_mcp_server.py.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import create_app
from app.cards import CardStore
from app.projects import ProjectStore
from app.questions import (
    ANSWERED,
    CANCELLED,
    PENDING,
    QuestionError,
    QuestionNotFound,
    QuestionNotPending,
    QuestionStore,
)
from app.runner import SessionRunner

FAKE_VIBE_SLOW = """#!/bin/sh
workdir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2;;
    *) shift;;
  esac
done
mkdir -p "$workdir"
echo "fake vibe slow start"
trap 'echo "fake vibe terminated"; exit 143' TERM
sleep 30
exit 0
"""


# ---------------------------------------------------------------- store

def test_store_ask_answer_list_persist(tmp_path):
    qs = QuestionStore(tmp_path / "sessions")
    q = qs.ask("n1", "sync or async?", ["sync", "async"])
    assert q["status"] == PENDING and q["id"] and q["options"] == ["sync", "async"]
    assert qs.pending("n1")["id"] == q["id"]
    assert [x["id"] for x in qs.list("n1")] == [q["id"]]

    answered = qs.answer("n1", q["id"], "async")
    assert answered["status"] == ANSWERED and answered["answer"] == "async"
    assert answered["answered_by"] == "user" and answered["answered_at"]
    assert qs.pending("n1") is None
    # answering twice -> QuestionNotPending
    with pytest.raises(QuestionNotPending):
        qs.answer("n1", q["id"], "again")
    with pytest.raises(QuestionNotFound):
        qs.get("n1", "nope00")

    # state survives a fresh store instance (file-backed)
    qs2 = QuestionStore(tmp_path / "sessions")
    assert qs2.get("n1", q["id"])["answer"] == "async"

    # validation
    with pytest.raises(QuestionError):
        qs.ask("n1", "   ")
    with pytest.raises(QuestionError):
        qs.ask("n1", "q", ["ok", ""])
    with pytest.raises(QuestionError):
        qs.ask("n1", "q", [])


def test_store_cancel_and_wait(tmp_path):
    qs = QuestionStore(tmp_path / "sessions")
    q = qs.ask("n2", "proceed?")
    # wait with no answer returns the still-pending record after the timeout
    t0 = time.monotonic()
    rec = qs.wait("n2", q["id"], 0.3)
    assert rec["status"] == PENDING
    assert time.monotonic() - t0 >= 0.25

    # a waiter in another thread wakes as soon as the answer lands
    result = {}
    def waiter():
        result["rec"] = qs.wait("n2", q["id"], 10.0)
    th = threading.Thread(target=waiter, daemon=True)
    th.start()
    time.sleep(0.2)
    qs.answer("n2", q["id"], "yes, go")
    th.join(timeout=5)
    assert result["rec"]["status"] == ANSWERED and result["rec"]["answer"] == "yes, go"

    # cancel: single + cancel-all-pending
    q2 = qs.ask("n2", "another?")
    q3 = qs.ask("n2", "third?")
    cancelled = qs.cancel("n2", q2["id"])
    assert len(cancelled) == 1 and cancelled[0]["status"] == CANCELLED
    with pytest.raises(QuestionNotPending):
        qs.cancel("n2", q2["id"])  # already cancelled
    all_cancelled = qs.cancel("n2")  # cancels the remaining pending one
    assert [c["id"] for c in all_cancelled] == [q3["id"]]
    assert qs.pending("n2") is None

    # drop forgets the in-memory state; the file is reloaded on next access
    qs.drop("n2")
    assert len(qs.list("n2")) == 3


# ---------------------------------------------------------------- HTTP API

def make_env(tmp_path: Path, script: str = FAKE_VIBE_SLOW) -> SimpleNamespace:
    store = CardStore(tmp_path / "cards")
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(script)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30)
    projects = ProjectStore(tmp_path / "projects.json")
    runner = SessionRunner(store, config, sessions_dir=tmp_path / "sessions",
                           projects=projects)
    app = create_app(config=config, store=store, runner=runner, projects=projects)
    return SimpleNamespace(client=TestClient(app), store=store, runner=runner,
                           sessions_dir=tmp_path / "sessions", tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def create_card(client, title="Q card", body="spec"):
    r = client.post("/api/cards", json={"title": title, "body": body})
    assert r.status_code == 201, r.text
    return r.json()


def test_question_api_lifecycle(env):
    card = create_card(env.client)
    nid = card["id"]

    # ask
    r = env.client.post(f"/api/cards/{nid}/questions",
                        json={"question": "Which DB?", "options": ["sqlite", "postgres"]})
    assert r.status_code == 201, r.text
    q = r.json()
    assert q["status"] == "pending" and q["options"] == ["sqlite", "postgres"]

    # the card payload carries the questions (card rendering + MCP get_card)
    got = env.client.get(f"/api/cards/{nid}").json()
    assert got["questions"][0]["id"] == q["id"]
    listed = env.client.get("/api/cards").json()
    assert listed[0]["questions"][0]["question"] == "Which DB?"

    # list endpoint
    assert env.client.get(f"/api/cards/{nid}/questions").json()[0]["id"] == q["id"]

    # validation + unknown ids
    assert env.client.post(f"/api/cards/{nid}/questions", json={"question": ""}).status_code in (400, 422)
    assert env.client.post("/api/cards/nope99/questions", json={"question": "x"}).status_code == 404
    assert env.client.get(f"/api/cards/{nid}/questions/nope99/wait?timeout=0").status_code == 404

    # long-poll wait: a thread answers after 0.5s, the wait returns resolved
    outcome = {}
    def answer_later():
        time.sleep(0.5)
        env.client.post(f"/api/cards/{nid}/questions/{q['id']}/answer",
                        json={"answer": "postgres"})
    th = threading.Thread(target=answer_later, daemon=True)
    th.start()
    t0 = time.monotonic()
    r = env.client.get(f"/api/cards/{nid}/questions/{q['id']}/wait?timeout=10")
    rec = r.json()
    assert rec["status"] == "answered" and rec["answer"] == "postgres"
    assert 0.3 < time.monotonic() - t0 < 5
    th.join(timeout=5)

    # answering again -> 409
    r = env.client.post(f"/api/cards/{nid}/questions/{q['id']}/answer", json={"answer": "x"})
    assert r.status_code == 409
    # wait on a resolved question returns immediately
    assert env.client.get(f"/api/cards/{nid}/questions/{q['id']}/wait?timeout=30").json()["status"] == "answered"


def test_question_wait_timeout_and_cancel(env):
    card = create_card(env.client)
    nid = card["id"]
    q = env.client.post(f"/api/cards/{nid}/questions", json={"question": "Proceed?"}).json()

    # no answer -> still pending after the wait timeout
    rec = env.client.get(f"/api/cards/{nid}/questions/{q['id']}/wait?timeout=0.3").json()
    assert rec["status"] == "pending"

    # cancel from the card (dismiss)
    r = env.client.post(f"/api/cards/{nid}/questions/{q['id']}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # a waiter sees the cancellation immediately
    rec = env.client.get(f"/api/cards/{nid}/questions/{q['id']}/wait?timeout=30").json()
    assert rec["status"] == "cancelled"
    # cancelling again -> 409
    assert env.client.post(f"/api/cards/{nid}/questions/{q['id']}/cancel").status_code == 409


def test_runner_cancels_pending_question_on_stop(env):
    card = create_card(env.client)
    nid = card["id"]
    r = env.client.post(f"/api/cards/{nid}/run")
    assert r.status_code == 200
    # wait until the (slow) session is live
    deadline = time.time() + 10
    while time.time() < deadline:
        if env.client.get(f"/api/cards/{nid}").json()["status"] == "running":
            break
        time.sleep(0.05)

    # the model asks a question mid-run
    q = env.client.post(f"/api/cards/{nid}/questions",
                        json={"question": "Dark or light theme?"}).json()
    assert env.client.get(f"/api/cards/{nid}/questions").json()[0]["status"] == "pending"

    # the user stops the card -> the pending question is cancelled
    env.client.post(f"/api/cards/{nid}/stop")
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status = env.client.get(f"/api/cards/{nid}").json()["status"]
        if status in ("stopped", "failed"):
            break
        time.sleep(0.05)
    assert status == "stopped"
    rec = env.client.get(f"/api/cards/{nid}/questions").json()[0]
    assert rec["status"] == "cancelled"

    # deleting the card drops the question state (sessions dir is removed)
    env.client.delete(f"/api/cards/{nid}")
    assert not (env.sessions_dir / nid).exists()
