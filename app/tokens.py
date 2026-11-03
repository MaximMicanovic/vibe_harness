"""One definition of what a card cost.

`session.tokens` is the WORKER's export total. It is already cumulative across
resumed fixer rounds, so it is added once and never summed per round. Review
rounds write their own number to `session.reviews[]` (and `session.review` for
cards written before the list existed); merge-conflict resolver rounds write
theirs to `session.resolver`. A card's real cost is the sum of all three.

The same shape holds for money: every round's export carries `cost_usd` (the
Claude backend maps Anthropic's `total_cost_usd` onto it in
`backends.normalize_export`), the worker's lands on `session.cost_usd` and
each review/resolver round records its own. `card_cost_usd` derives the
card's total from those per-round numbers, exactly like `card_tokens`.

Deriving the total here — instead of changing what `session.tokens` means —
keeps every card written before this helper existed readable, and gives the
board, `/api/stats`, `/api/split-report` and the per-card chip one number to
agree on.
"""
from __future__ import annotations

# Token-type keys of a breakdown dict, in display order (cached, output, input).
TOKEN_BREAKDOWN_KEYS = ("cached", "output", "input")


def _num(value) -> int | None:
    """A finite int for numeric values (bool excluded), else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    return None


def _cost(value) -> float | None:
    """A finite float for numeric values (bool excluded), else None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def export_cost_usd(export) -> float | None:
    """One round export's USD cost (the normalized `cost_usd` key), or None."""
    if not isinstance(export, dict):
        return None
    return _cost(export.get("cost_usd"))


def _round_cost(record) -> float | None:
    """One round record's USD cost (None when it has none)."""
    if not isinstance(record, dict):
        return None
    return _cost(record.get("cost_usd"))


def _round_tokens(record) -> int:
    """One round record's token count (0 when it has none)."""
    if not isinstance(record, dict):
        return 0
    return _num(record.get("tokens")) or 0


def _rounds(session: dict, list_key: str, single_key: str) -> list[dict]:
    """Every round record of one kind.

    The list is authoritative when present; the single record is the fallback
    for cards written before the list existed (and for a test-gate rejection,
    which records a single `review` without a review session).
    """
    records = session.get(list_key)
    if isinstance(records, list) and records:
        return [r for r in records if isinstance(r, dict)]
    single = session.get(single_key)
    return [single] if isinstance(single, dict) else []


def card_tokens(session: dict | None) -> int:
    """The tokens the card actually cost: worker + review + resolver rounds.

    `session.live_high_water` is the last live total shown for the card; it
    floors the result so the displayed number cannot dip at a round boundary
    (between rounds the live counter is gone until the next export lands).
    """
    if not isinstance(session, dict):
        return 0
    total = _num(session.get("tokens")) or 0
    total += sum(_round_tokens(r) for r in _rounds(session, "reviews", "review"))
    total += sum(_round_tokens(r) for r in _rounds(session, "resolvers", "resolver"))
    return max(total, _num(session.get("live_high_water")) or 0)


def card_token_breakdown(session: dict | None) -> dict | None:
    """Per-type breakdown (cached/output/input) of the card total.

    Sums the worker's export breakdown and every review and resolver round's
    breakdown per type, so the hover tooltip adds up to the total it describes.
    A type no round reported stays None (unknown, not zero); None when no round
    knows any type.
    """
    if not isinstance(session, dict):
        return None
    records = [session,
               *_rounds(session, "reviews", "review"),
               *_rounds(session, "resolvers", "resolver")]
    out: dict[str, int | None] = {}
    for key in TOKEN_BREAKDOWN_KEYS:
        values = []
        for record in records:
            breakdown = record.get("token_breakdown")
            if isinstance(breakdown, dict):
                values.append(_num(breakdown.get(key)))
        known = [v for v in values if v is not None]
        out[key] = sum(known) if known else None
    return out if any(v is not None for v in out.values()) else None


def card_cost_usd(session: dict | None) -> float | None:
    """The USD the card actually cost: worker + review + resolver rounds.

    Mirrors `card_tokens`: `session.cost_usd` is the worker's export total
    (cumulative across resumed fixer rounds, so added once), review rounds
    write theirs to `session.reviews[]` / `session.review`, resolver rounds to
    `session.resolvers[]` / `session.resolver`. None when no round ever
    reported a cost (unknown, not zero); the sum is rounded to 6 decimals so
    binary float noise never shows up as money.
    """
    if not isinstance(session, dict):
        return None
    values = [_cost(session.get("cost_usd"))]
    values += [_round_cost(r) for r in _rounds(session, "reviews", "review")]
    values += [_round_cost(r) for r in _rounds(session, "resolvers", "resolver")]
    known = [v for v in values if v is not None]
    if not known:
        return None
    return round(sum(known), 6)


def combine_live(recorded: int, live: dict) -> int:
    """The displayed total while a session is live.

    A resumed round's journal counter is cumulative (it already includes the
    earlier rounds), so it is maxed with the recorded total; a fresh round's
    counter is added to it.
    """
    tokens = _num(live.get("tokens")) or 0
    if live.get("cumulative"):
        return max(tokens, recorded)
    return recorded + tokens
