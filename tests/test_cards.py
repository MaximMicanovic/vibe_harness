"""Tests for app.cards.CardStore."""
from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest

from app.cards import (
    SESSION_KEYS,
    STATUSES,
    CardConflict,
    DependencyError,
    InvalidTransition,
    CardNotFound,
    CardRunning,
    CardStore,
)


@pytest.fixture
def store(tmp_path) -> CardStore:
    return CardStore(tmp_path / "cards")


# ----------------------------------------------------------------------
# create / read
# ----------------------------------------------------------------------

def test_create_read_round_trip(store: CardStore, tmp_path: Path):
    card = store.create("Dark mode toggle", "# Spec\n\nToggle the theme.")

    assert card["id"] and len(card["id"]) == 6
    int(card["id"], 16)  # hex
    assert card["title"] == "Dark mode toggle"
    assert card["status"] == "draft"
    assert card["status"] in STATUSES
    assert card["body"] == "# Spec\n\nToggle the theme."
    assert card["template"] is None
    assert card["auto_run"] is False
    assert card["accepted"] is False
    assert card["session"] is None
    # ISO 8601 UTC timestamps
    for key in ("created_at", "updated_at"):
        assert card[key].endswith("+00:00")

    # File on disk: cards/<slug>-<id>.md, slug derived from the title.
    files = list((tmp_path / "cards").glob("*.md"))
    assert len(files) == 1
    assert files[0].name == f"dark-mode-toggle-{card['id']}.md"

    # Read back through the store.
    fetched = store.get(card["id"])
    assert fetched == card


def test_frontmatter_round_trip(store: CardStore, tmp_path: Path):
    card = store.create("Web App", "body text", template="web-app", auto_run=True)
    path = tmp_path / "cards" / f"web-app-{card['id']}.md"

    post = frontmatter.load(path)
    assert post.metadata["id"] == card["id"]
    assert post.metadata["title"] == "Web App"
    assert post.metadata["status"] == "draft"
    assert post.metadata["template"] == "web-app"
    assert post.metadata["auto_run"] is True
    assert post.metadata["accepted"] is False
    assert post.content == "body text"


def test_slug_collapses_non_alphanumeric(store: CardStore, tmp_path: Path):
    card = store.create("Hello, World!  Foo__Bar", "x")
    path = tmp_path / "cards" / f"hello-world-foo-bar-{card['id']}.md"
    assert path.exists()


def test_get_matches_frontmatter_id_not_filename(store: CardStore, tmp_path: Path):
    card = store.create("Some title", "body")
    old_path = tmp_path / "cards" / f"some-title-{card['id']}.md"
    old_path.rename(tmp_path / "cards" / f"renamed-{card['id']}.md")
    assert store.get(card["id"])["title"] == "Some title"


# ----------------------------------------------------------------------
# update
# ----------------------------------------------------------------------

def test_update_only_provided_fields(store: CardStore):
    card = store.create("Title A", "body A")
    updated = store.update(card["id"], title="Title B")
    assert updated["title"] == "Title B"
    assert updated["body"] == "body A"          # untouched
    assert updated["status"] == "draft"        # untouched
    assert updated["created_at"] == card["created_at"]

    updated2 = store.update(card["id"], auto_run=True)
    assert updated2["auto_run"] is True
    assert updated2["title"] == "Title B"


def test_update_while_running_raises(store: CardStore):
    card = store.create("T", "b")
    store.set_status(card["id"], "running")
    with pytest.raises(CardRunning):
        store.update(card["id"], title="new title")
    with pytest.raises(CardRunning):
        store.update(card["id"], body="new body")


def test_edit_done_card_resets_to_draft(store: CardStore):
    card = store.create("T", "original body")
    store.set_status(card["id"], "running",
                     session_patch={"pid": 123, "workdir": "sessions/x/workspace"})
    store.set_status(card["id"], "done", session_patch={"exit_code": 0})

    updated = store.update(card["id"], body="edited body")
    assert updated["status"] == "draft"
    assert updated["session"] is None
    assert updated["accepted"] is False
    assert updated["body"] == "edited body"


