"""API tests via FastAPI TestClient. Hermetic: fake vibe binary, temp dirs."""
from __future__ import annotations

import contextlib
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import fsdialog, gitops
from app.config import Config
from app.main import create_app
from app.archives import ArchiveStore
from app.cards import CardStore
from app.projects import ProjectStore
from app.runner import SessionRunner
from app.settings import SettingsStore

FAKE_VIBE = """#!/bin/sh
# Fake vibe: write artifacts into --workdir / --output-dir, then exit 0.
# Review mode (--agent ask) prints a verdict instead of implementing.
# Resolver mode (round dir resolve-N): FAKE_RESOLVE=conclude (default) runs
# `git add -A && git commit` to conclude the in-progress merge, fail exits 1,
# hang sleeps until killed.
ARGS="$@"
workdir=""
outdir=""
agent=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2;;
    --output-dir) outdir="$2"; shift 2;;
    --agent) agent="$2"; shift 2;;
    *) shift;;
  esac
done
round_dir=$(basename "$(dirname "$outdir")")
case "$round_dir" in
  resolve-*)
    case "$FAKE_RESOLVE" in
      fail) echo "fake resolver failing"; exit 1 ;;
      hang)
        trap 'echo "fake resolver killed"; exit 0' TERM
        echo "fake resolver started"
        sleep 30
        exit 0
        ;;
      *)
        git add -A
        git commit -q -m "resolver: conclude merge"
        echo "fake resolver concluded merge"
        exit 0
        ;;
    esac
    ;;
esac
echo "fake vibe starting"
echo "fake vibe done"
mkdir -p "$workdir"
echo 'print("hello")' > "$workdir/hello.py"
mkdir -p "$outdir"
echo "$ARGS" > "$outdir/args.txt"
cat > "$outdir/export.json" <<'EOF'
{"usage": {"total_tokens": 42}, "config": {"active_model": "fake-model"}, "stop_reason": "end_turn", "outcome": "success"}
EOF
if [ "$agent" = "ask" ]; then
  echo "VERDICT: APPROVED"
fi
exit 0
"""

FAKE_VIBE_FAIL = """#!/bin/sh
echo "fake vibe failing"
exit 1
"""

# Same fake vibe, but export.json carries the full token-type usage breakdown
# (real vibe 3.x export.json: usage.input_tokens / output_tokens /
# cached_input_tokens next to total_tokens).
FAKE_VIBE_USAGE_DETAIL = FAKE_VIBE.replace(
    '"usage": {"total_tokens": 42}',
    '"usage": {"total_tokens": 42, "input_tokens": 30, "output_tokens": 2,'
    ' "cached_input_tokens": 10}')

FAKE_VIBE_ALWAYS_REJECT = """#!/bin/sh
# Fake vibe whose reviewer never approves (review/fix loop exhaustion).
workdir=""
outdir=""
agent=""
while [ $# -gt 0 ]; do
  case "$1" in
    --workdir) workdir="$2"; shift 2;;
    --output-dir) outdir="$2"; shift 2;;
    --agent) agent="$2"; shift 2;;
    *) shift;;
  esac
done
mkdir -p "$workdir" "$outdir"
if [ "$agent" = "ask" ]; then
  echo "VERDICT: REJECTED: fake reviewer always finds a bug"
else
  echo "fake vibe done"
fi
exit 0
"""

FAKE_VIBE_SLOW = """#!/bin/sh
# Fake vibe that hangs until killed (for stop/lock tests).
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


def make_env(tmp_path: Path, script: str = FAKE_VIBE, archives=None,
             settings=None, models=None, **config_kwargs) -> SimpleNamespace:
    cards_dir = tmp_path / "cards"
    sessions_dir = tmp_path / "sessions"
    store = CardStore(cards_dir)
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(script)
    fake.chmod(0o755)
    config_kwargs.setdefault("time_limit_seconds", 30)
    config = Config(vibe_bin=str(fake), **config_kwargs)
    projects = ProjectStore(tmp_path / "projects.json")
    # A per-test settings store: PUT /api/settings must never write the real
    # settings.json of the harness this suite runs in. A test that needs a
    # specific stored setting passes its own.
    if settings is None:
        settings = SettingsStore(tmp_path / "settings.json")
    runner = SessionRunner(store, config, sessions_dir=sessions_dir, projects=projects,
                           settings=settings, models=models)
    app = create_app(config=config, store=store, runner=runner, projects=projects,
                     archives=archives, settings=settings, models=models)
    client = TestClient(app)
    return SimpleNamespace(client=client, store=store, runner=runner,
                           projects=projects, sessions_dir=sessions_dir,
                           settings=settings, tmp_path=tmp_path)


@pytest.fixture()
def env(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        yield e
    e.client.close()


def create_card(client, title="Hello", body="do a thing", **extra):
    r = client.post("/api/cards", json={"title": title, "body": body, **extra})
    assert r.status_code == 201, r.text
    return r.json()


def wait_for_status(client, card_id, statuses, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"/api/cards/{card_id}")
        assert r.status_code == 200, r.text
        if r.json()["status"] in statuses:
            return r.json()
        time.sleep(0.05)
    raise AssertionError(f"card {card_id} did not reach {statuses} in {timeout}s")


# --------------------------------------------------------------------- cards

def test_create_get_list(env):
    card = create_card(env.client, title="My card", body="spec body")
    assert card["status"] == "draft"
    assert card["body"] == "spec body"
    assert card["session"] is None

    r = env.client.get(f"/api/cards/{card['id']}")
    assert r.status_code == 200
    assert r.json()["title"] == "My card"

    r = env.client.get("/api/cards")
    assert r.status_code == 200
    assert [n["id"] for n in r.json()] == [card["id"]]


def test_get_unknown_card_404(env):
    r = env.client.get("/api/cards/deadbe")
    assert r.status_code == 404
    assert "detail" in r.json()


def test_update_card(env):
    card = create_card(env.client)
    r = env.client.put(f"/api/cards/{card['id']}",
                       json={"title": "Renamed", "body": "new body"})
    assert r.status_code == 200
    assert r.json()["title"] == "Renamed"
    assert r.json()["body"] == "new body"


def test_update_card_with_stale_expected_updated_at_is_409(env):
    """Two clients open the same card; the first saves; the second gets a 409
    and its text is not written."""
    card = create_card(env.client, body="original")
    opened_at = card["updated_at"]

    # Client A saves first (it sends the version it loaded).
    r = env.client.put(f"/api/cards/{card['id']}",
                       json={"body": "A's text", "expected_updated_at": opened_at})
    assert r.status_code == 200, r.text

    # Client B still holds the version it opened: refused, loudly.
    r = env.client.put(f"/api/cards/{card['id']}",
                       json={"body": "B's text", "expected_updated_at": opened_at})
    assert r.status_code == 409, r.text
    assert "changed since you opened it" in r.json()["detail"]
    assert env.client.get(f"/api/cards/{card['id']}").json()["body"] == "A's text"


def test_update_card_overwrite_anyway_succeeds(env):
    """\"Overwrite anyway\" re-sends without expected_updated_at: it wins."""
    card = create_card(env.client, body="original")
    opened_at = card["updated_at"]
    env.client.put(f"/api/cards/{card['id']}",
                   json={"body": "A's text", "expected_updated_at": opened_at})
    r = env.client.put(f"/api/cards/{card['id']}",
                       json={"body": "B's text", "expected_updated_at": opened_at})
    assert r.status_code == 409, r.text

    r = env.client.put(f"/api/cards/{card['id']}", json={"body": "B's text"})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "B's text"
    assert env.client.get(f"/api/cards/{card['id']}").json()["body"] == "B's text"


def test_update_card_without_expected_updated_at_still_works(env):
    """MCP tools and scripts send no expected_updated_at: unchanged behavior."""
    card = create_card(env.client, body="original")
    env.client.put(f"/api/cards/{card['id']}", json={"body": "someone else"})
    r = env.client.put(f"/api/cards/{card['id']}", json={"body": "script edit"})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "script edit"


def test_update_card_with_matching_expected_updated_at_succeeds(env):
    """Nobody else touched the card: the editor's save goes through."""
    card = create_card(env.client, body="original")
    r = env.client.put(f"/api/cards/{card['id']}",
                       json={"body": "my edit",
                             "expected_updated_at": card["updated_at"]})
    assert r.status_code == 200, r.text
    assert r.json()["body"] == "my edit"


def test_delete_card_removes_session_dir(env):
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/run")
    card = wait_for_status(env.client, card["id"], {"done"})
    assert (env.sessions_dir / card["id"] / "workspace" / "hello.py").exists()

    r = env.client.delete(f"/api/cards/{card['id']}")
    assert r.status_code == 204
    assert not (env.sessions_dir / card["id"]).exists()
    assert env.client.get(f"/api/cards/{card['id']}").status_code == 404


# ------------------------------------------------------------------- sessions

def test_run_done_and_accept(env):
    card = create_card(env.client, body="write hello.py")
    r = env.client.post(f"/api/cards/{card['id']}/run")
    assert r.status_code == 200
    assert r.json()["status"] == "running"

    card = wait_for_status(env.client, card["id"], {"done"})
    session = card["session"]
    assert session["exit_code"] == 0
    # The export's tokens are persisted per round (worker on the session,
    # reviewer on its record), like the model and stop reason.
    assert session["tokens"] == 42
    assert session["model"] == "fake-model"
    assert session["stop_reason"] == "end_turn"
    assert session["started_at"] and session["finished_at"]
    # The run went through the reviewer and was approved before it became done.
    assert session["review"]["approved"] is True
    assert session["review_rounds"] == 1
    # artifacts on disk
    assert (env.sessions_dir / card["id"] / "workspace" / "hello.py").exists()
    prompt = (env.sessions_dir / card["id"] / "run-1" / "prompt.md").read_text()
    assert "write hello.py" in prompt
    assert "Mistral Vibe agent" in prompt

    r = env.client.post(f"/api/cards/{card['id']}/accept")
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert r.json()["accepted"] is True


def test_accept_blocked_without_review(env):
    card = create_card(env.client, title="Unreviewed")
    env.store.set_status(card["id"], "running")
    env.store.set_status(card["id"], "done")   # done, but never reviewed
    r = env.client.post(f"/api/cards/{card['id']}/accept")
    assert r.status_code == 409
    assert "review" in r.json()["detail"]


def _make_done(env, title):
    """A card sitting in Done with an approved review (no session run)."""
    card = create_card(env.client, title=title, body="x")
    env.store.set_status(card["id"], "running")
    env.store.set_status(card["id"], "done",
                         session_patch={"review": {"approved": True}})
    return card["id"]


def _wait_accepted(env, ids, timeout=10.0):
    deadline = time.time() + timeout
    statuses = []
    while time.time() < deadline:
        statuses = [env.client.get(f"/api/cards/{i}").json()["status"] for i in ids]
        if all(s == "accepted" for s in statuses):
            return statuses
        time.sleep(0.05)
    return statuses


