"""Why a card failed: the classifier (app/failures.py) and its exposure.

Two layers:

- pure unit tests over synthetic session dicts — one per kind, the priority
  order between them, and the cards that did not fail at all;
- integration tests through the API, where a real (fake-binary) run produces
  the session record and `annotate_card` attaches `session.failure`.
"""
from __future__ import annotations

import pytest

from app.failures import KINDS, classify

from test_api import (FAKE_VIBE_ALWAYS_REJECT, FAKE_VIBE_SLOW, create_card,
                      make_env, wait_for_status)

FAKE_VIBE_NO_VERDICT = """#!/bin/sh
# Fake vibe whose reviewer never answers: no VERDICT line, no submit_verdict.
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
echo "fake vibe done"
exit 0
"""


def card(status: str = "failed", session: dict | None = None, **extra) -> dict:
    """A card whose session looks like a finished, approved run by default."""
    s = {
        "exit_code": 0,
        "stop_reason": "end_turn",
        "verified": True,
        "review": {"approved": True, "reason": None, "round": 1, "via": "log"},
        "review_rounds": 1,
    }
    s.update(session or {})
    return {"id": "abc123", "title": "a card", "status": status,
            "session": s, **extra}


# --------------------------------------------------------------- no failure

def test_successful_card_has_no_failure():
    assert classify(card(status="done")) is None


def test_accepted_card_has_no_failure():
    assert classify(card(status="accepted")) is None


def test_approved_card_with_a_noisy_exit_code_has_no_failure():
    # The review process can exit non-zero and still print its verdict; an
    # approved card shipped, so a noisy exit code is not a failure.
    assert classify(card(status="done", session={"exit_code": 1})) is None


@pytest.mark.parametrize("status", ["draft", "running", "reviewing"])
def test_unfinished_cards_have_no_failure(status):
    # A live card may carry the previous round's rejection; it is not a failure
    # yet — the fixer is still working on it.
    assert classify(card(status=status, session={
        "exit_code": None, "verified": False,
        "review": {"approved": False, "reason": "nope", "source": "test"},
    })) is None


def test_card_without_a_session_has_no_failure():
    assert classify({"id": "abc123", "status": "draft", "session": None}) is None


# -------------------------------------------------------------------- kinds

def test_stopped_by_user():
    out = classify(card(status="stopped", session={"exit_code": -15}))
    assert out["kind"] == "stopped_by_user"
    assert "stopped" in out["detail"]


def test_git_conflict_while_the_resolver_runs():
    out = classify(card(status="done", session={
        "git": {"branch": "harness/abc123", "merge_conflict": True,
                "resolving": True},
        "resolver": {"pid": 42, "exit_code": None},
    }))
    assert out["kind"] == "git_conflict"
    assert "harness/abc123" in out["detail"]


def test_git_conflict_after_the_resolver_failed():
    out = classify(card(status="done", session={
        "git": {"branch": "harness/abc123", "merge_conflict": True,
                "resolving": False, "resolver_error": "resolver session exited with code 1"},
        "resolver": {"pid": 42, "exit_code": 1},
    }))
    assert out["kind"] == "git_conflict"
    assert "resolver" in out["detail"]


def test_timeout_from_stop_reason():
    out = classify(card(session={"exit_code": 0, "stop_reason": "limit"}))
    assert out["kind"] == "timeout"
    assert "limit" in out["detail"]


def test_timeout_from_the_harness_watchdog():
    # The Claude backend has no native time limit: the harness kills the
    # session and records `timed_out` (stop_reason stays empty).
    out = classify(card(status="stopped", session={
        "exit_code": -15, "stop_reason": None, "timed_out": True}))
    assert out["kind"] == "timeout"


def test_tests_failed_from_the_verified_flag():
    out = classify(card(session={"verified": False, "review_rounds": 3}))
    assert out["kind"] == "tests_failed"
    assert out["detail"] == "tests failed after 3 rounds"


def test_tests_failed_from_a_test_gate_rejection():
    out = classify(card(status="done", session={
        "verified": False, "review_rounds": 1,
        "review": {"approved": False, "reason": "the test command failed",
                   "source": "test", "round": 1, "exhausted": True},
    }))
    assert out["kind"] == "tests_failed"
    assert out["detail"] == "tests failed after 1 round"


