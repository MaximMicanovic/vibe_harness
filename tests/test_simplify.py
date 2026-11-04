"""SIMPLIFY-card guards: the USD cost system is gone, the dead per-card model
pin is gone, and card/state file writes are atomic.

The forbidden names below are built by concatenation so this guard file itself
stays clean of them (the card's verify greps `app/` and `tests/` for these).
"""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = [
    "cost" + "_usd",        # session/card/export/total money keys
    "max" + "_price",       # the price-cap setting and its env var
    "fmt" + "Cost",         # the UI formatter
    "total" + "_cost",
    "max-" + "budget",      # the claude budget flag
    "stat-" + "cost",       # the stats-bar element
    "chip-" + "cost",       # the per-card cost chip
    "card-" + "model",      # the dead per-card model picker
]


def _source_files():
    for base in (ROOT / "app", ROOT / "tests"):
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            yield path


def test_no_cost_or_dead_picker_symbols_in_sources():
    """grep -ri over app/ and tests/ finds none of the removed symbols."""
    hits = []
    for path in _source_files():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        low = text.lower()
        for pat in FORBIDDEN:
            if pat.lower() in low:
                hits.append(f"{path.relative_to(ROOT)}: {pat}")
    assert not hits, "removed symbols still present:\n" + "\n".join(hits)


def test_tokens_module_keeps_tokens_and_drops_money():
    import app.tokens as tokens

    for name in ("export" + "_cost" + "_usd", "card" + "_cost" + "_usd",
                 "_" + "cost", "_" + "round" + "_cost"):
        assert not hasattr(tokens, name), name
    # The token side is untouched.
    assert tokens.TOKEN_BREAKDOWN_KEYS == ("cached", "output", "input")
    session = {"tokens": 100,
               "reviews": [{"round": 1, "tokens": 20}],
               "resolver": {"round": 1, "tokens": 5}}
    assert tokens.card_tokens(session) == 125
    assert tokens.card_token_breakdown({"tokens": 1, "token_breakdown": {"input": 7}}) == {
        "cached": None, "output": None, "input": 7}
    assert tokens.combine_live(10, {"tokens": 5}) == 15
    assert tokens.combine_live(10, {"tokens": 50, "cumulative": True}) == 50


def test_config_has_no_price_cap():
    from app.config import Config, load_config

    assert not hasattr(Config(), "max" + "_price" + "_usd")
    cfg = load_config({"VIBE_HARNESS_MAX_" + "PRICE": "9.99"})
    assert not hasattr(cfg, "max" + "_price" + "_usd")
    assert cfg.time_limit_seconds == 1800  # time limits stay


def test_backends_never_pass_price_flags(tmp_path):
    from app.backends import ClaudeBackend, VibeBackend
    from app.config import Config

    config = Config(vibe_bin="vibe", claude_bin="claude", time_limit_seconds=30)
    for backend in (VibeBackend(), ClaudeBackend()):
        for kind in ("run", "review"):
            cmd = backend.build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                    ws=tmp_path, rdir=tmp_path, kind=kind,
                                    resume=None, mcp_config=None)
            assert "--max-" + "price" not in cmd
            assert "--max-" + "budget-usd" not in cmd
    # The time limit stays on the vibe command line.
    cmd = VibeBackend().build_cmd(config=config, prompt_path=tmp_path / "p.md",
                                  ws=tmp_path, rdir=tmp_path, kind="run",
                                  resume=None, mcp_config=None)
    assert "--time-limit" in cmd and "30" in cmd


def test_card_store_has_no_model_pin_and_no_cost_session_key(tmp_path):
    from app.cards import CardStore, SESSION_KEYS

    assert "cost" + "_usd" not in SESSION_KEYS
    store = CardStore(tmp_path / "cards")
    assert "model" not in inspect.signature(store.create).parameters
    assert "model" not in inspect.signature(store.update).parameters
    card = store.create("Title", "body")
    assert "model" not in card
    assert "model" not in (card.get("session") or {})
    # The session's own `model` (the model the round actually ran on) stays.
    patched = store.patch_session(card["id"], {"model": "fake-model", "tokens": 5})
    assert patched["session"]["model"] == "fake-model"
    running = store.set_status(card["id"], "running")
    assert "model" in running["session"]  # materialized SESSION_KEYS include it


def test_api_stats_and_cards_carry_tokens_but_no_cost(tmp_path):
    from test_api import create_card, make_env

    env = make_env(tmp_path)
    card = create_card(env.client)
    env.store.set_status(card["id"], "running")
    env.store.set_status(card["id"], "done", session_patch={
        "tokens": 1000,
        "reviews": [{"round": 1, "tokens": 250, "approved": True}],
    })
    final = env.client.get(f"/api/cards/{card['id']}").json()
    assert final["session"]["card_tokens"] == 1250
    assert "card" + "_cost" + "_usd" not in final["session"]
    stats = env.client.get("/api/stats").json()
    assert stats["total_tokens"] == 1250
    assert "total" + "_cost" + "_usd" not in stats
    # The dead picker's write path is gone too: a PUT naming a model changes nothing.
    env.client.put(f"/api/cards/{card['id']}", json={"title": "Renamed"})
    again = env.client.get(f"/api/cards/{card['id']}").json()
    assert again["title"] == "Renamed"
    assert "model" not in again


def test_mcp_stats_tool_description_mentions_no_money():
    from app import mcp_server

    stats_tool = next(t for t in mcp_server.TOOLS if t["name"] == "stats")
    desc = stats_tool["description"].lower()
    assert "usd" not in desc and "cost" not in desc and "price" not in desc
    assert "token" in desc


def test_project_store_save_is_atomic(tmp_path, monkeypatch):
    from app.projects import ProjectStore

    store = ProjectStore(tmp_path / "projects.json")
    store.create("alpha", str(tmp_path / "alpha"))
    original = store.path.read_text(encoding="utf-8")

    real_replace = os.replace

    def boom(*args, **kwargs):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.create("beta", str(tmp_path / "beta"))
    monkeypatch.setattr(os, "replace", real_replace)
    # The crash never corrupted the store: the old file is intact and parses.
    assert store.path.read_text(encoding="utf-8") == original
    assert [p["name"] for p in store.list()] == ["alpha"]
    # A clean save leaves no temp files behind.
    store.delete("alpha")
    assert list(tmp_path.glob("*.tmp-*")) == []
    assert store.list() == []


def test_card_store_write_is_atomic(tmp_path, monkeypatch):
    from app import mdstore

    from app.cards import CardStore

    store = CardStore(tmp_path / "cards")
    card = store.create("Title", "body")
    path = next((tmp_path / "cards").glob("*.md"))
    original = path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("simulated crash between write and rename")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        store.set_status(card["id"], "running")
    monkeypatch.undo()
    # The card file survived the crash untouched and still parses.
    assert path.read_text(encoding="utf-8") == original
    md, _ = mdstore.load(path)
    assert md["status"] == "draft"
    assert store.get(card["id"])["status"] == "draft"
    # A clean write leaves no temp files behind.
    store.set_status(card["id"], "running")
    assert list((tmp_path / "cards").glob("*.tmp-*")) == []
    assert store.get(card["id"])["status"] == "running"