def test_accept_all_merges_every_done_card(env):
    """Accept all snapshots the Done column and accepts each card in it."""
    ids = [_make_done(env, f"Done {i}") for i in range(3)]

    r = env.client.post("/api/cards/accept-all")
    assert r.status_code == 200
    assert sorted(r.json()["queued"]) == sorted(ids)

    assert _wait_accepted(env, ids) == ["accepted"] * 3


def test_accept_all_is_a_snapshot(env):
    """A card that reaches Done after the batch started is NOT in it — it is
    left for the next click (the UI renders it below the button)."""
    first = _make_done(env, "First")
    r = env.client.post("/api/cards/accept-all")
    assert r.json()["queued"] == [first]

    late = _make_done(env, "Late")
    # The running batch does not pick the late card up.
    assert env.client.get(f"/api/cards/{late}").json()["session"].get("accept_queued") is None
    assert _wait_accepted(env, [first]) == ["accepted"]

    # A second click takes the card that arrived meanwhile.
    r2 = env.client.post("/api/cards/accept-all")
    assert r2.json()["queued"] == [late]
    assert _wait_accepted(env, [late]) == ["accepted"]


def test_accept_all_skips_cards_already_queued(env):
    """Clicking twice while a batch runs does not queue the same card twice."""
    ids = [_make_done(env, f"Queued {i}") for i in range(2)]
    env.client.post("/api/cards/accept-all")
    second = env.client.post("/api/cards/accept-all")
    assert second.status_code == 200
    assert second.json()["queued"] == []
    assert _wait_accepted(env, ids) == ["accepted"] * 2


