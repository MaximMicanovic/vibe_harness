"""The docs must describe the program that exists.

Card ce5fb1: the README still called the harness "Vibe-only" (it drives two
agent CLIs), understated the MCP tool count, and omitted fourteen shipped
features; `docs/architecture.md`'s module map listed 5 of the 28 modules in
`app/`. Prose drifts silently, so every claim that can be checked against the
code is checked here: the backend list, the tool count, the module map, the
license. A doc that lies fails the suite instead of waiting to be noticed.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import mcp_server

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
ARCHITECTURE = ROOT / "docs" / "architecture.md"
APP_DIR = ROOT / "app"

#: The README is a landing page; the detail belongs in `docs/`.
README_MAX_LINES = 100


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """The body of one `## heading` section, up to the next `## `."""
    match = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)",
                      text, re.M | re.S)
    assert match, f"README has no '## {heading}' section"
    return match.group(1)


def _served_tools() -> list[dict]:
    """The tools the server advertises over `tools/list` — the real surface."""
    resp = mcp_server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return resp["result"]["tools"]


def test_readme_does_not_claim_a_single_backend():
    """`app/backends.py` has two backends; the README must not say otherwise."""
    text = _read(README).lower()
    assert "vibe-only" not in text
    assert "no other agent clis are supported" not in text


def test_readme_names_both_backends_and_the_model_choice():
    """The backend is not a setting of its own: the model you pick picks it."""
    text = _read(README)
    assert "Mistral Vibe" in text
    assert "Claude Code" in text
    assert "headless" in text.lower()
    assert re.search(r"model you pick", text, re.I), (
        "the README must say the model choice decides the backend"
    )


def test_readme_requirements_accept_either_cli():
    """`vibe` is not required — `claude` alone is a complete install."""
    section = _section(_read(README), "Requirements").lower()
    assert "python 3.12" in section
    assert "uv" in section
    assert "vibe" in section and "claude" in section
    assert "either" in section, "the CLI requirement must be either/or"


def test_readme_tool_count_matches_the_mcp_server():
    """The quoted count is the served tool list, not a `"name":` source grep.

    Counting `"name":` lines overcounts by three — a JSON-schema property in
    `create_archive`, `args["name"]` in the dispatcher, and `serverInfo.name` —
    which is how the README once claimed 38 for 35 tools. `tools/list` is what
    a model actually sees, so that is what the README has to agree with.
    """
    count = len(_served_tools())
    assert count >= 30, "the tool list no longer looks like the harness MCP"
    # Whitespace-tolerant: a reflowed line must not break the claim.
    assert re.search(rf"\b{count}\s+tools", _read(README)), (
        f"README must quote the real tool count ({count})"
    )


#: One entry per shipped feature the README has to mention, with the words that
#: carry it. Lowercase; matched case-insensitively.
FEATURES = {
    "activity popup": ("activity", "steer"),
    "diff popup": ("line number", "side-by-side", "word-level", "syntax"),
    "accept all": ("accept all",),
    "revert": ("revert",),
    "pr mode": ("pull request",),
    "search": ("search",),
    "notifications": ("notification",),
    "concurrency cap": ("concurrency",),
    "sleep guard": ("sleep",),
    "transcripts": ("transcript",),
    "duplication review": ("duplicat",),
}


@pytest.mark.parametrize("feature,words", FEATURES.items(), ids=FEATURES.keys())
def test_readme_covers_a_shipped_feature(feature, words):
    text = _read(README).lower()
    missing = [word for word in words if word not in text]
    assert not missing, f"README does not mention {feature}: missing {missing}"


def test_readme_stays_short():
    lines = _read(README).splitlines()
    assert len(lines) <= README_MAX_LINES, (
        f"README is {len(lines)} lines; it is a landing page, not the manual"
    )


def test_architecture_lists_every_module():
    """Every `app/*.py` appears in the module map, by filename."""
    text = _read(ARCHITECTURE)
    missing = [path.name for path in sorted(APP_DIR.glob("*.py"))
               if not re.search(rf"\b{re.escape(path.name)}\b", text)]
    assert not missing, f"docs/architecture.md does not list: {missing}"


def test_architecture_mentions_the_frontend_and_transcript_store():
    text = _read(ARCHITECTURE).lower()
    assert "vanilla" in text
    assert "no build step" in text
    assert ".vibe-harness" in text, "the transcript store lives in .vibe-harness/"


def test_readme_license_claim_matches_the_repo():
    """The README says there is no LICENSE file; that must stay true."""
    has_license = any((ROOT / name).exists()
                      for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"))
    text = _read(README).lower()
    if has_license:
        assert "no license" not in text
    else:
        assert "no license" in text