def test_update_with_stale_expected_updated_at_raises(store: CardStore):
    """Optimistic concurrency: a save that names the version it loaded is
    refused when somebody else wrote the card in between."""
    card = store.create("T", "original body")
    stale = card["updated_at"]
    store.update(card["id"], body="their edit")   # the other person saves first

    with pytest.raises(CardConflict) as exc:
        store.update(card["id"], body="my edit", expected_updated_at=stale)
    assert "changed since you opened it" in str(exc.value)
    # The refused save wrote nothing.
    assert store.get(card["id"])["body"] == "their edit"


def test_update_with_matching_expected_updated_at_succeeds(store: CardStore):
    card = store.create("T", "original body")
    updated = store.update(card["id"], body="my edit",
                           expected_updated_at=card["updated_at"])
    assert updated["body"] == "my edit"
    assert updated["updated_at"] != card["updated_at"]


def test_update_without_expected_updated_at_is_unconditional(store: CardStore):
    """MCP tools and scripts send no expected_updated_at: last write wins."""
    card = store.create("T", "original body")
    store.update(card["id"], body="their edit")
    updated = store.update(card["id"], body="script edit")
    assert updated["body"] == "script edit"


def test_edit_done_card_without_changes_keeps_status(store: CardStore):
    card = store.create("T", "body")
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done")

    # Same title/body -> no reset; only auto_run flips.
    updated = store.update(card["id"], title="T", body="body", auto_run=True)
    assert updated["status"] == "done"
    assert updated["auto_run"] is True
    assert updated["session"] is not None


# ----------------------------------------------------------------------
# delete
# ----------------------------------------------------------------------

def test_delete_while_running_raises(store: CardStore, tmp_path: Path):
    card = store.create("T", "b")
    store.set_status(card["id"], "running")
    with pytest.raises(CardRunning):
        store.delete(card["id"])
    assert store.get(card["id"])["status"] == "running"


def test_delete_removes_file(store: CardStore, tmp_path: Path):
    card = store.create("T", "b")
    store.delete(card["id"])
    assert list((tmp_path / "cards").glob("*.md")) == []
    with pytest.raises(CardNotFound):
        store.get(card["id"])


# ----------------------------------------------------------------------
# set_status / accept
# ----------------------------------------------------------------------

def test_set_status_valid_transitions(store: CardStore):
    card = store.create("T", "b")
    n = store.set_status(card["id"], "running")
    assert n["status"] == "running"
    for final in ("done", "failed", "stopped", "reviewing"):
        nid = store.create("T", "b")["id"]
        store.set_status(nid, "running")
        assert store.set_status(nid, final)["status"] == final

    # Review loop: reviewing -> done (approved) or -> running (fixer).
    nid = store.create("T", "b")["id"]
    store.set_status(nid, "running")
    store.set_status(nid, "reviewing")
    assert store.set_status(nid, "done")["status"] == "done"

    nid = store.create("T", "b")["id"]
    store.set_status(nid, "running")
    store.set_status(nid, "reviewing")
    assert store.set_status(nid, "running")["status"] == "running"


def test_set_status_rejects_invalid_transitions(store: CardStore):
    card = store.create("T", "b")
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "done")       # draft -> done
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "accepted")   # draft -> accepted
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "reviewing")  # draft -> reviewing
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "bogus")      # unknown status

    store.set_status(card["id"], "running")
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "draft")      # running -> draft
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "accepted")   # running -> accepted

    store.set_status(card["id"], "done")
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "running")    # done -> running
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "failed")     # done -> failed
    with pytest.raises(InvalidTransition):
        store.set_status(card["id"], "reviewing")  # done -> reviewing


def test_set_status_merges_session_patch(store: CardStore):
    card = store.create("T", "b")
    n = store.set_status(card["id"], "running",
                         session_patch={"pid": 4242, "workdir": "sessions/x/workspace"})
    session = n["session"]
    assert session["pid"] == 4242
    assert session["workdir"] == "sessions/x/workspace"
    # Missing keys are materialized as null.
    for key in SESSION_KEYS:
        assert key in session
    assert session["exit_code"] is None
    assert session["finished_at"] is None

    # Patch merges into the existing session rather than replacing it.
    n2 = store.set_status(card["id"], "done",
                          session_patch={"exit_code": 0, "tokens": 123})
    assert n2["session"]["pid"] == 4242
    assert n2["session"]["exit_code"] == 0
    assert n2["session"]["tokens"] == 123