def test_tests_failed_at_accept():
    # The post-merge green gate failed: the card stays done with git.test_error.
    out = classify(card(status="done", session={
        "git": {"branch": "harness/abc123", "merged": True,
                "test_error": "tests failed at accept"},
    }))
    assert out["kind"] == "tests_failed"


def test_reviewer_rejected():
    out = classify(card(status="done", session={
        "review_rounds": 5,
        "review": {"approved": False, "reason": "the diff is wrong",
                   "round": 5, "via": "submit_verdict", "exhausted": True},
    }))
    assert out["kind"] == "reviewer_rejected"
    assert out["detail"] == "reviewer rejected it after 5 rounds"


def test_no_verdict():
    out = classify(card(status="done", session={
        "review_rounds": 2,
        "review": {"approved": False, "reason": "reviewer did not produce a verdict",
                   "round": 2, "via": None, "exhausted": True},
    }))
    assert out["kind"] == "no_verdict"
    assert "verdict" in out["detail"]


def test_no_verdict_from_the_explicit_flag():
    out = classify(card(status="done", session={
        "review": {"approved": False, "reason": "", "no_verdict": True,
                   "exhausted": True},
    }))
    assert out["kind"] == "no_verdict"


def test_crashed():
    out = classify(card(session={"exit_code": 1, "verified": None,
                                 "review": None}))
    assert out["kind"] == "crashed"
    assert "1" in out["detail"]


def test_crashed_with_a_git_error():
    # The session exited 0 but the result could not be committed: still a
    # failure, and the detail says what went wrong.
    out = classify(card(session={"exit_code": 0, "verified": None,
                                 "review": None,
                                 "git_error": "nothing to commit"}))
    assert out["kind"] == "crashed"
    assert "nothing to commit" in out["detail"]


# ----------------------------------------------------------------- priority

def test_stopped_by_user_beats_timeout():
    # The user pressed Stop on a session that also reports a limit stop reason.
    out = classify(card(status="stopped", session={"stop_reason": "limit"}))
    assert out["kind"] == "stopped_by_user"


def test_git_conflict_beats_tests_failed():
    out = classify(card(status="done", session={
        "verified": False,
        "git": {"merge_conflict": True, "resolving": True},
    }))
    assert out["kind"] == "git_conflict"


def test_tests_failed_beats_crashed():
    # A test-gate rejection followed by a fixer that crashed: the recorded
    # rejection is the more specific explanation.
    out = classify(card(session={"exit_code": 1, "verified": False,
                                 "review_rounds": 2}))
    assert out["kind"] == "tests_failed"


def test_reviewer_rejected_beats_crashed():
    out = classify(card(status="done", session={
        "exit_code": 1,
        "review": {"approved": False, "reason": "wrong", "via": "log",
                   "exhausted": True},
    }))
    assert out["kind"] == "reviewer_rejected"


# ------------------------------------------------------------------- shape

def test_every_kind_is_reachable():
    """The kinds tuple is the contract the board filter offers."""
    seen = set()
    for c in (
        card(status="stopped"),
        card(status="done", session={"git": {"merge_conflict": True}}),
        card(session={"stop_reason": "limit"}),
        card(session={"verified": False}),
        card(status="done", session={"review": {"approved": False,
                                                "via": "log",
                                                "exhausted": True}}),
        card(status="done", session={"review": {"approved": False,
                                                "no_verdict": True,
                                                "exhausted": True}}),
        card(session={"exit_code": 1, "review": None}),
    ):
        out = classify(c)
        assert out is not None
        seen.add(out["kind"])
    assert seen == set(KINDS)


def test_detail_is_one_human_line():
    for c in (
        card(status="stopped"),
        card(status="done", session={"git": {"merge_conflict": True}}),
        card(session={"stop_reason": "limit"}),
        card(session={"verified": False}),
        card(status="done", session={"review": {"approved": False,
                                                "via": "log",
                                                "exhausted": True}}),
        card(status="done", session={"review": {"approved": False,
                                                "no_verdict": True,
                                                "exhausted": True}}),
        card(session={"exit_code": 1, "review": None}),
    ):
        out = classify(c)
        assert out["detail"] and "\n" not in out["detail"]
        assert out["detail"] == out["detail"].strip()