def test_accept_all_queues_each_card_once_under_two_simultaneous_clicks(env, monkeypatch):
    """Two clients clicking Accept all at the same moment queue each Done card
    exactly once (card 317592).

    The snapshot — read the Done column, skip what is already queued or being
    resolved, patch `accept_queued` — must be one critical section. Without it
    both clicks read the same Done column and both queue every card, so the
    second pass re-accepts cards the first one already shipped.
    """
    ids = [_make_done(env, f"Race {i}") for i in range(3)]

    # Widen the snapshot window: a click that read the Done column sleeps
    # before it patches, so two unlocked clicks are guaranteed to overlap.
    real_list = env.store.list

    def slow_list():
        cards = real_list()
        time.sleep(0.2)
        return cards

    monkeypatch.setattr(env.store, "list", slow_list)

    # Hold the drain on its first card: the batch is still running when the
    # second click returns, so `running` is deterministic.
    release = threading.Event()
    real_accept = env.runner.accept_card

    def blocked_accept(card_id, **kwargs):
        assert release.wait(10), "the drain was never released"
        return real_accept(card_id, **kwargs)

    monkeypatch.setattr(env.runner, "accept_card", blocked_accept)

    start = threading.Barrier(2)
    results: list[dict] = []

    def click():
        start.wait(10)
        results.append(env.client.post("/api/cards/accept-all").json())

    threads = [threading.Thread(target=click) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert not any(thread.is_alive() for thread in threads)

    # Exactly one click queued the batch; the other was a no-op that saw the
    # batch already running.
    assert sorted(sorted(r["queued"]) for r in results) == [[], sorted(ids)]
    noop = next(r for r in results if r["queued"] == [])
    assert noop["running"] is True

    release.set()
    assert _wait_accepted(env, ids) == ["accepted"] * 3


def test_accept_all_leaves_a_failing_card_in_done(env):
    """A card the green gate rejects stays in Done, out of the batch, with the
    reason recorded — the rest of the batch still goes through."""
    good = _make_done(env, "Good")
    bad = _make_done(env, "Bad")
    # The gate runs the test command in the workspace; make it fail for `bad`
    # only (both workspaces must exist, or the command fails for both).
    env.runner.config.test_cmd = "test ! -f FAIL"
    for cid in (good, bad):
        (env.sessions_dir / cid / "workspace").mkdir(parents=True, exist_ok=True)
    (env.sessions_dir / bad / "workspace" / "FAIL").write_text("x")

    r = env.client.post("/api/cards/accept-all")
    assert sorted(r.json()["queued"]) == sorted([good, bad])
    assert _wait_accepted(env, [good]) == ["accepted"]

    deadline = time.time() + 10
    while time.time() < deadline:
        card = env.client.get(f"/api/cards/{bad}").json()
        if card["session"].get("accept_error"):
            break
        time.sleep(0.05)
    assert card["status"] == "done"
    assert card["session"]["accept_queued"] is False
    assert "test" in card["session"]["accept_error"].lower()


def test_auto_accept_via_api(env):
    card = create_card(env.client, title="Hands-free", auto_accept=True)
    assert card["auto_accept"] is True
    env.client.post(f"/api/cards/{card['id']}/run")
    final = wait_for_status(env.client, card["id"], {"accepted"})
    assert final["status"] == "accepted"
    assert final["accepted"] is True
    assert final["session"]["review"]["approved"] is True


def test_auto_accept_field_update(env):
    card = create_card(env.client, title="Toggle")
    assert card["auto_accept"] is False
    r = env.client.put(f"/api/cards/{card['id']}", json={"auto_accept": True})
    assert r.status_code == 200
    assert r.json()["auto_accept"] is True
    assert r.json()["status"] == "draft"   # settings change does not reset


def test_accept_after_review_rounds_exhausted(tmp_path):
    """Loop cap hit -> done but unapproved (review.exhausted); the user
    accepts manually, but ONLY via the explicit force override.
    auto_accept does not fire without an approval."""
    e = make_env(tmp_path, script=FAKE_VIBE_ALWAYS_REJECT, max_review_rounds=1)
    with e.client:
        card = create_card(e.client, title="Stuck", auto_accept=True)
        e.client.post(f"/api/cards/{card['id']}/run")
        final = wait_for_status(e.client, card["id"], {"done"})
        assert final["session"]["review"]["exhausted"] is True
        assert final["accepted"] is False
        # A bare accept is refused: the exhausted review is not an approval.
        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 409
        assert "force" in r.json()["detail"]
        assert e.client.get(f"/api/cards/{card['id']}").json()["status"] == "done"
        # The explicit override accepts it.
        r = e.client.post(f"/api/cards/{card['id']}/accept", json={"force": True})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted"


def test_accept_force_is_refused_when_review_was_approved(env):
    """force is an override for the exhausted case only: on an approved card
    the endpoint refuses it (409), while a plain accept still works."""
    card_id = _make_done(env, "Approved")
    r = env.client.post(f"/api/cards/{card_id}/accept", json={"force": True})
    assert r.status_code == 409
    assert "force" in r.json()["detail"]
    assert env.client.get(f"/api/cards/{card_id}").json()["status"] == "done"
    r = env.client.post(f"/api/cards/{card_id}/accept")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"


def _make_exhausted_done(env, title):
    """A card sitting in Done with an exhausted, unapproved review."""
    card = create_card(env.client, title=title, body="x")
    env.store.set_status(card["id"], "running")
    env.store.set_status(card["id"], "done",
                         session_patch={"review": {"approved": False,
                                                   "exhausted": True,
                                                   "reason": "bugs found"}})
    return card["id"]


def test_accept_all_does_not_force_exhausted_cards(env):
    """accept-all queues every Done card, but an exhausted card is NOT
    force-accepted by the batch: it stays Done with the refusal recorded as
    accept_error, while the approved cards around it still merge."""
    good = _make_done(env, "Good")
    stuck = _make_exhausted_done(env, "Stuck")
    r = env.client.post("/api/cards/accept-all")
    assert r.status_code == 200
    assert sorted(r.json()["queued"]) == sorted([good, stuck])

    assert _wait_accepted(env, [good]) == ["accepted"]
    # The exhausted card is left in Done for an explicit per-card force.
    stuck_now = env.client.get(f"/api/cards/{stuck}").json()
    assert stuck_now["status"] == "done"
    assert stuck_now["session"].get("accept_queued") is False
    assert "force" in (stuck_now["session"].get("accept_error") or "")


def test_run_failure_marks_failed(tmp_path):
    e = make_env(tmp_path, script=FAKE_VIBE_FAIL)
    with e.client:
        card = create_card(e.client)
        e.client.post(f"/api/cards/{card['id']}/run")
        card = wait_for_status(e.client, card["id"], {"failed"})
        assert card["session"]["exit_code"] == 1


def test_accept_requires_done(env):
    card = create_card(env.client)  # draft
    r = env.client.post(f"/api/cards/{card['id']}/accept")
    assert r.status_code == 409


def test_double_run_and_edit_locks_then_stop(tmp_path):
    e = make_env(tmp_path, script=FAKE_VIBE_SLOW)
    with e.client:
        card = create_card(e.client)
        r = e.client.post(f"/api/cards/{card['id']}/run")
        assert r.json()["status"] == "running"

        # second run -> 409
        r = e.client.post(f"/api/cards/{card['id']}/run")
        assert r.status_code == 409
        # edit while running -> 409
        r = e.client.put(f"/api/cards/{card['id']}", json={"title": "nope"})
        assert r.status_code == 409
        # delete while running -> 409
        r = e.client.delete(f"/api/cards/{card['id']}")
        assert r.status_code == 409

        # stop -> 204, then status becomes "stopped"
        r = e.client.post(f"/api/cards/{card['id']}/stop")
        assert r.status_code == 204
        card = wait_for_status(e.client, card["id"], {"stopped"})
        assert card["session"]["exit_code"] is not None

        # stop again -> 404
        r = e.client.post(f"/api/cards/{card['id']}/stop")
        assert r.status_code == 404


def test_log_tail(env):
    card = create_card(env.client)
    # never run -> empty log
    r = env.client.get(f"/api/cards/{card['id']}/log")
    assert r.status_code == 200
    assert r.json()["lines"] == ""

    env.client.post(f"/api/cards/{card['id']}/run")
    wait_for_status(env.client, card["id"], {"done"})
    r = env.client.get(f"/api/cards/{card['id']}/log?tail=50")
    assert "fake vibe" in r.json()["lines"]


def test_log_unknown_card_404(env):
    assert env.client.get("/api/cards/deadbe/log").status_code == 404


def test_auto_run_on_create(env):
    card = create_card(env.client, auto_run=True)
    card = wait_for_status(env.client, card["id"], {"done"})
    assert card["status"] == "done"


def test_auto_run_on_update_queues_run(env):
    # Saving a draft with auto_run on queues the run immediately.
    card = create_card(env.client)
    assert card["status"] == "draft"
    r = env.client.put(f"/api/cards/{card['id']}", json={"auto_run": True})
    assert r.status_code == 200, r.text
    assert r.json()["auto_run"] is True
    assert r.json()["status"] == "running"
    final = wait_for_status(env.client, card["id"], {"done"})
    assert final["status"] == "done"


def test_run_button_skips_run_when_save_already_queued_it(env):
    # Reproducer for card 888d19: the editor's "▶ Run with Vibe" button saves
    # first and then POSTs /run. With auto_run on, the save already queued the
    # run (the returned card is "running"), so the button must NOT POST /run
    # again — that 409s with "card <id> is already running" (the error the
    # user saw) even though the run did start. The server keeps 409ing a
    # genuine double-run (test_double_run_and_edit_locks_then_stop), so the
    # guard belongs in the button handler, on the saved card's status.
    import re

    r = env.client.get("/app.js")
    assert r.status_code == 200
    js = r.text
    m = re.search(
        r"btnRun\.addEventListener\('click', \(\) => action\(async \(\) => \{(.*?)\}\)\);",
        js, re.S)
    assert m, "btnRun click handler not found in app.js"
    block = m.group(1)
    assert "'/run'" in block, "btnRun must still POST /run for a plain draft"
    assert re.search(r"saved\.status", block), (
        "btnRun must skip POST /run when the save (auto_run) already started "
        "the run — otherwise the user sees 409 'already running'")


# ---------------------------------------------------------------------- misc

def test_templates(env):
    r = env.client.get("/api/templates")
    assert r.status_code == 200
    names = {t["name"] for t in r.json()}
    assert {"web-app", "script", "refactor", "bug"} <= names
    assert all(t["body"] for t in r.json())


def test_stats(env):
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/run")
    wait_for_status(env.client, card["id"], {"done"})

    r = env.client.get("/api/stats")
    assert r.status_code == 200
    stats = r.json()
    assert stats["counts"]["done"] == 1
    assert stats["counts"]["draft"] == 0
    # The fake vibe reports 42 tokens for the run AND for the review round:
    # the card's total is the sum of both.
    assert stats["total_tokens"] == 84
    # The fake export.json has no per-type usage: the breakdown stays unknown.
    assert stats["token_breakdown"] == {"input": None, "output": None, "cached": None}
    assert stats["total_runtime_seconds"] >= 0
    assert stats["avg_runtime_seconds"] >= 0


def test_stats_counts_review_rounds(env):
    """A card's total is what it actually cost: the worker export PLUS its
    review rounds. `session.tokens` stays the worker's export total (existing
    data keeps working); the card total is derived from it."""
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/run")
    final = wait_for_status(env.client, card["id"], {"done"})
    # The fake vibe reports 42 tokens for the run AND for the review round.
    assert final["session"]["tokens"] == 42
    assert final["session"]["reviews"][0]["tokens"] == 42
    assert final["session"]["card_tokens"] == 84

    stats = env.client.get("/api/stats").json()
    assert stats["total_tokens"] == 84
    # The stats total is exactly the sum of the per-card totals.
    cards = env.client.get("/api/cards").json()
    assert stats["total_tokens"] == sum(n["session"]["card_tokens"] for n in cards)


def test_stats_sums_two_review_rounds(env):
    # A rejected round and the fixer's approved round both cost tokens: the
    # card total is worker + review-1 + review-2.
    card = create_card(env.client)
    env.store.set_status(card["id"], "running")
    env.store.set_status(card["id"], "done", session_patch={
        "tokens": 1000,
        "reviews": [{"round": 1, "tokens": 250, "approved": False},
                    {"round": 2, "tokens": 125, "approved": True}],
        "review": {"round": 2, "tokens": 125, "approved": True},
    })
    final = env.client.get(f"/api/cards/{card['id']}").json()
    assert final["session"]["card_tokens"] == 1375
    stats = env.client.get("/api/stats").json()
    assert stats["total_tokens"] == 1375


def test_stats_total_equals_the_sum_of_the_per_card_totals(env):
    # Two cards, each with a worker + review round: the board total is the sum
    # of the chips, not the sum of the worker exports.
    for title in ("One", "Two"):
        card = create_card(env.client, title=title)
        env.client.post(f"/api/cards/{card['id']}/run")
        wait_for_status(env.client, card["id"], {"done"})

    cards = env.client.get("/api/cards").json()
    stats = env.client.get("/api/stats").json()
    assert [n["session"]["card_tokens"] for n in cards] == [84, 84]
    assert stats["total_tokens"] == 168


def test_stats_includes_resolver_tokens(env):
    # A merge-conflict resolver's cost is part of the card's total.
    card = create_card(env.client)
    env.store.set_status(card["id"], "running")
    env.store.set_status(card["id"], "done", session_patch={
        "tokens": 1000,
        "reviews": [{"round": 1, "tokens": 250, "approved": True}],
        "review": {"round": 1, "tokens": 250, "approved": True},
        "resolver": {"round": 1, "exit_code": 0, "tokens": 400},
    })
    final = env.client.get(f"/api/cards/{card['id']}").json()
    assert final["session"]["card_tokens"] == 1650
    assert env.client.get("/api/stats").json()["total_tokens"] == 1650


def test_stats_counts_a_legacy_card_without_a_reviews_list(env):
    # Cards written before session.reviews existed keep only session.review.
    card = create_card(env.client)
    env.store.set_status(card["id"], "running")
    env.store.set_status(card["id"], "done", session_patch={
        "tokens": 1000,
        "review": {"round": 1, "tokens": 250, "approved": True},
    })
    final = env.client.get(f"/api/cards/{card['id']}").json()
    assert final["session"]["card_tokens"] == 1250
    assert env.client.get("/api/stats").json()["total_tokens"] == 1250




def test_token_breakdown_recorded_from_export(tmp_path):
    # export.json usage carries input/output/cached_input_tokens next to
    # total_tokens -> the card's session and /api/stats expose them as
    # token_breakdown {input, output, cached} for the hover tooltip.
    env = make_env(tmp_path, script=FAKE_VIBE_USAGE_DETAIL)
    with env.client:
        card = create_card(env.client)
        create_card(env.client, title="Other project", project="other")
        env.client.post(f"/api/cards/{card['id']}/run")
        final = wait_for_status(env.client, card["id"], {"done"})
        assert final["session"]["tokens"] == 42
        assert final["session"]["token_breakdown"] == {
            "input": 30, "output": 2, "cached": 10}

        stats = env.client.get("/api/stats").json()
        # Worker + review round: the total and its breakdown both add up.
        assert stats["total_tokens"] == 84
        assert stats["token_breakdown"] == {"input": 60, "output": 4, "cached": 20}
        # Project scoping applies to the breakdown too (the "other" card is a
        # draft without a session and contributes nothing).
        scoped = env.client.get("/api/stats?project=other").json()
        assert scoped["total_tokens"] == 0
        assert scoped["token_breakdown"] == {"input": None, "output": None, "cached": None}


def test_no_token_breakdown_key_when_export_lacks_it(env):
    # Defensive: an export.json with only total_tokens records no breakdown.
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/run")
    final = wait_for_status(env.client, card["id"], {"done"})
    assert final["session"]["tokens"] == 42
    assert "token_breakdown" not in (final["session"] or {})


def test_restart_server_409_when_not_serving(env, monkeypatch):
    # In-process TestClient: this process is not the port listener, so the
    # endpoint must refuse (and never spawn a real server from a test).
    import os
    monkeypatch.setattr(os, "getpid", lambda: -1)
    r = env.client.post("/api/server/restart")
    assert r.status_code == 409
    assert "not the server" in r.json()["detail"]


def test_index_served(env):
    r = env.client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_static_assets_served(env):
    for asset in ("app.js", "style.css", "theme.js"):
        r = env.client.get(f"/{asset}")
        assert r.status_code == 200, asset


def test_dark_mode_assets_wired(env):
    # theme.js loads synchronously in <head> (before first paint) and the top
    # bar has the indicator pill that explains the automatic theme.
    r = env.client.get("/")
    html = r.text
    assert '<script src="./theme.js"></script>' in html
    assert 'id="theme-indicator"' in html

    # Sweden (Gothenburg) is the standard; a known location is used instead.
    r = env.client.get("/theme.js")
    js = r.text
    assert "Gothenburg" in js
    assert "navigator.geolocation" in js


def test_deck_view_removed(env):
    # Card 7630ae: the D-key "deck" flip view does not represent what the deck
    # feature is (drag cards onto each other to stack them, card 9f3758), so
    # the whole D-key deck view is removed — markup, JS and styles. Stacking
    # stays (also covered by test_card_stacking_wired). D itself is now the
    # diff shortcut (card b4e872), which is not a deck view.
    r = env.client.get("/")
    html = r.text
    assert "deck" not in html.lower()

    r = env.client.get("/app.js")
    js = r.text
    # No deck state, functions or listeners remain.
    assert "deck" not in js.lower()
    # ...while card stacking (the real deck feature) is still wired.
    assert "function stackCards(dragId, targetId)" in js

    r = env.client.get("/style.css")
    css = r.text
    assert "deck" not in css.lower()


def test_card_stacking_wired(env):
    # The board's cards can be dragged onto each other to stack: app.js
    # implements the drag source/target wiring, the stack model (persisted per
    # browser), and the hover fan-out / mouse-leave collapse.
    r = env.client.get("/app.js")
    js = r.text
    for needle in (
        "'vibe-harness:stacks'",
        "function findStackOf(cardId)",
        "function cleanStacks()",
        "function stackCards(dragId, targetId)",
        "function unstackCard(cardId)",
        "function wireCardDrag(card, cardData)",
        "function wireCardDrop(card, cardData)",
        "function wireColumnDrop(wrap, colKey)",
        "function buildStack(stack, byId, queuePos)",
        "function layoutStack(stackEl)",
        "stack--open",
        "card--drop",
        "draggable = true",
    ):
        assert needle in js, needle

    # style.css carries the stack styles: collapsed pile, hover fan-out,
    # count badge and the drag/drop highlights.
    r = env.client.get("/style.css")
    css = r.text
    for needle in (
        ".stack {",
        ".stack .stack-card",
        ".stack--open",
        ".stack-count",
        ".card--dragging",
        ".card--drop",
        ".cards--drop",
    ):
        assert needle in css, needle


def test_new_card_project_selection_wired(env):
    # The editor's Project field is a real dropdown (no-project + registered
    # projects), replacing the old free-text input + datalist.
    r = env.client.get("/")
    html = r.text
    assert '<select id="card-project"' in html
    assert 'id="card-project" type="text"' not in html
    assert 'list="project-list"' not in html
    assert 'id="project-list"' not in html

    # A new card automatically uses the last used project: the chosen project
    # is remembered per browser and becomes the dropdown's default, falling
    # back to the top-bar project selection when nothing was used yet.
    r = env.client.get("/app.js")
    js = r.text
    assert "vibe-harness:last-project" in js
    assert "function defaultProjectForNewCard()" in js
    assert "cardProject.value = defaultProjectForNewCard();" in js
    assert "saveLastProjectDefault(project || '')" in js


def test_last_used_project_remembered_in_browser_and_on_cards(env):
    # "Working on a project in the browser or on the cards remembers which one
    # was used last": the top-bar switcher selection is persisted per browser
    # and restored on boot, and any card save (create or edit) plus any
    # concrete top-bar pick updates the last-used memory that defaults new
    # cards.
    r = env.client.get("/app.js")
    js = r.text

    # Top-bar selection persistence (localStorage, restored at boot).
    assert "vibe-harness:selected-project" in js
    assert "function loadSelectedProject()" in js
    assert "function saveSelectedProject(name)" in js
    assert "state.selectedProject = loadSelectedProject();" in js
    assert "saveSelectedProject(state.selectedProject);" in js

    # Picking a concrete project in the top bar also marks it last used;
    # "All projects" (empty value) does not.
    assert "if (state.selectedProject) saveLastProjectDefault(state.selectedProject);" in js

    # A card save remembers its project on BOTH paths — the remember call sits
    # before the edit (PUT) branch, not only in a create-only .then callback.
    # Scoped to saveCard(): other PUTs elsewhere in app.js (e.g. the sidebar's
    # per-role model picker) must not decide this. (saveCard takes an options
    # object since card 960bec — "overwrite anyway" saves without the
    # expected_updated_at guard — so the slice matches the opening paren.)
    save_fn = js[js.index("async function saveCard("):]
    save_fn = save_fn[:save_fn.index("\n}\n")]
    remember = save_fn.index("saveLastProjectDefault(project || '')")
    assert js.count("saveLastProjectDefault(project || '')") == 1
    assert remember < save_fn.index("method: 'PUT'")
    assert ".then((card) => {" not in save_fn[remember:save_fn.index("method: 'POST'")]


def test_card_title_fits_instead_of_truncating(env):
    # Card headers must not cut the card's name down to a single "Tex…" line:
    # the title wraps (two lines max, then ellipsis) and long words break.
    # The header's extras (spinner/tags) wrap to their own line when they
    # would squeeze the name below half the header row.
    import re

    r = env.client.get("/style.css")
    css = r.text
    m = re.search(r"\.card-title\s*\{([^}]*)\}", css)
    assert m, ".card-title rule missing from style.css"
    block = m.group(1)
    assert "nowrap" not in block
    assert "-webkit-line-clamp: 2" in block
    assert "overflow-wrap: break-word" in block
    # The name keeps at least half the header row, so the header's extras
    # (spinner/tags) cannot squeeze it into a sliver.
    assert re.search(r"min-width:\s*50%", block)
    # The header wraps: when extras would block the name, they drop to
    # their own line instead of shrinking the title.
    m = re.search(r"\.card-head\s*\{([^}]*)\}", css)
    assert m, ".card-head rule missing from style.css"
    assert "flex-wrap: wrap" in m.group(1)


def test_long_card_title_expands_dynamically(env):
    # Long card names must still be displayable in full: the title font
    # auto-fits the two-line clamp, and a more/less toggle lifts the clamp.
    import re

    r = env.client.get("/style.css")
    css = r.text
    # The default two-line clamp stays (previous fix)…
    m = re.search(r"\.card-title\s*\{([^}]*)\}", css)
    assert m and "-webkit-line-clamp: 2" in m.group(1)
    # …but the expanded state lifts the clamp so the full name shows.
    m = re.search(r"\.card-title--expanded\s*\{([^}]*)\}", css)
    assert m, ".card-title--expanded rule missing from style.css"
    assert "line-clamp" in m.group(1)
    # The more/less toggle is styled and hidden unless the title is clamped.
    assert re.search(r"\.card-title-toggle\s*\{[^}]*\}", css)
    assert ".card-title-toggle[hidden]" in css

    r = env.client.get("/app.js")
    js = r.text
    # Dynamic fit: measure the clamped title and shrink the font until the
    # full name fits; the toggle's expanded state survives board re-renders.
    assert "function fitCardTitle(" in js
    assert "TITLE_FONT_FLOOR_PX" in js
    assert "scrollHeight" in js and "clientHeight" in js
    assert "expandedTitles" in js
    assert "card-title--expanded" in js
    assert "card-title-toggle" in js
    assert "stopPropagation" in js  # the toggle must not open the editor
    assert "ResizeObserver" in js   # re-fit when the layout changes


def test_run_from_done_resets_and_reruns(env):
    card = create_card(env.client)
    env.client.post(f"/api/cards/{card['id']}/run")
    first = wait_for_status(env.client, card["id"], {"done"})
    first_pid = first["session"]["pid"]

    # Re-run without editing: resets to draft, runs again, finishes fresh.
    r = env.client.post(f"/api/cards/{card['id']}/run")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    second = wait_for_status(env.client, card["id"], {"done"})
    assert second["session"]["pid"] != first_pid


def test_project_workdir_fields_and_run(env, tmp_path):
    target = tmp_path / "proj"
    card = create_card(env.client, project="my-app", workdir=str(target))
    assert card["project"] == "my-app"
    assert card["workdir"] == str(target)

    r = env.client.put(f"/api/cards/{card['id']}", json={"project": "other-app"})
    assert r.status_code == 200
    assert r.json()["project"] == "other-app"

    env.client.post(f"/api/cards/{card['id']}/run")
    final = wait_for_status(env.client, card["id"], {"done"})
    # The fake vibe worked in the card's linked worktree (not the custom folder
    # itself), and that worktree is recorded as the session workdir.
    wt = env.sessions_dir / card["id"] / "worktree"
    assert (wt / "hello.py").exists()
    assert not (target / "hello.py").exists()
    assert final["session"]["workdir"] == str(wt.resolve())


def test_workdir_not_allowed_403(tmp_path):
    e = make_env(tmp_path, allowed_workdirs=[str(tmp_path / "allowed")])
    with e.client:
        card = create_card(e.client, workdir=str(tmp_path / "nope"))
        r = e.client.post(f"/api/cards/{card['id']}/run")
        assert r.status_code == 403
        assert "not in" in r.json()["detail"]


# ------------------------------------------------------------------- projects

def test_projects_crud(env):
    assert env.client.get("/api/projects").json() == []

    r = env.client.post("/api/projects", json={"name": "my-app", "path": str(env.tmp_path / "my-app")})
    assert r.status_code == 201
    assert r.json()["name"] == "my-app"
    assert (env.tmp_path / "my-app").is_dir()

    r = env.client.post("/api/projects", json={"name": "my-app", "path": "/tmp/x"})
    assert r.status_code == 409

    # Empty values are rejected by request validation; whitespace-only by the store.
    r = env.client.post("/api/projects", json={"name": "", "path": "/tmp/x"})
    assert r.status_code == 422
    r = env.client.post("/api/projects", json={"name": "   ", "path": "/tmp/x"})
    assert r.status_code == 400

    projects = env.client.get("/api/projects").json()
    assert [p["name"] for p in projects] == ["my-app"]

    r = env.client.delete("/api/projects/my-app")
    assert r.status_code == 204
    assert env.client.get("/api/projects").json() == []
    assert env.client.delete("/api/projects/my-app").status_code == 404


def test_stats_scoped_to_project(env):
    create_card(env.client, title="A1", project="alpha")
    create_card(env.client, title="A2", project="alpha")
    create_card(env.client, title="B1", project="beta")

    stats = env.client.get("/api/stats").json()
    assert stats["counts"]["draft"] == 3
    assert stats["project"] is None

    stats = env.client.get("/api/stats?project=alpha").json()
    assert stats["counts"]["draft"] == 2
    assert stats["project"] == "alpha"

    stats = env.client.get("/api/stats?project=beta").json()
    assert stats["counts"]["draft"] == 1


def test_run_uses_project_path(env):
    env.client.post("/api/projects", json={"name": "my-app", "path": str(env.tmp_path / "my-app")})
    card = create_card(env.client, project="my-app",
                       body="write hello.py")
    env.client.post(f"/api/cards/{card['id']}/run")
    final = wait_for_status(env.client, card["id"], {"done"})
    # Vibe worked in the card's linked worktree, not the project folder itself;
    # the project folder (main worktree) stays on its base branch.
    wt = env.sessions_dir / card["id"] / "worktree"
    assert (wt / "hello.py").exists()
    assert not (env.tmp_path / "my-app" / "hello.py").exists()
    assert final["session"]["workdir"] == str(wt.resolve())
    assert final["session"]["git"]["worktree"] == str(wt.resolve())
    assert gitops.current_branch(env.tmp_path / "my-app") \
        == final["session"]["git"]["base_branch"]


# ------------------------------------------------------------- folder picker

def test_choose_folder_returns_path(env, monkeypatch):
    monkeypatch.setattr(fsdialog, "choose_folder", lambda: "/tmp/picked-folder")
    r = env.client.post("/api/fs/choose-folder")
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "/tmp/picked-folder"}