def test_accept_only_from_done(store: CardStore):
    card = store.create("T", "b")
    with pytest.raises(InvalidTransition):
        store.accept(card["id"])                   # draft

    store.set_status(card["id"], "running")
    with pytest.raises(InvalidTransition):
        store.accept(card["id"])                   # running

    store.set_status(card["id"], "failed")
    with pytest.raises(InvalidTransition):
        store.accept(card["id"])                   # failed

    # done without an approved review -> still blocked
    unreviewed = store.create("T2", "b")
    store.set_status(unreviewed["id"], "running")
    store.set_status(unreviewed["id"], "done")
    with pytest.raises(InvalidTransition):
        store.accept(unreviewed["id"])

    # done with an approved review -> accepted
    done_card = store.create("T3", "b")
    store.set_status(done_card["id"], "running")
    store.set_status(done_card["id"], "done",
                     session_patch={"review": {"approved": True}})
    accepted = store.accept(done_card["id"])
    assert accepted["status"] == "accepted"
    assert accepted["accepted"] is True

    with pytest.raises(InvalidTransition):
        store.accept(done_card["id"])              # accepted -> accepted


def test_accept_requires_approved_review(store: CardStore):
    card = store.create("T", "b")
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done",
                     session_patch={"review": {"approved": False,
                                               "reason": "bugs found"}})
    with pytest.raises(InvalidTransition):
        store.accept(card["id"])

    other = store.create("T2", "b")
    store.set_status(other["id"], "running")
    store.set_status(other["id"], "done",
                     session_patch={"review": {"approved": True}})
    assert store.accept(other["id"])["status"] == "accepted"


def test_accept_allowed_when_review_rounds_exhausted(store: CardStore):
    """done + review.exhausted (loop cap hit): the user may accept manually."""
    card = store.create("T", "b")
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done",
                     session_patch={"review": {"approved": False,
                                               "exhausted": True,
                                               "reason": "bugs found"}})
    accepted = store.accept(card["id"])
    assert accepted["status"] == "accepted"
    assert accepted["accepted"] is True


def test_reviewing_blocks_edit_delete_reset(store: CardStore):
    card = store.create("T", "b")
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "reviewing")
    with pytest.raises(CardRunning):
        store.update(card["id"], body="x")
    with pytest.raises(CardRunning):
        store.delete(card["id"])
    with pytest.raises(CardRunning):
        store.reset_to_draft(card["id"])


def test_auto_accept_field_round_trip(store: CardStore):
    card = store.create("T", "b", auto_accept=True)
    assert card["auto_accept"] is True
    assert store.get(card["id"])["auto_accept"] is True

    # A settings change does not reset the card.
    updated = store.update(card["id"], auto_accept=False)
    assert updated["auto_accept"] is False
    assert updated["status"] == "draft"

    # A content edit resets to draft but keeps the auto_accept flag.
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done",
                     session_patch={"review": {"approved": True}})
    store.accept(card["id"])
    edited = store.update(card["id"], body="new body")
    assert edited["status"] == "draft"
    assert edited["auto_accept"] is False
    assert edited["session"] is None


# ----------------------------------------------------------------------
# list / get
# ----------------------------------------------------------------------

def test_list_sorting_most_recent_first(store: CardStore):
    a = store.create("A", "body a")
    b = store.create("B", "body b")
    assert [n["id"] for n in store.list()] == [b["id"], a["id"]]

    store.update(a["id"], body="body a v2")        # a becomes most recent
    assert [n["id"] for n in store.list()] == [a["id"], b["id"]]

    store.update(b["id"], body="body b v2")
    assert [n["id"] for n in store.list()] == [b["id"], a["id"]]


def test_list_skips_unparseable_files(store: CardStore, tmp_path: Path):
    store.create("Good", "body")
    (tmp_path / "cards" / "broken.md").write_text("---\n: : :\n---\nnot yaml: [")
    (tmp_path / "cards" / "no-frontmatter.md").write_text("just markdown, no fm")
    cards = store.list()
    assert [n["title"] for n in cards] == ["Good"]