def test_classify_survives_junk_sessions():
    # Cards written by older versions (or hand-edited files) must not crash the
    # board: unknown shapes classify as "no failure" or as a crash.
    for session in ({}, {"review": "nope"}, {"git": []}, {"resolver": 3},
                    {"exit_code": "boom"}, {"stop_reason": 7}):
        out = classify({"id": "abc123", "status": "failed", "session": session})
        assert out is None or out["kind"] in KINDS


# ------------------------------------------------------------- through the API

def test_api_attaches_the_failure_to_the_card(tmp_path):
    e = make_env(tmp_path, test_cmd="false", max_review_rounds=1)
    with e.client:
        c = create_card(e.client, title="Red tests")
        e.client.post(f"/api/cards/{c['id']}/run")
        final = wait_for_status(e.client, c["id"], {"done", "failed"})
        failure = final["session"]["failure"]
        assert failure["kind"] == "tests_failed", failure
        assert "tests failed" in failure["detail"]

        # ...and the list endpoint carries it too (the MCP reads this one).
        listed = e.client.get("/api/cards").json()
        assert listed[0]["session"]["failure"]["kind"] == "tests_failed"


def test_api_reviewer_rejected(tmp_path):
    e = make_env(tmp_path, script=FAKE_VIBE_ALWAYS_REJECT, max_review_rounds=1)
    with e.client:
        c = create_card(e.client, title="Rejected")
        e.client.post(f"/api/cards/{c['id']}/run")
        final = wait_for_status(e.client, c["id"], {"done"})
        assert final["session"]["failure"]["kind"] == "reviewer_rejected"


def test_api_stopped_by_user(tmp_path):
    e = make_env(tmp_path, script=FAKE_VIBE_SLOW)
    with e.client:
        c = create_card(e.client, title="Stopped")
        e.client.post(f"/api/cards/{c['id']}/run")
        e.client.post(f"/api/cards/{c['id']}/stop")
        final = wait_for_status(e.client, c["id"], {"stopped"})
        assert final["session"]["failure"]["kind"] == "stopped_by_user"


def test_api_successful_card_has_no_failure(tmp_path):
    e = make_env(tmp_path)
    with e.client:
        c = create_card(e.client, title="Fine")
        e.client.post(f"/api/cards/{c['id']}/run")
        final = wait_for_status(e.client, c["id"], {"done"})
        assert "failure" not in final["session"]


# ------------------------------------------- what the runner records for us

FAKE_CLAUDE_SLOW = """#!/bin/sh
# Fake Claude Code that hangs until the harness watchdog kills it.
cat > /dev/null
echo "fake claude slow start"
sleep 30
exit 0
"""


def test_runner_records_a_watchdog_timeout(tmp_path):
    """The Claude backend has no native time limit, so the harness kills the
    session itself. That must be recorded as `timed_out` — otherwise a timeout
    is indistinguishable from the user pressing Stop."""
    from app.settings import SettingsStore

    settings = SettingsStore(tmp_path / "settings.json")
    settings.set_backend("claude")
    e = make_env(tmp_path, script=FAKE_CLAUDE_SLOW, settings=settings,
                 time_limit_seconds=1, claude_bin=str(tmp_path / "fake_vibe.sh"))
    with e.client:
        c = create_card(e.client, title="Too slow")
        e.client.post(f"/api/cards/{c['id']}/run")
        final = wait_for_status(e.client, c["id"], {"stopped"}, timeout=20)
        assert final["session"]["timed_out"] is True
        assert final["session"]["failure"]["kind"] == "timeout"


def test_runner_marks_a_review_round_without_a_verdict(tmp_path):
    """A review round that never calls submit_verdict and prints no VERDICT:
    line is recorded as `review.no_verdict`, so the classifier does not have to
    guess from the reason text."""
    e = make_env(tmp_path, script=FAKE_VIBE_NO_VERDICT, max_review_rounds=1)
    with e.client:
        c = create_card(e.client, title="Silent reviewer")
        e.client.post(f"/api/cards/{c['id']}/run")
        final = wait_for_status(e.client, c["id"], {"done"})
        assert final["session"]["review"]["no_verdict"] is True
        assert final["session"]["failure"]["kind"] == "no_verdict"
