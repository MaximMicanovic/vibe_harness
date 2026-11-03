"""One definition of what a card cost (app/tokens.py).

`session.tokens` is the WORKER's export total — cumulative across resumed fixer
rounds, so it must never be summed per round. Review rounds write their own
number to `session.reviews[]` / `session.review`, resolver rounds to
`session.resolver`, and the card's real cost is the sum of all three. These
tests pin that derivation, the per-type breakdown that rides along with it, and
the live combination used while a session runs.
"""
from __future__ import annotations

from app.tokens import (
    card_cost_usd,
    card_token_breakdown,
    card_tokens,
    combine_live,
    export_cost_usd,
)


# --------------------------------------------------------------- card_tokens

def test_worker_only():
    assert card_tokens({"tokens": 1000}) == 1000


def test_empty_or_missing_session_is_zero():
    assert card_tokens(None) == 0
    assert card_tokens({}) == 0
    assert card_tokens({"tokens": None}) == 0


def test_one_review_round_is_added_to_the_worker_total():
    # The bug: a card that ran 1 worker round + 1 review round reported only
    # the worker's export total.
    session = {"tokens": 1000,
               "reviews": [{"round": 1, "tokens": 250}]}
    assert card_tokens(session) == 1250


def test_two_review_rounds_are_both_counted():
    session = {"tokens": 1000,
               "reviews": [{"round": 1, "tokens": 250},
                           {"round": 2, "tokens": 125}]}
    assert card_tokens(session) == 1375


def test_legacy_card_without_a_reviews_list_uses_the_single_review_record():
    # Cards written before session.reviews existed keep only the last record.
    session = {"tokens": 1000, "review": {"round": 1, "tokens": 250}}
    assert card_tokens(session) == 1250


def test_the_reviews_list_wins_over_the_single_record():
    # session.review is the LAST round and is also inside session.reviews:
    # counting both would double-count it.
    session = {"tokens": 1000,
               "review": {"round": 2, "tokens": 125},
               "reviews": [{"round": 1, "tokens": 250},
                           {"round": 2, "tokens": 125}]}
    assert card_tokens(session) == 1375


def test_resolver_tokens_are_counted():
    session = {"tokens": 1000,
               "resolver": {"round": 1, "exit_code": 0, "tokens": 400}}
    assert card_tokens(session) == 1400


def test_resolver_list_is_summed():
    session = {"tokens": 1000,
               "resolvers": [{"round": 1, "tokens": 400},
                             {"round": 2, "tokens": 100}]}
    assert card_tokens(session) == 1500


def test_worker_review_and_resolver_all_add_up():
    session = {"tokens": 1000,
               "reviews": [{"round": 1, "tokens": 250},
                           {"round": 2, "tokens": 125}],
               "resolver": {"round": 1, "tokens": 400}}
    assert card_tokens(session) == 1775


def test_missing_and_non_numeric_round_tokens_are_ignored():
    session = {"tokens": 1000,
               "reviews": [{"round": 1}, {"round": 2, "tokens": None},
                           {"round": 3, "tokens": "nope"}, {"round": 4, "tokens": 50}],
               "resolver": {"round": 1, "tokens": True}}
    assert card_tokens(session) == 1050


def test_a_test_gate_rejection_record_without_tokens_adds_nothing():
    # The test gate writes a review record with tokens=None (no review session
    # ran): it must not break the sum.
    session = {"tokens": 1000,
               "reviews": [{"round": 1, "source": "test", "tokens": None}]}
    assert card_tokens(session) == 1000


def test_the_total_never_drops_below_the_last_live_value():
    # Between rounds the live counter is gone; the last value shown is kept as
    # a floor so the display cannot dip at a round boundary.
    session = {"tokens": 1000, "live_high_water": 1500}
    assert card_tokens(session) == 1500
    # Once the recorded rounds catch up, the floor stops mattering.
    session = {"tokens": 1000, "reviews": [{"tokens": 800}],
               "live_high_water": 1500}
    assert card_tokens(session) == 1800


# ------------------------------------------------------- card_token_breakdown

