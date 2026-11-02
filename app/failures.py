"""Why a card failed — one line, instead of a log to read.

The runner already records the raw material on the card's session:
`exit_code`, `stop_reason`, `verified`, `test_output`, `git_error`,
`review.approved` / `review.reason` / `review.source` / `review.exhausted`,
`review_rounds`, `resolver` and `git.*`. `classify(card)` turns that into a
`{kind, detail}` pair — or `None` when the card did not fail.

It is a **pure function**: no store, no I/O, no clock. The same card always
classifies the same way, which is what makes it unit-testable against synthetic
session dicts and safe to call for every card on every board poll.

Kinds, in priority order (the first match wins):

1. `stopped_by_user` — the user pressed Stop.
2. `interrupted` — the session was signalled from outside (a sleeping machine
   suspends the process and the CLI gives up with `interrupted`). Not a crash:
   the work was never judged, so re-running is the right response.
3. `git_conflict` — a merge conflict / resolver was involved.
4. `timeout` — the session hit its time (or other) limit.
5. `tests_failed` — the test gate rejected it.
6. `reviewer_rejected` — the review loop ran out with a rejection.
7. `no_verdict` — the reviewer produced no verdict at all.
8. `crashed` — a non-zero exit (or a failed commit) with no other explanation.
"""
from __future__ import annotations

#: Every kind the classifier can return, in priority order. The board's filter
#: offers exactly these.
KINDS: tuple[str, ...] = (
    "stopped_by_user",
    "interrupted",
    "git_conflict",
    "timeout",
    "tests_failed",
    "reviewer_rejected",
    "no_verdict",
    "crashed",
)

#: `stop_reason` values that mean "the session ran out of budget" rather than
#: "the agent finished". Vibe writes `limit` (an iteration, token or price cap);
#: Claude Code's `terminal_reason` uses the others.
TIMEOUT_STOP_REASONS = frozenset({
    "limit", "time_limit", "timeout", "timed_out",
    "max_turns", "max_budget_usd", "blocking_limit",
})

#: `stop_reason` values that mean the session was signalled from outside rather
#: than failing on its own. A sleeping machine suspends the process and the CLI
#: reports `interrupted`; the work was never judged, so this is not a crash.
INTERRUPTED_STOP_REASONS = frozenset({
    "interrupted", "cancelled", "canceled", "sigterm", "sigint", "suspended",
})

#: Statuses that cannot have failed yet: nothing has finished. `accepted` is a
#: shipped card — whatever went wrong on the way is history, not a failure.
NOT_FAILED_STATUSES = frozenset({"draft", "running", "reviewing", "accepted"})

#: The runner's wording when a review round produced no verdict at all.
NO_VERDICT_REASON = "reviewer did not produce a verdict"


def _failure(kind: str, detail: str) -> dict:
    return {"kind": kind, "detail": detail}


def _rounds(n: int) -> str:
    return f"{n} round" if n == 1 else f"{n} rounds"


def _loop_rounds(session: dict, review: dict) -> int | None:
    """How many review/fix rounds the loop ran: the session counter, else the
    round recorded on the last review."""
    for value in (session.get("review_rounds"), review.get("round")):
        if isinstance(value, int) and value > 0:
            return value
    return None


def _git_conflict(git: dict, resolver: dict) -> dict | None:
    """A merge conflict, or a resolver session that was involved and failed."""
    branch = git.get("branch")
    where = f" on {branch}" if isinstance(branch, str) and branch else ""
    resolver_exit = resolver.get("exit_code")
    resolver_failed = isinstance(resolver_exit, int) and resolver_exit != 0
    if git.get("merge_conflict") is True:
        if git.get("resolving") is True:
            return _failure("git_conflict",
                            f"merge conflict{where} — a resolver session is working on it")
        if resolver_failed:
            return _failure("git_conflict",
                            f"merge conflict{where} — the resolver failed "
                            f"(exit {resolver_exit})")
        return _failure("git_conflict",
                        f"merge conflict{where} — Accept retries the merge")
    if git.get("resolver_error"):
        return _failure("git_conflict", f"merge conflict{where} — {git['resolver_error']}")
    if resolver_failed:
        return _failure("git_conflict",
                        f"the merge resolver failed{where} (exit {resolver_exit})")
    return None


