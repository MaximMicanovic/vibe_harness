"""Documentation guards: the docs must match the code.

`MCPTOOLS.md` is the reference a spawned model is pointed at, so it has to
document every tool the harness MCP publishes — derived from
`app.mcp_server.TOOLS`, never from memory. The same goes for the env vars in
`app/config.py` and the features described in `docs/`.

The tests parse the code and the markdown instead of hard-coding counts, so a
new tool or env var fails here until it is documented.
"""
from __future__ import annotations

import re
from pathlib import Path

from app import config as app_config
from app import mailbox_server, mcp_server

ROOT = Path(__file__).resolve().parent.parent
MCPTOOLS = ROOT / "MCPTOOLS.md"
DOCS = ROOT / "docs"
ROUTE = ROOT / "route.md"

#: A markdown table row whose first cell is a backticked tool name.
ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|")


def _section(text: str, needle: str) -> str:
    """The `## ` section whose heading contains `needle` (up to the next `## `)."""
    parts = re.split(r"(?m)^## ", text)
    for part in parts[1:]:
        if needle.lower() in part.splitlines()[0].lower():
            return part
    raise AssertionError(f"no '## ' section mentioning {needle!r}")


def _rows(section: str) -> set[str]:
    return {m.group(1) for line in section.splitlines() if (m := ROW.match(line))}


def _env_vars_in_config() -> set[str]:
    return set(re.findall(r"VIBE_HARNESS_[A-Z0-9_]+", (ROOT / "app" / "config.py").read_text()))


def test_mcptools_documents_every_harness_tool():
    """One entry per tool in `app.mcp_server.TOOLS` — no more, no fewer."""
    text = MCPTOOLS.read_text()
    section = _section(text, "Harness MCP")
    documented = _rows(section)
    expected = {tool["name"] for tool in mcp_server.TOOLS}
    assert documented == expected, (
        f"undocumented: {sorted(expected - documented)}; "
        f"invented: {sorted(documented - expected)}")
    # The heading states the real count, so a stale number cannot survive.
    assert f"{len(mcp_server.TOOLS)} tools" in section.splitlines()[0]


def test_mcptools_documents_the_mailbox_tools():
    """The mailbox server's three tools are documented next to the harness ones."""
    section = _section(MCPTOOLS.read_text(), "Mailbox MCP")
    assert _rows(section) == {tool["name"] for tool in mailbox_server.TOOLS}


def test_mcptools_states_the_published_prefix_and_the_groups():
    text = MCPTOOLS.read_text()
    # How a model sees them: <server name>_<tool> in the mcp_harness group.
    assert "harness_<tool>" in text
    assert "mcp_harness" in text
    # The required grouping.
    for group in ("cards", "dependencies", "stacks", "archives", "transcripts",
                  "models", "projects", "templates", "stats", "ask_user"):
        assert group in text.lower(), group


def test_mcptools_marks_the_read_only_tools():
    """Read-only vs mutating is stated, and the read-only set is the real one."""
    text = MCPTOOLS.read_text()
    assert "read-only" in text.lower()
    read_only = {"current_card", "card_log", "stats", "list_cards", "get_card",
                 "list_templates", "list_models", "list_transcripts",
                 "get_transcript", "list_projects", "list_dependencies",
                 "list_archives", "get_archive", "list_stacks"}
    section = _section(text, "read-only")
    assert read_only <= _rows(section) | set(re.findall(r"`([a-z_]+)`", section))


def test_mcptools_mentions_no_tool_that_does_not_exist():
    """Every `harness_<name>` in the docs is a real tool (no invented surface)."""
    known = {tool["name"] for tool in mcp_server.TOOLS}
    # `harness_<tool>` as a tool reference — not the `mcp_harness_mailbox` group
    # name, which also contains "harness_".
    ref = re.compile(r"(?<![A-Za-z0-9_-])harness_([a-z_]+)")
    for path in [MCPTOOLS, *sorted(DOCS.glob("*.md")), ROUTE]:
        for name in ref.findall(path.read_text()):
            assert name in known, f"{path.name} mentions harness_{name}, which does not exist"


def test_configuration_documents_every_env_var():
    """Every VIBE_HARNESS_* `app/config.py` reads has a row in the table."""
    text = (DOCS / "configuration.md").read_text()
    missing = sorted(v for v in _env_vars_in_config() if v not in text)
    assert not missing, f"docs/configuration.md is missing: {missing}"


def test_configuration_rows_are_real_env_vars():
    """No documented env var is invented: each one is read somewhere in `app/`."""
    text = (DOCS / "configuration.md").read_text()
    read = set()
    for path in (ROOT / "app").glob("*.py"):
        read |= set(re.findall(r"VIBE_HARNESS_[A-Z0-9_]+", path.read_text()))
    documented = set(re.findall(r"VIBE_HARNESS_[A-Z0-9_]+", text))
    assert documented <= read, f"documented but never read: {sorted(documented - read)}"


def test_configuration_documents_the_three_new_switches():
    """MAILBOX / REVIEW_DIFF / WORKER_RESUME: default on, and what they do."""
    text = (DOCS / "configuration.md").read_text()
    defaults = app_config.load_config({})
    assert defaults.mailbox is True
    assert defaults.review_diff is True
    assert defaults.worker_resume is True
    for var in ("VIBE_HARNESS_MAILBOX", "VIBE_HARNESS_REVIEW_DIFF",
                "VIBE_HARNESS_WORKER_RESUME"):
        row = next(line for line in text.splitlines() if f"`{var}`" in line)
        assert "| `1` |" in row, row


def test_how_it_works_documents_steering_sleep_and_accept_all():
    text = (DOCS / "how-it-works.md").read_text().lower()
    assert "steering" in text
    assert "sleep guard" in text and "idle-sleep inhibitor" in text
    assert "accept all" in text


def test_mcp_doc_is_the_overview():
    """Purpose, both attach paths, the mailbox alongside, and the full reference."""
    text = (DOCS / "mcp.md").read_text()
    for needle in ("MCPTOOLS.md", "VIBE_MCP_SERVERS", "--mcp-config",
                   "harness-mailbox", "get_transcript", "list_transcripts",
                   "steering"):
        assert needle in text, needle


def test_projects_doc_documents_the_setup_and_the_transcript_store():
    text = (DOCS / "projects.md").read_text()
    for needle in ("agent.md", "route.md", "git init", ".vibe-harness"):
        assert needle in text, needle


def test_route_md_documents_syntax_highlighting():
    assert "syntax highlighting" in ROUTE.read_text().lower()