def test_choose_folder_cancelled(env, monkeypatch):
    def cancelled():
        raise fsdialog.FolderPickerCancelled("cancelled")
    monkeypatch.setattr(fsdialog, "choose_folder", cancelled)
    r = env.client.post("/api/fs/choose-folder")
    assert r.status_code == 409, r.text


def test_choose_folder_unsupported(env, monkeypatch):
    def unavailable():
        raise fsdialog.FolderPickerUnavailable("folder picker needs macOS with osascript")
    monkeypatch.setattr(fsdialog, "choose_folder", unavailable)
    r = env.client.post("/api/fs/choose-folder")
    assert r.status_code == 501, r.text
    assert "macOS" in r.json()["detail"]


# --------------------------------------------------------------------- git

git_test = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")


def _git(args, cwd):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@git_test
def test_accept_merges_git_branch(tmp_path):
    """A project run is git-backed: the card runs on its branch in a linked
    worktree (the project folder stays on the base branch); accept merges the
    branch into the base IN THE MAIN WORKTREE with --no-ff, and KEEPS the
    branch and the worktree so the card's diff stays inspectable."""
    e = make_env(tmp_path)
    with e.client:
        proj = tmp_path / "proj"
        e.client.post("/api/projects", json={"name": "p", "path": str(proj)})
        card = create_card(e.client, project="p", body="write hello.py")
        e.client.post(f"/api/cards/{card['id']}/run")
        final = wait_for_status(e.client, card["id"], {"done"})
        git_info = final["session"]["git"]
        assert git_info["branch"].startswith("harness/")
        wt = Path(git_info["worktree"])
        # The work is committed on the card branch in the linked worktree,
        # not on the base branch yet; the main worktree is untouched.
        assert (wt / "hello.py").exists()
        assert not (proj / "hello.py").exists()
        assert _git(["symbolic-ref", "--short", "HEAD"], proj) == git_info["base_branch"]
        assert "hello.py" not in _git(
            ["show", "--name-only", "--format=", git_info["base_branch"]], proj)

        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted"
        assert r.json()["session"]["git"]["merged"] is True
        assert _git(["symbolic-ref", "--short", "HEAD"], proj) == git_info["base_branch"]
        assert "hello.py" in _git(["ls-tree", "--name-only", git_info["base_branch"]], proj)
        # One merge commit per accepted card, recorded on the session.
        g = r.json()["session"]["git"]
        assert g["merge_commit"] == _git(["rev-parse", "HEAD"], proj)
        assert g["merge_was_ff"] is False
        assert g["merged_at"]
        assert len(_git(["rev-list", "--parents", "-n", "1", "HEAD"], proj).split()) == 3
        # The branch and the linked worktree are kept after accept.
        assert gitops.branch_exists(proj, git_info["branch"])
        assert wt.exists()
        assert wt.resolve() in gitops.registered_worktrees(proj)


