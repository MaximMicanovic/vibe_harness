"""Tests for GET /api/search — find a card by text, on the board and in the archives.

The board's cards are already in the browser (`GET /api/cards` carries every
body), so board search is client-side. Archives load per folder, so finding an
archived card by text needs a server-side query: `GET /api/search?q=<text>`
scans the board's cards and every archived card and answers

    [{id, title, status, where, snippet}]

`where` is "board" or the archive's location ("<project> / <archive name>"),
`snippet` is the matching line trimmed to ~120 chars with the match highlighted
by offsets ({text, start, end}). Matching is case-insensitive over title, body
and id. The list is capped at 50; the response says so in headers
(X-Search-Truncated / X-Search-Total) so the documented list shape stays.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.archives import ArchiveStore
from app.cards import CardStore
from app.config import Config
from app.main import create_app
from app.projects import ProjectStore
from app.runner import SessionRunner
from app.stacks import StackStore

FAKE_VIBE = "#!/bin/sh\nexit 0\n"


@pytest.fixture()
def env(tmp_path) -> SimpleNamespace:
    cards = CardStore(tmp_path / "cards")
    projects = ProjectStore(tmp_path / "projects.json")
    stacks = StackStore(tmp_path / "stacks.json", cards)
    archives = ArchiveStore(tmp_path / "archives", cards, projects, stacks=stacks)
    fake = tmp_path / "fake_vibe.sh"
    fake.write_text(FAKE_VIBE)
    fake.chmod(0o755)
    config = Config(vibe_bin=str(fake), time_limit_seconds=30)
    runner = SessionRunner(cards, config, sessions_dir=tmp_path / "sessions",
                           projects=projects)
    app = create_app(config=config, store=cards, runner=runner, projects=projects,
                     archives=archives, stacks=stacks)
    client = TestClient(app)
    with client:
        yield SimpleNamespace(client=client, cards=cards, archives=archives,
                              projects=projects, tmp_path=tmp_path)
    client.close()


def accepted_card(store: CardStore, title: str, body: str, **kw) -> dict:
    """Create a card and walk it to accepted, so it can be archived."""
    card = store.create(title, body, **kw)
    store.set_status(card["id"], "running")
    store.set_status(card["id"], "done",
                     session_patch={"review": {"approved": True, "round": 1}})
    return store.accept(card["id"])


def search(env, q: str):
    r = env.client.get("/api/search", params={"q": q})
    assert r.status_code == 200, r.text
    return r


# ------------------------------------------------------------------ board

def test_search_finds_a_board_card_by_body(env):
    card = env.cards.create("Rebuild the flux capacitor", "the flux capacitor is old")
    r = search(env, "flux")
    results = r.json()
    assert [n["id"] for n in results] == [card["id"]]
    hit = results[0]
    assert hit["title"] == "Rebuild the flux capacitor"
    assert hit["status"] == "draft"
    assert hit["where"] == "board"


def test_search_matches_title_body_and_id_case_insensitively(env):
    by_title = env.cards.create("Zebra crossing", "nothing here")
    by_body = env.cards.create("Something else", "a ZEBRA appears in the body")
    by_id = env.cards.create("Third card", "no match in the text")
    # The id is searchable too — the card's own handle.
    env.cards.create("Fourth card", "no match either")

    found = {n["id"] for n in search(env, "zebra").json()}
    assert found == {by_title["id"], by_body["id"]}

    found = {n["id"] for n in search(env, by_id["id"].upper()).json()}
    assert by_id["id"] in found


def test_search_returns_nothing_for_an_empty_query(env):
    env.cards.create("Anything", "at all")
    assert search(env, "").json() == []
    assert search(env, "   ").json() == []


def test_search_returns_nothing_when_nothing_matches(env):
    env.cards.create("Anything", "at all")
    assert search(env, "no-such-word-anywhere").json() == []


# ---------------------------------------------------------------- archives

def test_search_finds_an_archived_card_by_body_and_names_the_archive(env):
    archive = env.archives.create("Old work", type="cards", project="Vibe Harness")
    card = accepted_card(env.cards, "Retired card", "the quokka lives in the body")
    env.archives.archive_card(card["id"], archive["id"])

    results = search(env, "quokka").json()
    assert [n["id"] for n in results] == [card["id"]]
    hit = results[0]
    assert hit["title"] == "Retired card"
    assert hit["status"] == "accepted"
    # The archive it lives in, named the way the board names it.
    assert hit["where"] == "Vibe Harness / Old work"


def test_search_names_an_unfiled_archive_by_its_name(env):
    archive = env.archives.create("Loose pile")
    card = accepted_card(env.cards, "Loose card", "a wombat in the body")
    env.archives.archive_card(card["id"], archive["id"])
    assert search(env, "wombat").json()[0]["where"] == "Loose pile"


def test_search_covers_the_board_and_every_archive_at_once(env):
    first = env.archives.create("First pile")
    second = env.archives.create("Second pile")
    on_board = env.cards.create("Board card", "a narwhal on the board")
    in_first = accepted_card(env.cards, "First card", "a narwhal in the first pile")
    in_second = accepted_card(env.cards, "Second card", "a narwhal in the second pile")
    env.archives.archive_card(in_first["id"], first["id"])
    env.archives.archive_card(in_second["id"], second["id"])

    results = search(env, "narwhal").json()
    assert {n["id"] for n in results} == {on_board["id"], in_first["id"], in_second["id"]}
    wheres = {n["id"]: n["where"] for n in results}
    assert wheres[on_board["id"]] == "board"
    assert wheres[in_first["id"]] == "First pile"
    assert wheres[in_second["id"]] == "Second pile"


def test_search_skips_unreadable_files_instead_of_failing(env):
    archive = env.archives.create("Broken pile")
    good = accepted_card(env.cards, "Good card", "a capybara in the body")
    env.archives.archive_card(good["id"], archive["id"])
    # A card file with no frontmatter at all: unreadable, must not 500.
    broken = env.tmp_path / "archives" / f"broken-pile-{archive['id']}" / "cards" / "broken.md"
    broken.write_text("not a card at all\n", encoding="utf-8")

    results = search(env, "capybara").json()
    assert [n["id"] for n in results] == [good["id"]]


def test_search_skips_a_broken_archive_manifest(env):
    archive = env.archives.create("Good pile")
    good = accepted_card(env.cards, "Good card", "a tapir in the body")
    env.archives.archive_card(good["id"], archive["id"])
    # An archive folder whose manifest has no frontmatter: skipped, not fatal.
    broken = env.tmp_path / "archives" / "broken-pile" / "archive.md"
    broken.parent.mkdir(parents=True)
    broken.write_text("no frontmatter here\n", encoding="utf-8")

    results = search(env, "tapir").json()
    assert [n["id"] for n in results] == [good["id"]]


# ----------------------------------------------------------------- snippet

def test_snippet_highlights_the_match_by_offsets(env):
    env.cards.create("A card", "first line\nsecond line mentions the PLATYPUS here\nthird")
    hit = search(env, "platypus").json()[0]
    snippet = hit["snippet"]
    assert snippet["text"].startswith("second line mentions")
    assert snippet["text"][snippet["start"]:snippet["end"]].lower() == "platypus"


def test_snippet_is_the_matching_line_trimmed_to_about_120_chars(env):
    long_line = "x" * 300 + " the axolotl " + "y" * 300
    env.cards.create("Long card", "intro\n" + long_line + "\noutro")
    snippet = search(env, "axolotl").json()[0]["snippet"]
    assert len(snippet["text"]) <= 130
    assert snippet["text"][snippet["start"]:snippet["end"]].lower() == "axolotl"
    assert "axolotl" in snippet["text"]


def test_snippet_falls_back_to_the_title_when_only_the_title_matches(env):
    env.cards.create("The pangolin card", "body without the word")
    snippet = search(env, "pangolin").json()[0]["snippet"]
    assert snippet["text"] == "The pangolin card"
    assert snippet["text"][snippet["start"]:snippet["end"]].lower() == "pangolin"


# --------------------------------------------------------------------- cap

def test_search_caps_the_result_count_and_says_when_it_was_capped(env):
    for i in range(60):
        env.cards.create(f"Card {i}", "a shared word: okapi")
    r = search(env, "okapi")
    assert len(r.json()) == 50
    assert r.headers["X-Search-Truncated"] == "true"
    assert r.headers["X-Search-Total"] == "60"


def test_search_reports_an_uncapped_result(env):
    env.cards.create("Only card", "a lone okapi")
    r = search(env, "okapi")
    assert len(r.json()) == 1
    assert r.headers["X-Search-Truncated"] == "false"
    assert r.headers["X-Search-Total"] == "1"


# ------------------------------------------------------------ store level

def test_search_store_returns_results_and_total(env):
    """The store-level function is what the route wraps; keep it testable."""
    from app.search import search_cards

    env.cards.create("One", "a marmot")
    results, total = search_cards("marmot", env.cards, env.archives)
    assert total == 1
    assert results[0]["where"] == "board"