def test_get_unknown_id_raises(store: CardStore):
    with pytest.raises(CardNotFound):
        store.get("ffffff")
    with pytest.raises(CardNotFound):
        store.update("ffffff", title="x")
    with pytest.raises(CardNotFound):
        store.delete("ffffff")
    with pytest.raises(CardNotFound):
        store.set_status("ffffff", "running")
    with pytest.raises(CardNotFound):
        store.accept("ffffff")


def test_reset_to_draft(store: CardStore):
    card = store.create("T", "body")
    store.set_status(card["id"], "running", session_patch={"pid": 1})
    store.set_status(card["id"], "done",
                     session_patch={"exit_code": 0, "tokens": 5,
                                    "review": {"approved": True}})
    store.accept(card["id"])
    assert store.get(card["id"])["status"] == "accepted"

    reset = store.reset_to_draft(card["id"])
    assert reset["status"] == "draft"
    assert reset["session"] is None
    assert reset["accepted"] is False
    assert reset["body"] == "body"

    # A running card cannot be reset.
    store.set_status(reset["id"], "running")
    with pytest.raises(CardRunning):
        store.reset_to_draft(reset["id"])


def test_project_and_workdir_fields(store: CardStore):
    card = store.create("T", "b", project="my-app", workdir="projects/my-app")
    assert card["project"] == "my-app"
    assert card["workdir"] == "projects/my-app"

    plain = store.create("U", "b")
    assert plain["project"] is None
    assert plain["workdir"] is None

    # Settings-only updates do not reset a finished card.
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done")
    updated = store.update(card["id"], project="other", workdir="/abs/path")
    assert updated["project"] == "other"
    assert updated["workdir"] == "/abs/path"
    assert updated["status"] == "done"


# ----------------------------------------------------------------------
# dependencies ("runs after" links)
# ----------------------------------------------------------------------

def test_depends_on_defaults_to_empty(store: CardStore):
    card = store.create("T", "b")
    assert card["depends_on"] == []
    assert store.get(card["id"])["depends_on"] == []


def test_add_and_remove_dependency(store: CardStore):
    a = store.create("A", "body a")
    b = store.create("B", "body b")
    linked = store.add_dependency(a["id"], b["id"])
    assert linked["depends_on"] == [b["id"]]
    # persisted on disk
    assert store.get(a["id"])["depends_on"] == [b["id"]]
    # idempotent
    assert store.add_dependency(a["id"], b["id"])["depends_on"] == [b["id"]]
    # a second edge accumulates
    c = store.create("C", "body c")
    assert store.add_dependency(a["id"], c["id"])["depends_on"] == [b["id"], c["id"]]
    # removal (idempotent)
    assert store.remove_dependency(a["id"], b["id"])["depends_on"] == [c["id"]]
    assert store.remove_dependency(a["id"], b["id"])["depends_on"] == [c["id"]]
    assert store.remove_dependency(a["id"], c["id"])["depends_on"] == []


def test_add_dependency_self_and_unknown(store: CardStore):
    a = store.create("A", "body a")
    with pytest.raises(DependencyError):
        store.add_dependency(a["id"], a["id"])
    with pytest.raises(CardNotFound):
        store.add_dependency(a["id"], "ffffff")
    with pytest.raises(CardNotFound):
        store.add_dependency("ffffff", a["id"])
    with pytest.raises(CardNotFound):
        store.remove_dependency("ffffff", a["id"])


def test_add_dependency_cycle_rejected(store: CardStore):
    a = store.create("A", "body a")
    b = store.create("B", "body b")
    c = store.create("C", "body c")
    store.add_dependency(a["id"], b["id"])   # a -> b
    store.add_dependency(b["id"], c["id"])   # b -> c
    # a -> c is fine (no cycle)
    store.add_dependency(a["id"], c["id"])
    # c -> a would close a cycle
    with pytest.raises(DependencyError):
        store.add_dependency(c["id"], a["id"])
    # b -> a would too (a -> b exists)
    with pytest.raises(DependencyError):
        store.add_dependency(b["id"], a["id"])
    # and a direct self-loop via a longer chain: c -> b is fine? b -> c exists,
    # so c -> b closes a cycle
    with pytest.raises(DependencyError):
        store.add_dependency(c["id"], b["id"])
    assert store.get(c["id"])["depends_on"] == []