@git_test
def test_run_auto_stashes_dirty_repo(tmp_path):
    """A dirty tree left by an interrupted previous run does not block the run:
    the leftover changes in the MAIN worktree are stashed automatically (kept
    in `git stash list`) and the session git record flags stashed/auto_stashed.
    The card branch is checked out in the linked worktree; the main worktree
    stays on the base branch."""
    e = make_env(tmp_path)
    with e.client:
        proj = tmp_path / "proj"
        proj.mkdir()
        gitops.ensure_repo(proj)
        (proj / "wip.txt").write_text("x\n")
        e.client.post("/api/projects", json={"name": "p", "path": str(proj)})
        card = create_card(e.client, project="p")
        r = e.client.post(f"/api/cards/{card['id']}/run")
        assert r.status_code == 200, r.text
        final = wait_for_status(e.client, card["id"], {"done"})
        git_info = final["session"]["git"]
        assert git_info["stashed"] is True
        assert git_info["auto_stashed"] is True
        assert not (proj / "wip.txt").exists()
        assert "vibe-harness" in _git(["stash", "list"], proj)
        assert _git(["symbolic-ref", "--short", "HEAD"], proj) == git_info["base_branch"]
        wt = Path(git_info["worktree"])
        assert _git(["symbolic-ref", "--short", "HEAD"], wt) == git_info["branch"]


@git_test
def test_accept_spawns_resolver_on_merge_conflict(tmp_path):
    """Accept on a conflicted merge returns 200 with the card still 'done'
    (flagged resolving — the card spins); the resolver concludes the merge and
    the card becomes accepted + merged automatically."""
    e = make_env(tmp_path)
    with e.client:
        proj = tmp_path / "proj"
        e.client.post("/api/projects", json={"name": "p", "path": str(proj)})
        gitops.ensure_repo(proj)
        (proj / "hello.py").write_text("print('base')\n")
        _git(["add", "-A"], proj)
        _git(["commit", "-q", "-m", "base"], proj)
        card = create_card(e.client, project="p", body="write hello.py")
        e.client.post(f"/api/cards/{card['id']}/run")
        final = wait_for_status(e.client, card["id"], {"done"})
        git_info = final["session"]["git"]
        # The user edits the same file on the base branch after the run.
        _git(["checkout", "-q", git_info["base_branch"]], proj)
        (proj / "hello.py").write_text("print('user')\n")
        _git(["add", "hello.py"], proj)
        _git(["commit", "-q", "-m", "user edit"], proj)

        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "done"     # stays in Done, starts spinning
        g = r.json()["session"]["git"]
        assert g["resolving"] is True
        assert g["merge_conflict"] is True
        # The conflicted merge is in the MAIN worktree; branch + linked
        # worktree are kept, and the resolver runs in the project folder.
        assert gitops.merge_in_progress(proj)
        assert Path(g["worktree"]).is_dir()
        assert e.client.get(f"/api/cards/{card['id']}").json()["session"]["resolver"]["workdir"] \
            == str(proj.resolve())

        final = wait_for_status(e.client, card["id"], {"accepted"}, timeout=15)
        g = final["session"]["git"]
        assert g["merged"] is True
        assert g["resolving"] is False
        assert _git(["symbolic-ref", "--short", "HEAD"], proj) == g["base_branch"]
        assert "hello.py" in _git(["ls-tree", "--name-only", g["base_branch"]], proj)
        # The resolver's commit is the card's merge commit; branch + worktree
        # are kept so the diff survives the accept.
        assert g["merge_commit"] == _git(["rev-parse", "HEAD"], proj)
        assert g["merged_at"]
        assert gitops.branch_exists(proj, g["branch"])
        assert Path(g["worktree"]).is_dir()
        assert Path(g["worktree"]).resolve() in gitops.registered_worktrees(proj)


@git_test
def test_accept_409_while_resolver_running(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RESOLVE", "hang")
    e = make_env(tmp_path)
    with e.client:
        proj = tmp_path / "proj"
        e.client.post("/api/projects", json={"name": "p", "path": str(proj)})
        gitops.ensure_repo(proj)
        (proj / "hello.py").write_text("print('base')\n")
        _git(["add", "-A"], proj)
        _git(["commit", "-q", "-m", "base"], proj)
        card = create_card(e.client, project="p", body="write hello.py")
        e.client.post(f"/api/cards/{card['id']}/run")
        final = wait_for_status(e.client, card["id"], {"done"})
        git_info = final["session"]["git"]
        _git(["checkout", "-q", git_info["base_branch"]], proj)
        (proj / "hello.py").write_text("print('user')\n")
        _git(["add", "hello.py"], proj)
        _git(["commit", "-q", "-m", "user edit"], proj)

        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 200
        assert r.json()["session"]["git"]["resolving"] is True
        # A second accept while the resolver is live -> 409.
        r2 = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r2.status_code == 409
        assert "resolver" in r2.json()["detail"]
        # While the conflict is open, the linked worktree is kept.
        assert Path(git_info["worktree"]).is_dir()
        # Clean up: stop the hanging resolver.
        assert e.client.post(f"/api/cards/{card['id']}/stop").status_code == 204


@git_test
def test_run_draft_while_merge_conflict_queues_instead_of_409(tmp_path, monkeypatch):
    """POST /run on a draft while a merge conflict is being applied in its
    repo (resolver live, MERGE_HEAD set) returns 200 with the card still
    draft but flagged queued — not the old 409 GitError."""
    monkeypatch.setenv("FAKE_RESOLVE", "hang")
    e = make_env(tmp_path)
    with e.client:
        proj = tmp_path / "proj"
        e.client.post("/api/projects", json={"name": "p", "path": str(proj)})
        gitops.ensure_repo(proj)
        (proj / "hello.py").write_text("print('base')\n")
        _git(["add", "-A"], proj)
        _git(["commit", "-q", "-m", "base"], proj)
        card = create_card(e.client, project="p", body="write hello.py")
        e.client.post(f"/api/cards/{card['id']}/run")
        final = wait_for_status(e.client, card["id"], {"done"})
        git_info = final["session"]["git"]
        _git(["checkout", "-q", git_info["base_branch"]], proj)
        (proj / "hello.py").write_text("print('user')\n")
        _git(["add", "hello.py"], proj)
        _git(["commit", "-q", "-m", "user edit"], proj)

        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 200
        assert r.json()["session"]["git"]["resolving"] is True
        assert gitops.merge_in_progress(proj)

        # A second draft in the same repo: the run QUEUES (200, still draft).
        draft = create_card(e.client, title="Second", project="p")
        r2 = e.client.post(f"/api/cards/{draft['id']}/run")
        assert r2.status_code == 200, r2.text
        body = r2.json()
        assert body["status"] == "draft"
        assert body["session"]["queued"] is True
        assert gitops.merge_in_progress(proj)  # conflict still open, run stays queued

        # Clean up: stop the hanging resolver.
        assert e.client.post(f"/api/cards/{card['id']}/stop").status_code == 204


def test_test_gate_rejection_and_accept_gate(tmp_path):
    """End-to-end over HTTP: a failing test command rejects the run into the
    fixer loop (no review session runs), exhaustion lands the card in 'done',
    and the accept-time green gate returns 409 until the tests pass."""
    e = make_env(tmp_path, test_cmd="echo TESTS-FAILED; test ! -f FAIL",
                 max_review_rounds=1)
    with e.client:
        card = create_card(e.client, body="do a thing")
        # Tests fail from the very first round (deterministic: FAIL exists
        # before the run starts).
        ws = e.sessions_dir / card["id"] / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "FAIL").write_text("x")

        r = e.client.post(f"/api/cards/{card['id']}/run")
        assert r.status_code == 200, r.text
        final = wait_for_status(e.client, card["id"], {"done"})
        s = final["session"]
        assert s["review"]["exhausted"] is True
        assert s["review"]["source"] == "test"
        assert "TESTS-FAILED" in s["review"]["reason"]
        assert not (e.sessions_dir / card["id"] / "review-1").exists()
        assert (e.sessions_dir / card["id"] / "run-1").exists()

        # Accept gate: the review was exhausted, so a bare accept is refused
        # on the review gate (409, mentions force) before tests even run.
        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 409
        assert "force" in r.json()["detail"]
        assert e.client.get(f"/api/cards/{card['id']}").json()["status"] == "done"

        # With the explicit force override, the test gate applies: tests still
        # failing -> 409, card stays done.
        r = e.client.post(f"/api/cards/{card['id']}/accept", json={"force": True})
        assert r.status_code == 409
        assert "tests failed" in r.json()["detail"]
        assert e.client.get(f"/api/cards/{card['id']}").json()["status"] == "done"

        # Tests green -> forced accept succeeds and records verified.
        (ws / "FAIL").unlink()
        r = e.client.post(f"/api/cards/{card['id']}/accept", json={"force": True})
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "accepted"
        assert r.json()["session"]["verified"] is True


def test_create_project_sets_up_git_agent_md_and_route_md(env):
    """Registering a project always sets up its folder: git init + initial
    commit, managed agent.md, and an EMPTY agent-owned route.md."""
    import shutil
    from app.routemd import AGENT_MD_MARKER
    r = env.client.post("/api/projects",
                        json={"name": "routed", "path": str(env.tmp_path / "routed")})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["route_md"] == "created"
    assert body["agent_md"] == "created"
    folder = env.tmp_path / "routed"
    assert (folder / "route.md").read_text() == ""      # empty, agent-owned
    agent = (folder / "agent.md").read_text()
    assert AGENT_MD_MARKER in agent
    assert "route.md" in agent                          # rules mention route.md
    if shutil.which("git"):
        assert (folder / ".git").is_dir()               # git init'ed
        import subprocess
        names = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"],
                               cwd=folder, capture_output=True, text=True).stdout
        assert "agent.md" in names and "route.md" in names


def test_startup_syncs_project_files_into_existing_projects(tmp_path):
    """Projects registered before this feature get the files on server start:
    an empty route.md and a managed agent.md."""
    from app.routemd import AGENT_MD_MARKER
    e = make_env(tmp_path)
    e.projects.create("legacy", str(tmp_path / "legacy"))   # store-level: no setup
    assert not (tmp_path / "legacy" / "route.md").exists()
    with e.client:                                          # lifespan runs the sync
        assert (tmp_path / "legacy" / "route.md").read_text() == ""
        assert AGENT_MD_MARKER in (tmp_path / "legacy" / "agent.md").read_text()
    e.client.close()


# ------------------------------------------------------------------ dependencies