def test_breakdown_sums_every_round_per_type():
    session = {
        "token_breakdown": {"cached": 10, "output": 20, "input": 70},
        "reviews": [{"token_breakdown": {"cached": 1, "output": 2, "input": 7}}],
        "resolver": {"token_breakdown": {"cached": 3, "output": 4, "input": 5}},
    }
    assert card_token_breakdown(session) == {
        "cached": 14, "output": 26, "input": 82}


def test_breakdown_keeps_unknown_types_unknown():
    session = {"token_breakdown": {"cached": None, "output": 20, "input": 70},
               "reviews": [{"token_breakdown": {"cached": 5, "output": None,
                                                "input": None}}]}
    assert card_token_breakdown(session) == {
        "cached": 5, "output": 20, "input": 70}


def test_breakdown_is_none_when_no_round_reports_one():
    assert card_token_breakdown({"tokens": 1000}) is None
    assert card_token_breakdown(None) is None


def test_breakdown_ignores_a_malformed_breakdown():
    session = {"token_breakdown": "nope",
               "reviews": [{"token_breakdown": {"input": 5}}]}
    assert card_token_breakdown(session) == {
        "cached": None, "output": None, "input": 5}


# ------------------------------------------------------------ card_cost_usd

def test_cost_worker_only():
    assert card_cost_usd({"cost_usd": 0.25}) == 0.25


def test_cost_is_none_when_no_round_reports_one():
    # Unknown, not zero: a card whose rounds never exported a cost has no
    # cost number at all (the board shows no cost chip).
    assert card_cost_usd(None) is None
    assert card_cost_usd({}) is None
    assert card_cost_usd({"tokens": 1000}) is None
    assert card_cost_usd({"cost_usd": None}) is None


def test_cost_sums_worker_review_and_resolver_rounds():
    session = {"cost_usd": 0.10,
               "reviews": [{"round": 1, "cost_usd": 0.02},
                           {"round": 2, "cost_usd": 0.01}],
               "resolver": {"round": 1, "cost_usd": 0.03}}
    assert card_cost_usd(session) == 0.16


def test_cost_legacy_card_without_a_reviews_list_uses_the_single_review_record():
    session = {"cost_usd": 0.10, "review": {"round": 1, "cost_usd": 0.02}}
    assert card_cost_usd(session) == 0.12


def test_cost_the_reviews_list_wins_over_the_single_record():
    # Counting both would double-count the last round.
    session = {"cost_usd": 0.10,
               "review": {"round": 2, "cost_usd": 0.01},
               "reviews": [{"round": 1, "cost_usd": 0.02},
                           {"round": 2, "cost_usd": 0.01}]}
    assert card_cost_usd(session) == 0.13


def test_cost_ignores_missing_and_non_numeric_round_costs():
    session = {"cost_usd": 0.10,
               "reviews": [{"round": 1}, {"round": 2, "cost_usd": None},
                           {"round": 3, "cost_usd": "nope"}, {"round": 4, "cost_usd": 0.05}],
               "resolver": {"round": 1, "cost_usd": True}}
    assert card_cost_usd(session) == 0.15


def test_cost_rounds_away_float_noise():
    # 0.1 + 0.2 is 0.30000000000000004 in binary floats; the aggregate is a
    # clean money number.
    assert card_cost_usd({"cost_usd": 0.1, "reviews": [{"cost_usd": 0.2}]}) == 0.3


# ----------------------------------------------------------- export_cost_usd

def test_export_cost_usd_reads_the_normalized_export_key():
    assert export_cost_usd({"cost_usd": 0.25}) == 0.25
    assert export_cost_usd({"cost_usd": 0}) == 0.0
    assert export_cost_usd({"usage": {"total_tokens": 42}}) is None
    assert export_cost_usd({"cost_usd": None}) is None
    assert export_cost_usd({"cost_usd": "0.25"}) is None
    assert export_cost_usd({"cost_usd": True}) is None
    assert export_cost_usd({}) is None


# ---------------------------------------------------------------- combine_live

def test_combine_live_adds_a_fresh_round_to_the_recorded_total():
    assert combine_live(1000, {"tokens": 300, "cumulative": False}) == 1300


def test_combine_live_keeps_a_resumed_round_cumulative():
    # A resumed worker's journal already includes earlier rounds.
    assert combine_live(1000, {"tokens": 800, "cumulative": True}) == 1000
    assert combine_live(1000, {"tokens": 1200, "cumulative": True}) == 1200