def _test_gate_rejected(review: dict) -> bool:
    """The last rejection came from the test gate, not from the reviewer."""
    return review.get("approved") is False and review.get("source") == "test"


def _no_verdict(review: dict) -> bool:
    """The reviewer never answered: no verdict channel produced one."""
    if review.get("approved") is not False:
        return False
    if review.get("source") == "test":
        return False  # the test gate rejected it — that is a verdict of sorts
    if review.get("no_verdict") is True:
        return True
    if review.get("via"):
        return False  # a verdict arrived (submit_verdict or the log)
    reason = str(review.get("reason") or "").lower()
    return "did not produce a verdict" in reason or "no verdict" in reason


def _crash_detail(session: dict) -> str:
    """The most specific one-liner for a failure with no better explanation."""
    git_error = session.get("git_error")
    if git_error:
        return f"could not commit the result: {git_error}"
    exit_code = session.get("exit_code")
    if isinstance(exit_code, int) and exit_code != 0:
        return f"session exited with code {exit_code}"
    return "the session failed without a recorded reason"


def classify(card: dict) -> dict | None:
    """`{kind, detail}` for a card that failed, `None` for one that did not."""
    if not isinstance(card, dict):
        return None
    status = card.get("status")
    if status in NOT_FAILED_STATUSES:
        return None
    session = card.get("session")
    if not isinstance(session, dict):
        return None

    review = session.get("review")
    review = review if isinstance(review, dict) else {}
    git = session.get("git")
    git = git if isinstance(git, dict) else {}
    resolver = session.get("resolver")
    resolver = resolver if isinstance(resolver, dict) else {}
    stop_reason = session.get("stop_reason")
    stop_reason = stop_reason.strip().lower() if isinstance(stop_reason, str) else ""
    timed_out = session.get("timed_out") is True
    exit_code = session.get("exit_code")

    # 1. the user stopped it (a watchdog timeout is not the user's doing)
    if status == "stopped" and not timed_out:
        return _failure("stopped_by_user", "stopped by the user")

    # 2. the session was signalled from outside. A sleeping machine suspends
    #    the process and the CLI gives up with `interrupted` — the work was
    #    never judged, so this is not a crash and re-running is the fix.
    if status == "failed" and stop_reason in INTERRUPTED_STOP_REASONS:
        return _failure(
            "interrupted",
            "the session was interrupted from outside (the machine may have "
            "slept) — re-run it")

    # 3. a merge conflict / resolver was involved
    conflict = _git_conflict(git, resolver)
    if conflict is not None:
        return conflict

    # 3. the session hit its limit
    if timed_out:
        return _failure("timeout", "session hit the time limit")
    if stop_reason in TIMEOUT_STOP_REASONS:
        return _failure("timeout", f"session hit its limit ({stop_reason})")

    # 4. the test gate rejected it
    if session.get("verified") is False or _test_gate_rejected(review) or git.get("test_error"):
        rounds = _loop_rounds(session, review)
        detail = (f"tests failed after {_rounds(rounds)}" if rounds
                  else "the test command failed")
        return _failure("tests_failed", detail)

    # 5. the review loop ran out with a rejection
    exhausted = review.get("exhausted") is True and review.get("approved") is False
    if exhausted and not _no_verdict(review):
        rounds = _loop_rounds(session, review)
        detail = (f"reviewer rejected it after {_rounds(rounds)}" if rounds
                  else "the reviewer rejected it")
        return _failure("reviewer_rejected", detail)

    # 6. the reviewer produced no verdict
    if _no_verdict(review):
        return _failure("no_verdict", "the reviewer produced no verdict")

    # 7. non-zero exit (or a failed commit) with no other explanation
    if review.get("approved") is True:
        # The reviewer approved it: a noisy exit code from the review process
        # is not a failure — the card shipped.
        return None
    if isinstance(exit_code, int) and exit_code != 0:
        return _failure("crashed", _crash_detail(session))
    if status == "failed":
        return _failure("crashed", _crash_detail(session))
    return None