def test_dependency_endpoints_crud(env):
    a = create_card(env.client, title="A")
    b = create_card(env.client, title="B")

    # link a -> b
    r = env.client.post(f"/api/cards/{a['id']}/depends/{b['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["depends_on"] == [b["id"]]
    # idempotent
    r = env.client.post(f"/api/cards/{a['id']}/depends/{b['id']}")
    assert r.json()["depends_on"] == [b["id"]]

    # dependency view
    r = env.client.get(f"/api/cards/{a['id']}/dependencies")
    assert r.status_code == 200
    body = r.json()
    assert body == {"card_id": a["id"], "depends_on": [b["id"]],
                    "blocks": [], "unmet": [b["id"]]}
    r = env.client.get(f"/api/cards/{b['id']}/dependencies")
    assert r.json()["blocks"] == [a["id"]]

    # self-link and unknown cards
    assert env.client.post(f"/api/cards/{a['id']}/depends/{a['id']}").status_code == 409
    assert env.client.post(f"/api/cards/{a['id']}/depends/nope99").status_code == 404
    assert env.client.post(f"/api/cards/nope99/depends/{b['id']}").status_code == 404
    assert env.client.get("/api/cards/nope99/dependencies").status_code == 404

    # cycle: b -> a would close a -> b
    assert env.client.post(f"/api/cards/{b['id']}/depends/{a['id']}").status_code == 409

    # unlink
    r = env.client.delete(f"/api/cards/{a['id']}/depends/{b['id']}")
    assert r.status_code == 200
    assert r.json()["depends_on"] == []


def test_run_blocked_until_dependency_is_merged(env):
    """A draft linked to an unfinished card waits; it starts only once the
    dependency is MERGED (accepted) — a reviewer-approved "done" is not enough."""
    dep = create_card(env.client, title="First", body="write first.py")
    follow = create_card(env.client, title="Second", body="write second.py")
    env.client.post(f"/api/cards/{follow['id']}/depends/{dep['id']}")

    # Running the dependent now does not start it — it waits.
    r = env.client.post(f"/api/cards/{follow['id']}/run")
    assert r.status_code == 200
    card = r.json()
    assert card["status"] == "draft"
    assert card["session"]["waiting_for"] == [dep["id"]]

    # Run the dependency. Reviewer approval alone must NOT release the
    # dependent: it has to be merged first.
    env.client.post(f"/api/cards/{dep['id']}/run")
    wait_for_status(env.client, dep["id"], {"done"})
    time.sleep(0.3)                       # give a (wrong) drain a chance to fire
    still = env.client.get(f"/api/cards/{follow['id']}").json()
    assert still["status"] == "draft"
    assert still["session"]["waiting_for"] == [dep["id"]]

    # Accepting (merging) the dependency releases it.
    assert env.client.post(f"/api/cards/{dep['id']}/accept").status_code == 200
    follow_final = wait_for_status(env.client, follow["id"], {"done", "failed"}, timeout=15)
    assert follow_final["status"] == "done"
    assert follow_final["session"]["waiting_for"] is None
    assert follow_final["session"]["review"]["approved"] is True


def test_link_onto_already_merged_card_runs_immediately(env):
    dep = create_card(env.client, title="Merged dep", body="write dep.py")
    env.client.post(f"/api/cards/{dep['id']}/run")
    wait_for_status(env.client, dep["id"], {"done"})
    assert env.client.post(f"/api/cards/{dep['id']}/accept").status_code == 200

    follow = create_card(env.client, title="Follow-up", body="write follow.py")
    # Linking a draft onto an already-merged card starts it right away.
    r = env.client.post(f"/api/cards/{follow['id']}/depends/{dep['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    final = wait_for_status(env.client, follow["id"], {"done", "failed"}, timeout=15)
    assert final["status"] == "done"


def test_remove_dependency_can_unblock_run(env):
    dep = create_card(env.client, title="Dep", body="x")
    follow = create_card(env.client, title="Follow", body="y")
    env.client.post(f"/api/cards/{follow['id']}/depends/{dep['id']}")
    r = env.client.post(f"/api/cards/{follow['id']}/run")
    assert r.json()["status"] == "draft"          # waiting
    assert r.json()["session"]["waiting_for"] == [dep["id"]]
    # Removing the link makes the draft runnable: run_if_ready starts it now.
    r = env.client.delete(f"/api/cards/{follow['id']}/depends/{dep['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "running"
    wait_for_status(env.client, follow["id"], {"done", "failed"}, timeout=15)


def test_failed_dependency_does_not_trigger_dependent(env):
    """Only a merged (accepted) dependency satisfies a link; a failed one
    leaves the dependent waiting (it can be re-run manually after the dep is
    fixed)."""
    dep = create_card(env.client, title="Flaky", body="fail")
    follow = create_card(env.client, title="Follow", body="y")
    env.client.post(f"/api/cards/{follow['id']}/depends/{dep['id']}")
    env.client.post(f"/api/cards/{follow['id']}/run")
    assert env.client.get(f"/api/cards/{follow['id']}").json()["status"] == "draft"

    # Make the dependency fail: point the runner at a failing fake vibe.
    env.runner.config.vibe_bin = str(env.tmp_path / "fail.sh")
    (env.tmp_path / "fail.sh").write_text("#!/bin/sh\nexit 1\n")
    (env.tmp_path / "fail.sh").chmod(0o755)
    env.client.post(f"/api/cards/{dep['id']}/run")
    wait_for_status(env.client, dep["id"], {"failed"})
    # The dependent is still a waiting draft.
    card = env.client.get(f"/api/cards/{follow['id']}").json()
    assert card["status"] == "draft"
    assert card["session"]["waiting_for"] == [dep["id"]]


def test_delete_dependency_card_cleans_references(env):
    dep = create_card(env.client, title="Dep", body="x")
    follow = create_card(env.client, title="Follow", body="y")
    env.client.post(f"/api/cards/{follow['id']}/depends/{dep['id']}")
    r = env.client.delete(f"/api/cards/{dep['id']}")
    assert r.status_code == 204
    card = env.client.get(f"/api/cards/{follow['id']}").json()
    assert card["depends_on"] == []
    # A missing dependency counts as satisfied: the view shows no unmet.
    assert env.client.get(f"/api/cards/{follow['id']}/dependencies").json()["unmet"] == []


def test_server_startup_survives_broken_resolving_card(tmp_path):
    """Lifespan startup (recover_stale) must not crash on a card whose repo was
    deleted while the server was down — the server still comes up."""
    e = make_env(tmp_path)
    card = e.store.create("Broken resolving", "spec")
    card_id = card["id"]
    gone = tmp_path / "deleted-repo"
    git_info = {"repo": str(gone), "branch": f"harness/{card_id}",
                "base_branch": "main", "worktree": str(tmp_path / "wt"),
                "merged": False, "resolving": True, "merge_conflict": True}
    e.store.set_status(card_id, "running", session_patch={
        "pid": None, "workdir": str(gone), "git": git_info,
        "review": {"approved": True, "reason": None, "round": 1, "source": "review",
                   "tokens": 1, "log": None, "export_dir": None}})
    e.store.set_status(card_id, "done", session_patch={
        "git": git_info,
        "resolver": {"pid": 999999, "started_at": "2026-01-01T00:00:00+00:00",
                     "finished_at": None, "exit_code": None, "round": 1,
                     "workdir": str(gone), "log": "", "export_dir": ""}})

    with e.client:                             # lifespan runs recover_stale()
        resp = e.client.get("/api/cards")
        assert resp.status_code == 200
        ids = [n["id"] for n in resp.json()]
        assert card_id in ids
    e.client.close()


def test_duplication_endpoint_reports_clones_with_file_and_line(env):
    """GET /api/duplication is the mechanical duplication report: the same
    detector the reviewer sees, served as `{count, clones}` with a file and a
    1-based line number per occurrence."""
    r = env.client.get("/api/duplication")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"count", "clones"}
    assert body["count"] == len(body["clones"])
    for clone in body["clones"]:
        assert clone["length"] >= 6
        assert len(clone["occurrences"]) >= 2
        for occ in clone["occurrences"]:
            # Paths are relative to the scanned `app/` folder.
            assert occ["file"].startswith("app/"), occ
            assert isinstance(occ["start"], int) and occ["start"] >= 1


def test_duplication_endpoint_serves_the_detector_report(env, monkeypatch):
    """The endpoint is a thin wrapper: whatever the detector finds is what the
    board shows (no second, divergent notion of a duplicate)."""
    from app import dupes

    clone = {"length": 7,
             "occurrences": [{"file": "app/one.py", "start": 3},
                             {"file": "app/two.py", "start": 9}]}
    monkeypatch.setattr(dupes, "find_clones", lambda root, min_lines=6: [clone])

    body = env.client.get("/api/duplication").json()
    assert body == {"count": 1, "clones": [clone]}


def test_version_endpoint_fingerprints_front_end_assets(env):
    # The dashboard is a long-lived tab: without a build fingerprint the browser
    # keeps running whatever app.js it loaded days ago, so every front-end fix
    # looks like it did nothing. The client polls this and reloads on change.
    r = env.client.get("/api/version")
    assert r.status_code == 200
    version = r.json()["version"]
    assert isinstance(version, str) and len(version) == 12
    assert env.client.get("/api/version").json()["version"] == version  # stable


def test_static_assets_are_revalidated_not_heuristically_cached(env):
    # StaticFiles sends an ETag but no Cache-Control, so a reload could be
    # answered from the browser's heuristic cache and keep serving old JS.
    r = env.client.get("/app.js")
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-cache"
    # API responses keep their own caching behaviour.
    assert env.client.get("/api/cards").headers.get("cache-control") != "no-cache"


# ----------------------------------------------------------------------
# importing the module must not build the app
# ----------------------------------------------------------------------

def test_importing_app_main_does_not_build_the_app():
    """`app = create_app()` at import time built a real CardStore on the repo's
    cards/ dir, and its legacy notes/ -> cards/ migration then moved tracked
    files in every card worktree (a full-suite run showed seven deleted
    notes/*.md). Importing must be side-effect free; `uvicorn app.main:app`
    still resolves the app on first access."""
    repo = Path(__file__).resolve().parent.parent
    code = "import app.main as m\nprint('eager', 'app' in vars(m))\n"
    out = subprocess.run([sys.executable, "-c", code], cwd=repo,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "eager False" in out.stdout, out.stdout


def test_app_attribute_is_built_lazily_and_cached(monkeypatch):
    """`app.main.app` is built on first access (uvicorn's entry point) and
    cached, so a second access does not build a second app."""
    import app.main as main_module

    monkeypatch.setattr(main_module, "create_app", lambda: "APP")
    monkeypatch.delitem(vars(main_module), "app", raising=False)
    try:
        assert main_module.app == "APP"
        assert vars(main_module)["app"] == "APP"
    finally:
        vars(main_module).pop("app", None)


# ----------------------------------------------------------------------
# card diff (what the card actually changed)
# ----------------------------------------------------------------------

@contextlib.contextmanager
def _git_card(tmp_path, body="write hello.py", archives=None):
    """A project-backed card that ran to done, inside a live client context
    (the lifespan's startup pruning runs before the card, not after it).
    Yields (env, card, git_info)."""
    e = make_env(tmp_path, archives=archives)
    with e.client:
        proj = tmp_path / "proj"
        e.client.post("/api/projects", json={"name": "p", "path": str(proj)})
        card = create_card(e.client, project="p", body=body)
        e.client.post(f"/api/cards/{card['id']}/run")
        final = wait_for_status(e.client, card["id"], {"done"})
        yield e, final, final["session"]["git"]


@git_test
def test_diff_endpoint_card_range_before_and_after_accept(tmp_path):
    """`range=card` is what accept merged: base_branch..harness/<id> before
    accept, merge_commit^..merge_commit after — and it survives the accept
    because the branch and the worktree are kept."""
    with _git_card(tmp_path) as (e, card, git_info):
        r = e.client.get(f"/api/cards/{card['id']}/diff")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["range"] == "card"
        assert "hello.py" in body["diff"]
        assert body["truncated"] is False
        assert body["files"] == [{"path": "hello.py", "additions": 1, "deletions": 0}]
        assert body["base"] and body["head"]
        assert body["review"]["approved"] is True

        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 200, r.text
        g = r.json()["session"]["git"]
        assert g["merge_commit"] and g["merged_at"]
        assert g["merge_was_ff"] is False
        # The branch and the linked worktree survive the accept.
        proj = Path(git_info["repo"])
        assert gitops.branch_exists(proj, git_info["branch"])
        assert Path(git_info["worktree"]).exists()

        r = e.client.get(f"/api/cards/{card['id']}/diff")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "hello.py" in body["diff"]
        assert body["head"] == g["merge_commit"]
        assert body["base"] == _git(["rev-parse", g["merge_commit"] + "^"], proj)
        assert body["files"] == [{"path": "hello.py", "additions": 1, "deletions": 0}]


@git_test
def test_diff_endpoint_round_and_worktree_ranges(tmp_path):
    """`round` is what the reviewer judged, `worktree` the uncommitted changes."""
    with _git_card(tmp_path) as (e, card, git_info):
        r = e.client.get(f"/api/cards/{card['id']}/diff?range=round")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["range"] == "round"
        assert "hello.py" in body["diff"]
        assert body["review"]["approved"] is True

        # Uncommitted work in the card's worktree shows up in `worktree`
        # (`git diff HEAD`: tracked changes, like the reviewer's own view).
        wt = Path(git_info["worktree"])
        (wt / "hello.py").write_text("print('hi')\nprint('uncommitted')\n")
        r = e.client.get(f"/api/cards/{card['id']}/diff?range=worktree")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["range"] == "worktree"
        assert "uncommitted" in body["diff"]
        assert body["files"] == [{"path": "hello.py", "additions": 2, "deletions": 1}]

        # An unknown range is a client error, not a 500.
        r = e.client.get(f"/api/cards/{card['id']}/diff?range=nope")
        assert r.status_code == 400


@git_test
def test_diff_endpoint_card_range_ignores_base_moves(tmp_path):
    """A card branch cut before the base moved must show what the CARD changed,
    not the base's newer commits as deletions.

    This is the reviewer's false "revert": `git diff base..branch` compares two
    tips, so anything that landed on the base after the branch was cut shows up
    as the card deleting it. The card range is measured from the merge-base —
    which is exactly what accept merges (`merge_commit^..merge_commit`), so the
    before-accept and after-accept views agree.
    """
    with _git_card(tmp_path) as (e, card, git_info):
        proj = Path(git_info["repo"])
        # The base moves forward while the card waits for review.
        (proj / "someone_else.py").write_text("print('other work')\n")
        _git(["add", "-A"], proj)
        _git(["commit", "-q", "-m", "unrelated work on the base"], proj)

        r = e.client.get(f"/api/cards/{card['id']}/diff")
        assert r.status_code == 200, r.text
        body = r.json()
        assert "hello.py" in body["diff"]
        # The base's newer commit is NOT shown as the card deleting it.
        assert "someone_else.py" not in body["diff"]
        assert [f["path"] for f in body["files"]] == ["hello.py"]
        # base is the merge-base, not the moved base tip.
        assert body["base"] == _git(["merge-base", git_info["base_branch"],
                                     git_info["branch"]], proj)


@git_test
def test_diff_endpoint_round_range_ignores_base_moves(tmp_path):
    """Round 1's range starts at the merge-base too.

    The Round panel shows what the reviewer judged; measuring round 1 from the
    base TIP would show the base's newer commits as deletions — the same false
    revert the card range avoids.
    """
    with _git_card(tmp_path) as (e, card, git_info):
        proj = Path(git_info["repo"])
        # The base moves forward after the card branch was cut.
        (proj / "someone_else.py").write_text("print('other work')\n")
        _git(["add", "-A"], proj)
        _git(["commit", "-q", "-m", "unrelated work on the base"], proj)

        r = e.client.get(f"/api/cards/{card['id']}/diff?range=round")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["range"] == "round"
        assert "hello.py" in body["diff"]
        # The base's newer commit is NOT shown as the card deleting it.
        assert "someone_else.py" not in body["diff"]
        assert [f["path"] for f in body["files"]] == ["hello.py"]
        assert body["base"] == _git(["merge-base", git_info["base_branch"],
                                     git_info["branch"]], proj)


@git_test
def test_diff_endpoint_truncates_huge_diffs(tmp_path):
    """The diff is capped (400 KB) and flagged, so a giant card cannot hang
    the panel."""
    with _git_card(tmp_path) as (e, card, git_info):
        wt = Path(git_info["worktree"])
        (wt / "hello.py").write_text("x" * 500_000 + "\n")
        r = e.client.get(f"/api/cards/{card['id']}/diff?range=worktree")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["truncated"] is True
        assert len(body["diff"]) <= 400 * 1024 + 200


@git_test
def test_archiving_an_accepted_card_removes_its_worktree(tmp_path):
    """An archived card leaves the board, so its linked worktree goes away
    (the card branch is kept) — that is what bounds the disk cost of keeping
    every accepted card's worktree around for the diff view."""
    archives = ArchiveStore(tmp_path / "archives", CardStore(tmp_path / "cards"))
    with _git_card(tmp_path, archives=archives) as (e, card, git_info):
        e.client.post(f"/api/cards/{card['id']}/accept")
        wt = Path(git_info["worktree"])
        assert wt.exists()
        a = e.client.post("/api/archives",
                          json={"name": "done", "type": "cards"}).json()
        r = e.client.post(f"/api/archives/{a['id']}/cards/{card['id']}")
        assert r.status_code == 200, r.text
        assert not wt.exists()
        assert gitops.branch_exists(Path(git_info["repo"]), git_info["branch"])


def test_diff_endpoint_sandbox_card_is_not_git_backed(env):
    """A sandbox (non-git) card answers clearly instead of 500ing."""
    card = create_card(env.client, body="write hello.py")
    env.client.post(f"/api/cards/{card['id']}/run")
    wait_for_status(env.client, card["id"], {"done"})
    r = env.client.get(f"/api/cards/{card['id']}/diff")
    assert r.status_code == 200, r.text
    assert r.json()["diff"] is None
    assert r.json()["reason"] == "not git-backed"


def test_diff_endpoint_unknown_card_404(env):
    assert env.client.get("/api/cards/deadbe/diff").status_code == 404


# ----------------------------------------------------------------------
# front-end wiring: diff popup + keys panel
# ----------------------------------------------------------------------

def test_diff_panel_wired(env):
    """D + click opens the diff popup; the panel renders the diff with a line
    classifier and inserts every line as a TEXT NODE (the content is
    model-written — never innerHTML)."""
    js = env.client.get("/app.js").text
    for needle in (
        "function openDiff(",
        "function renderDiff(",
        "function classifyDiffLine(",
        "/diff?range=",
        "createTextNode",
        "KEYBINDINGS",
    ):
        assert needle in js, needle
    # The diff body is never assigned through innerHTML.
    assert "diffBody.innerHTML" not in js
    assert "innerHTML" not in js.split("function renderDiff(")[1].split("function ")[0]
    html = env.client.get("/").text
    assert 'data-panel="keys"' in html
    assert 'id="diff-popup"' in html
    assert 'id="btn-diff"' in html


def test_keys_panel_lists_every_keybinding(env):
    """The Keys panel is generated from one list in app.js, so it cannot drift
    from the handlers."""
    js = env.client.get("/app.js").text
    keys_block = js.split("const KEYBINDINGS = [")[1].split("];")[0]
    assert "'N'" in keys_block and "'Esc'" in keys_block
    assert "'D + click'" in keys_block and "'C + click'" in keys_block
    assert "renderKeysPanel" in js


# --------------------------------------------------------------------- revert
# Undo an accepted card: revert its merge commit and reopen it as a draft.

@git_test
def test_revert_accepted_card_undoes_the_merge(tmp_path):
    """Accept a card, revert it: the working tree no longer contains the
    change, the card is draft again, and the revert is recorded on the
    session (which keeps the cost record of the accepted run)."""
    with _git_card(tmp_path) as (e, card, git_info):
        proj = Path(git_info["repo"])
        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 200, r.text
        merge_commit = r.json()["session"]["git"]["merge_commit"]
        assert (proj / "hello.py").exists()
        tokens = r.json()["session"]["tokens"]

        r = e.client.post(f"/api/cards/{card['id']}/revert")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "draft"
        assert body["accepted"] is False
        session = body["session"]
        assert session["revert_commit"] == _git(["rev-parse", "HEAD"], proj)
        assert session["revert_commit"] != merge_commit
        assert session["reverted_at"]
        # The change is gone from the working tree and from the base branch.
        assert not (proj / "hello.py").exists()
        assert "hello.py" not in _git(["ls-tree", "--name-only",
                                       git_info["base_branch"]], proj)
        # The cost record of the accepted run survives the reopen.
        assert session["tokens"] == tokens
        assert session["review"]["approved"] is True
        assert session["git"]["merge_commit"] == merge_commit
        # The repo is clean and no revert is left in progress.
        assert not gitops.is_dirty(proj)
        assert not gitops.revert_in_progress(proj)


@git_test
def test_revert_without_merge_commit_returns_409(tmp_path):
    """A card accepted before merge commits were recorded has nothing to
    revert: a clear 409, and the card stays accepted."""
    e = make_env(tmp_path)
    with e.client:
        card = create_card(e.client, title="Old", body="x")
        e.store.set_status(card["id"], "running")
        e.store.set_status(card["id"], "done",
                           session_patch={"review": {"approved": True}})
        e.store.accept(card["id"])
        r = e.client.post(f"/api/cards/{card['id']}/revert")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "merge commit" in detail
        assert e.store.get(card["id"])["status"] == "accepted"


@git_test
def test_revert_conflict_aborts_and_returns_409(tmp_path):
    """A conflicting revert aborts cleanly: 409 naming the conflicting paths,
    the repo untouched (clean, HEAD unchanged, no revert in progress) and the
    card still accepted."""
    with _git_card(tmp_path) as (e, card, git_info):
        proj = Path(git_info["repo"])
        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 200, r.text
        # The user edits the same file on the base branch after the accept.
        (proj / "hello.py").write_text("print('user edit')\n")
        _git(["commit", "-q", "-am", "user edit"], proj)
        before = _git(["rev-parse", "HEAD"], proj)

        r = e.client.post(f"/api/cards/{card['id']}/revert")
        assert r.status_code == 409, r.text
        detail = r.json()["detail"]
        assert "hello.py" in detail
        assert "aborted" in detail
        # Untouched: same HEAD, clean tree, no revert in progress.
        assert _git(["rev-parse", "HEAD"], proj) == before
        assert not gitops.is_dirty(proj)
        assert not gitops.revert_in_progress(proj)
        assert (proj / "hello.py").read_text() == "print('user edit')\n"
        assert e.store.get(card["id"])["status"] == "accepted"


def test_revert_requires_an_accepted_card(env):
    """Only an accepted card can be reverted (409 for a draft)."""
    card = create_card(env.client, title="Draft", body="x")
    r = env.client.post(f"/api/cards/{card['id']}/revert")
    assert r.status_code == 409, r.text
    assert "accepted" in r.json()["detail"]
    assert env.store.get(card["id"])["status"] == "draft"


def test_revert_unknown_card_404(env):
    assert env.client.post("/api/cards/deadbe/revert").status_code == 404


@git_test
def test_revert_archived_card_restores_it_to_the_board(tmp_path):
    """An archived accepted card comes back to the board first (the existing
    restore path), then the revert reopens it as a draft."""
    archives = ArchiveStore(tmp_path / "archives", CardStore(tmp_path / "cards"))
    with _git_card(tmp_path, archives=archives) as (e, card, git_info):
        proj = Path(git_info["repo"])
        assert e.client.post(f"/api/cards/{card['id']}/accept").status_code == 200
        a = e.client.post("/api/archives",
                          json={"name": "done", "type": "cards"}).json()
        r = e.client.post(f"/api/archives/{a['id']}/cards/{card['id']}")
        assert r.status_code == 200, r.text
        assert e.client.get(f"/api/cards/{card['id']}").status_code == 404

        r = e.client.post(f"/api/cards/{card['id']}/revert")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "draft"
        assert body["archived"] is False
        assert body["session"]["revert_commit"]
        assert not (proj / "hello.py").exists()
        # It is back on the board.
        assert e.client.get(f"/api/cards/{card['id']}").status_code == 200


def test_revert_action_wired(env):
    """An accepted card offers a Revert action behind a confirm dialog that
    names the merge commit being reverted; a reverted card shows a note."""
    html = env.client.get("/").text
    assert 'id="btn-revert"' in html
    assert 'id="revert-note"' in html

    js = env.client.get("/app.js").text
    # Offered on accepted cards only (the API refuses anything else).
    assert "btnRevert.hidden = card.status !== 'accepted'" in js
    # The confirm dialog names the merge commit being reverted.
    assert "merge commit" in js
    assert "window.confirm(" in js
    # The action posts to the revert endpoint.
    assert "'/revert'" in js
    # A reverted card carries a note (chip + editor note) with the revert.
    assert "s.reverted_at" in js
    assert "chip('reverted'" in js
    assert "revertNote" in js

# ----------------------------------------------------------------------
# accept_mode: local (merge) or pr (push + open a pull request)
# ----------------------------------------------------------------------

def test_settings_expose_the_accept_mode(env):
    data = env.client.get("/api/settings").json()
    assert data["accept_mode"] == "local"
    assert [m["key"] for m in data["accept_modes"]] == ["local", "pr"]


def test_settings_switch_the_accept_mode(env):
    r = env.client.put("/api/settings", json={"accept_mode": "pr"})
    assert r.status_code == 200, r.text
    assert r.json()["accept_mode"] == "pr"
    assert env.client.get("/api/settings").json()["accept_mode"] == "pr"
    # ...and back, without touching the backend.
    r = env.client.put("/api/settings", json={"accept_mode": "local"})
    assert r.json()["accept_mode"] == "local"
    assert r.json()["backend"] == "vibe"


def test_settings_reject_an_unknown_accept_mode(env):
    r = env.client.put("/api/settings", json={"accept_mode": "merge"})
    assert r.status_code == 400
    assert env.client.get("/api/settings").json()["accept_mode"] == "local"


def test_settings_empty_patch_keeps_the_accept_mode(env):
    env.client.put("/api/settings", json={"accept_mode": "pr"})
    r = env.client.put("/api/settings", json={})
    assert r.status_code == 200
    assert r.json()["accept_mode"] == "pr"


# ----------------------------------------------------------------------
# test_cmd visibility: the accept-time green gate is OFF unless a test
# command is configured, and that must be loud in GET /api/settings (the
# board renders a banner from it) — while accepts stay possible.
# ----------------------------------------------------------------------

def test_settings_expose_whether_a_test_cmd_is_configured(env, tmp_path):
    """GET /api/settings carries test_cmd_configured: False by default (no
    test_cmd -> gate off) and True when one is configured."""
    data = env.client.get("/api/settings").json()
    assert data["test_cmd_configured"] is False

    e2 = make_env(tmp_path / "gated", test_cmd="exit 0")
    with e2.client:
        data2 = e2.client.get("/api/settings").json()
        assert data2["test_cmd_configured"] is True


def test_accept_still_possible_when_no_test_cmd_is_configured(env):
    """No test_cmd -> the green gate is off, but accepting an approved card
    must NOT be blocked (the board would deadlock otherwise)."""
    card_id = _make_done(env, "No gate")
    r = env.client.post(f"/api/cards/{card_id}/accept")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "accepted"
    # verified stays unset: no test command ever ran.
    assert r.json()["session"].get("verified") is None


def test_done_cards_warn_loudly_when_the_test_gate_is_off(env, tmp_path):
    """A card waiting in Done carries a visible warning when no test command
    is configured, and when its review was exhausted it warns that accept
    needs the force override. Cards not awaiting accept carry no warning."""
    stuck = _make_exhausted_done(env, "Stuck")
    card = env.client.get(f"/api/cards/{stuck}").json()
    warnings = card.get("warnings") or []
    assert any("test command" in w for w in warnings)
    assert any("force" in w for w in warnings)

    draft = create_card(env.client, title="Draft")
    assert "warnings" not in env.client.get(f"/api/cards/{draft['id']}").json()

    # With a test command configured, the gate warning disappears (the force
    # warning for the exhausted review stays).
    e2 = make_env(tmp_path / "gated", test_cmd="exit 0")
    with e2.client:
        stuck2 = _make_exhausted_done(e2, "Stuck2")
        warnings2 = e2.client.get(f"/api/cards/{stuck2}").json().get("warnings") or []
        assert not any("test command" in w for w in warnings2)
        assert any("force" in w for w in warnings2)


# ----------------------------------------------------------------------
# prevent_sleep: the sleep-guard toggle is persisted like the other settings,
# so a server restart does not revert it to the VIBE_HARNESS_PREVENT_SLEEP
# env default.
# ----------------------------------------------------------------------

def test_settings_persist_and_round_trip_prevent_sleep(env):
    r = env.client.put("/api/settings", json={"prevent_sleep": False})
    assert r.status_code == 200, r.text
    assert r.json()["prevent_sleep"] is False
    assert env.runner.sleep_guard.enabled is False
    # Persisted: a fresh store over the same settings.json reads it back.
    assert SettingsStore(env.tmp_path / "settings.json").prevent_sleep(True) is False
    # The GET round-trips for the UI.
    assert env.client.get("/api/settings").json()["prevent_sleep"] is False
    # ...and back on, without touching the other settings.
    r = env.client.put("/api/settings", json={"prevent_sleep": True})
    assert r.json()["prevent_sleep"] is True
    assert env.runner.sleep_guard.enabled is True
    assert SettingsStore(env.tmp_path / "settings.json").prevent_sleep(False) is True
    assert r.json()["backend"] == "vibe"


def test_settings_restore_prevent_sleep_at_startup(tmp_path):
    """A stored toggle wins over the env default when the server starts."""
    settings = SettingsStore(tmp_path / "settings.json")
    settings.set_prevent_sleep(False)
    e = make_env(tmp_path, settings=settings, prevent_sleep=True)
    with e.client:
        assert e.runner.sleep_guard.enabled is False
        assert e.client.get("/api/settings").json()["prevent_sleep"] is False


def test_settings_prevent_sleep_keeps_the_env_default_at_startup(tmp_path):
    """No stored value: the VIBE_HARNESS_PREVENT_SLEEP default still applies."""
    e = make_env(tmp_path / "a", prevent_sleep=False)
    with e.client:
        assert e.runner.sleep_guard.enabled is False
        assert e.client.get("/api/settings").json()["prevent_sleep"] is False
    e = make_env(tmp_path / "b", prevent_sleep=True)
    with e.client:
        assert e.runner.sleep_guard.enabled is True
        assert e.client.get("/api/settings").json()["prevent_sleep"] is True


@git_test
def test_accept_in_pr_mode_opens_a_pull_request(tmp_path, monkeypatch):
    """pr mode over HTTP: accept pushes the branch and opens a PR (stubbed),
    records the URL on the card, and never merges locally."""
    from app import praccept

    opened = []

    def fake_open(git_info, title, body):
        opened.append({"branch": git_info["branch"], "title": title, "body": body})
        return "https://github.com/me/app/pull/3"

    monkeypatch.setattr(praccept, "preflight", lambda git_info: None)
    monkeypatch.setattr(praccept, "push_branch", lambda git_info: None)
    monkeypatch.setattr(praccept, "open_pr", fake_open)
    monkeypatch.setattr(praccept, "diff_stat", lambda git_info: " hello.py | 1 +")

    e = make_env(tmp_path, accept_mode="pr")
    with e.client:
        proj = tmp_path / "proj"
        e.client.post("/api/projects", json={"name": "p", "path": str(proj)})
        card = create_card(e.client, project="p", body="write hello.py")
        e.client.post(f"/api/cards/{card['id']}/run")
        done = wait_for_status(e.client, card["id"], {"done"})
        git_info = done["session"]["git"]

        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "accepted"
        assert body["session"]["pr_url"] == "https://github.com/me/app/pull/3"
        assert body["session"]["pr_number"] == 3
        assert body["session"]["git"]["merged"] is False
        # The branch and its worktree are kept for follow-up commits.
        assert gitops.branch_exists(proj, git_info["branch"])
        assert Path(git_info["worktree"]).is_dir()
        assert opened[0]["branch"] == git_info["branch"]
        assert "write hello.py" in opened[0]["body"]


@git_test
def test_accept_in_pr_mode_without_gh_is_a_409(tmp_path, monkeypatch):
    """gh missing: the API answers 409 naming gh and the card stays done."""
    from app import praccept

    monkeypatch.setattr(praccept, "gh_available", lambda: False)

    e = make_env(tmp_path, accept_mode="pr")
    with e.client:
        proj = tmp_path / "proj"
        e.client.post("/api/projects", json={"name": "p", "path": str(proj)})
        card = create_card(e.client, project="p", body="write hello.py")
        e.client.post(f"/api/cards/{card['id']}/run")
        wait_for_status(e.client, card["id"], {"done"})

        r = e.client.post(f"/api/cards/{card['id']}/accept")
        assert r.status_code == 409, r.text
        assert "gh" in r.json()["detail"]
        assert e.client.get(f"/api/cards/{card['id']}").json()["status"] == "done"


def test_accept_button_reads_open_pr_in_pr_mode(tmp_path):
    """The UI follows the setting: the accept button says "Open PR" in pr mode
    and an accepted card with a PR renders a link to it."""
    e = make_env(tmp_path, accept_mode="pr")
    with e.client:
        js = e.client.get("/app.js").text
        assert "Open PR" in js
        assert "pr_url" in js
        assert "accept_mode" in js