def test_dependency_changes_blocked_while_live(store: CardStore):
    a = store.create("A", "body a")
    b = store.create("B", "body b")
    store.set_status(a["id"], "running")
    with pytest.raises(CardRunning):
        store.add_dependency(a["id"], b["id"])
    with pytest.raises(CardRunning):
        store.remove_dependency(a["id"], b["id"])


def test_delete_card_strips_dependency_references(store: CardStore):
    a = store.create("A", "body a")
    b = store.create("B", "body b")
    c = store.create("C", "body c")
    store.add_dependency(a["id"], b["id"])
    store.add_dependency(c["id"], b["id"])
    store.delete(b["id"])
    assert store.get(a["id"])["depends_on"] == []
    assert store.get(c["id"])["depends_on"] == []


def test_dependencies_view(store: CardStore):
    a = store.create("A", "body a")
    b = store.create("B", "body b")
    c = store.create("C", "body c")
    store.add_dependency(a["id"], b["id"])
    store.add_dependency(a["id"], c["id"])
    store.set_status(b["id"], "running")
    store.set_status(b["id"], "done", session_patch={"review": {"approved": True}})
    deps = store.dependencies(a["id"])
    assert deps["depends_on"] == [b["id"], c["id"]]
    # b is only done (reviewer-approved, not merged) — it still blocks.
    assert deps["unmet"] == [b["id"], c["id"]]
    assert deps["blocks"] == []
    assert store.dependencies(b["id"])["blocks"] == [a["id"]]
    # Merging b satisfies it; c is still a draft.
    store.set_status(b["id"], "accepted")
    assert store.dependencies(a["id"])["unmet"] == [c["id"]]
    # a deleted dependency counts as satisfied
    store.delete(c["id"])
    assert store.dependencies(a["id"])["unmet"] == []
    with pytest.raises(CardNotFound):
        store.dependencies("ffffff")


# ----------------------------------------------------------------------
# revert: accepted -> draft
# ----------------------------------------------------------------------

def test_accepted_to_draft_transition_is_allowed(store: CardStore):
    """`accepted` is no longer a dead end: a revert reopens the card."""
    from app.cards import TRANSITIONS
    assert "draft" in TRANSITIONS["accepted"]

    card = store.create("T", "b")
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done", session_patch={"review": {"approved": True}})
    store.accept(card["id"])
    reopened = store.set_status(card["id"], "draft")
    assert reopened["status"] == "draft"
    # The accepted flag follows the status: a draft card is not accepted.
    assert reopened["accepted"] is False


def test_reopen_after_revert_keeps_session_history(store: CardStore):
    """Reopening after a revert keeps the session (tokens, logs, reviews, git)
    so the cost record of the accepted run survives, and records the revert."""
    card = store.create("T", "b")
    store.set_status(card["id"], "running",
                     session_patch={"pid": 4242, "tokens": 1234,
                                    "log": "sessions/x/run-1/session.log"})
    store.set_status(card["id"], "done",
                     session_patch={"review": {"approved": True, "round": 1}})
    store.accept(card["id"])
    store.patch_session(card["id"], {"git": {"merge_commit": "a" * 40}})

    reopened = store.reopen_after_revert(
        card["id"], {"reverted_at": "2026-01-01T00:00:00+00:00",
                     "revert_commit": "b" * 40})
    assert reopened["status"] == "draft"
    assert reopened["accepted"] is False
    session = reopened["session"]
    assert session["tokens"] == 1234
    assert session["log"] == "sessions/x/run-1/session.log"
    assert session["review"] == {"approved": True, "round": 1}
    assert session["git"]["merge_commit"] == "a" * 40
    assert session["revert_commit"] == "b" * 40
    assert session["reverted_at"] == "2026-01-01T00:00:00+00:00"

    # Only an accepted card can be reopened.
    with pytest.raises(InvalidTransition):
        store.reopen_after_revert(card["id"], {})
