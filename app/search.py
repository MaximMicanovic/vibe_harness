"""Search: find a card by text, on the board and in the archives.

The board's cards are already in the browser (`GET /api/cards` carries every
body), so board search is client-side. Archives load per folder, so finding an
archived card by text needs a server-side query — this module is that query.

A linear scan is fine at this scale (a few hundred cards): no index, no
dependencies. Matching is case-insensitive over title, body and id; the snippet
is the matching body line (or the title, when only the title/id matched)
trimmed to ~120 chars around the match, with the match's offsets so the UI can
highlight it without re-searching.
"""
from __future__ import annotations

from app.archives import ArchiveStore
from app.cards import CardStore

# How much of the matching line a snippet carries, and how much of it is kept
# before the match (so the match is not glued to the left edge).
SNIPPET_WIDTH = 120
SNIPPET_LEAD = 40

# The most results one query returns; the route reports the uncapped total.
RESULT_LIMIT = 50

ELLIPSIS = "…"


def _snippet(text: str, needle: str) -> dict:
    """The matching line trimmed to ~120 chars, plus the match's offsets.

    `start`/`end` index into the returned `text`, so the client highlights
    `text[start:end]` without searching again.
    """
    idx = text.lower().find(needle)
    if idx < 0:
        # Only reachable when the match was in the id: nothing to highlight.
        return {"text": text[:SNIPPET_WIDTH], "start": 0, "end": 0}
    start = max(0, idx - SNIPPET_LEAD)
    end = min(len(text), start + SNIPPET_WIDTH)
    start = max(0, end - SNIPPET_WIDTH)  # keep the window full near the end
    prefix = ELLIPSIS if start > 0 else ""
    suffix = ELLIPSIS if end < len(text) else ""
    return {
        "text": prefix + text[start:end] + suffix,
        "start": len(prefix) + idx - start,
        "end": len(prefix) + idx - start + len(needle),
    }


def _matching_line(body: str, needle: str) -> str | None:
    """The first line of the body containing the needle (case-insensitive)."""
    for line in (body or "").splitlines():
        if needle in line.lower():
            return line.strip()
    return None


def _hit(card: dict, where: str, needle: str) -> dict | None:
    """One search result for a card, or None when it does not match."""
    title = card.get("title") or ""
    body = card.get("body") or ""
    card_id = card.get("id") or ""
    if (needle not in title.lower() and needle not in body.lower()
            and needle not in card_id.lower()):
        return None
    line = _matching_line(body, needle)
    # A card that matched only on its title or id still gets a snippet: its
    # title, so every result row has something to show.
    snippet = _snippet(line if line is not None else title, needle)
    return {
        "id": card_id,
        "title": title,
        "status": card.get("status"),
        "where": where,
        "snippet": snippet,
    }


def archive_where(archive: dict) -> str:
    """How the board names an archive's location: "<project> / <name>".

    An archive filed under no project is named by its own name alone.
    """
    project = (archive.get("project") or "").strip()
    name = (archive.get("name") or "").strip()
    return f"{project} / {name}" if project else name


def search_cards(q: str, cards: CardStore, archives: ArchiveStore,
                 limit: int = RESULT_LIMIT) -> tuple[list[dict], int]:
    """Search the board and every archive. Returns (results, total matches).

    `results` is capped at `limit`; `total` is the uncapped count, so the
    caller can say when the list was cut short. Unreadable card files are
    skipped by the stores, never fatal.
    """
    needle = (q or "").strip().lower()
    if not needle:
        return [], 0
    results: list[dict] = []
    for card in cards.list():
        hit = _hit(card, "board", needle)
        if hit is not None:
            results.append(hit)
    for archive, items in archives.scan():
        where = archive_where(archive)
        for card in items:
            hit = _hit(card, where, needle)
            if hit is not None:
                results.append(hit)
    return results[:limit], len(results)
