"""YAML-frontmatter read/write for card and archive markdown files.

In-repo replacement for the python-frontmatter dependency: split a leading
`---` YAML block from the markdown body, and dump it back. The on-disk
format is byte-compatible with the files python-frontmatter wrote — sorted
YAML keys (PyYAML's default), lines folded at the default width, unicode
unescaped, no trailing newline after the body.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

try:  # libyaml when available, same as python-frontmatter
    from yaml import CSafeDumper as SafeDumper
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeDumper
    from yaml import SafeLoader

# A fence is a line of 3+ dashes — the same boundary regex as
# python-frontmatter's YAML handler.
_FENCE = re.compile(r"^-{3,}\s*$", re.MULTILINE)


def load(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read a markdown file into (metadata, body), UTF-8.

    A file without a closing fence — or without any fence at all — is all
    body with empty metadata; a fence whose YAML is not a mapping is
    treated the same way. Broken YAML raises (callers skip such files).
    Like python-frontmatter, surrounding whitespace is stripped from both
    the whole text and the body.
    """
    text = Path(path).read_text(encoding="utf-8").strip()
    parts = _FENCE.split(text, 2)
    if len(parts) != 3:
        return {}, text
    _, fm, body = parts
    metadata = yaml.load(fm, Loader=SafeLoader)
    if not isinstance(metadata, dict):
        return {}, body.strip()
    return metadata, body.strip()


def dumps(metadata: dict, body: str) -> str:
    """Serialize (metadata, body) to markdown with a `---` YAML frontmatter
    block — byte-compatible with python-frontmatter's dumps."""
    fm = yaml.dump(metadata, Dumper=SafeDumper, default_flow_style=False,
                   allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body}\n".strip()
