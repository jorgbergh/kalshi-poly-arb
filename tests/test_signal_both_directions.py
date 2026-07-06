"""Walked §3.4 pricing, both directions; price/threshold stage accounting.

Numbers are hand-computed in the comments (plan §7, §12).
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from arbdetector.config import load_config
from arbdetector.engine.signal import (
    PairBooks,
    dump_recordings,
    evaluate_pair,
    load_recordings,
    matched_pair_id,
    opportunity_id,
    quote_direction,
    replay_fetcher,
    run_price,
    run_threshold,
)
from arbdetector.fees import build_fee_registry
from arbdetector.schema import Direction, MatchedPair, NormalizedMarket, OrderBookLevel, Platform
from arbdetector.tracking import DropReason
from tests.conftest import CONFIG_PATH

D = Decimal
NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


def lvl(price: str, size: str) -> OrderBookLevel:
    return OrderBookLevel(price=D(price), size=D(size))


def make_market(platform: Platform, market_id: str, category: str) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        market_id=market_id,
        yes_token_id=None if platform is Platform.KALSHI else "yes-token",
        no_token_id=None if platform is Platform.KALSHI else "no-token",
        title="Will X happen?",
        category=category,
        resolution_criteria="rules",
        resolution_source=None,
        close_time="2026-12-31T00:00:00Z",
        yes_ask=[],
        no_ask=[],
        raw={},
    )


def make_pair(n: int = 1, same_direction: bool = True) -> MatchedPair:
    return MatchedPair(
        kalshi=make_market(Platform.KALSHI, f"KX-{n}", "World"),
        polymarket=make_market(Platform.POLYMARKET, f"0x{n}", "geopolitics"),
        is_same_event=True,
        confidence=0.9,
        same_direction=same_direction,
        resolution_caveats="",
        verdict_ts="2026-07-06T00:00:00+00:00",
        rules_hash="abcd1234abcd1234",
    )


def books(kalshi_yes=(), kalshi_no=(), poly_yes=(), poly_no=(), age_sec: float = 0) -> PairBooks:
    return PairBooks(
        kalshi_yes_ask=list(kalshi_yes),
        kalshi_no_ask=list(kalshi_no),
        poly_yes_ask=list(poly_yes),
        poly_no_ask=list(poly_no),
        fetched_at=NOW - timedelta(seconds=age_sec),
    )


@pytest.fixture(scope="module")
def registry():
    return build_fee_registry(load_config(CONFIG_PATH).fees)


class TestWalkedQuote:
    def test_multi_level_hand_computed(self, registry):
        # K yes: 100@0.13 + 100@0.15 -> 200 filled, cost 28.00, avg 0.14
        # P no:  120@0.84 + 80@0.86  -> 200 filled, cost 169.60, avg 0.848
        # gross = 200 - 28 - 169.60 = 2.40
        # kalshi fee @0.14 x200: 0.07*0.14*0.86*200 = 1.6856 -> ceil $1.69; poly 0
        # net = 0.71 ; net/pair = 0.00355
        kalshi = make_market(Platform.KALSHI, "K1", "World")
        poly = make_market(Platform.POLYMARKET, "0x1", "geopolitics")
        kalshi.yes_ask = [lvl("0.13", "100"), lvl("0.15", "100")]
        poly.no_ask = [lvl("0.84", "120"), lvl("0.86", "80")]
        q = quote_direction(
            direction=Direction.YES_KALSHI_NO_POLY,
            yes_market=kalshi,
            no_market=poly,
            target_size=D("500"),
            fee_registry=registry,
        )
        assert q.size == D("200")  # partial: book depth < 500
        assert q.fill_yes == D("0.14")
        assert q.fill_no == D("0.848")
        assert q.fee_yes == D("1.69") and q.fee_no == D("0")
        assert q.net_total == D("0.71")
        assert q.net_per_pair == D("0.00355")

    def test_deeper_leg_rewalked_at_achievable_size(self, registry):
        # poly depth caps the pair at 50; kalshi must be priced at its
        # FIRST level only (0.13), not the 200-deep walk average
        kalshi = make_market(Platform.KALSHI, "K1", "World")
        poly = make_market(Platform.POLYMARKET, "0x1", "geopolitics")
        kalshi.yes_ask = [lvl("0.13", "100"), lvl("0.15", "100")]
        poly.no_ask = [lvl("0.84", "50")]
        q = quote_direction(
            direction=Direction.YES_KALSHI_NO_POLY,
            yes_market=kalshi,
            no_market=poly,
            target_size=D("500"),
            fee_registry=registry,
        )
        assert q.size == D("50")
        assert q.fill_yes == D("0.13")  # would be blended if not re-walked
        # gross = 50 - 6.50 - 42.00 = 1.50 ; kalshi fee 0.40 ; net 1.10
        assert q.net_total == D("1.10")
        assert q.net_per_pair == D("0.022")

    def test_empty_book_returns_none(self, registry):
        kalshi = make_market(Platform.KALSHI, "K1", "World")
        poly = make_market(Platform.POLYMARKET, "0x1", "geopolitics")
        kalshi.yes_ask = [lvl("0.13", "100")]
        assert (
            quote_direction(
                direction=Direction.YES_KALSHI_NO_POLY,
                yes_market=kalshi,
                no_market=poly,
                target_size=D("500"),
                fee_registry=registry,
            )
            is None
        )


class TestEvaluatePair:
    def test_both_directions_best_net_first(self, registry):
        kalshi = make_market(Platform.KALSHI, "K1", "World")
        poly = make_market(Platform.POLYMARKET, "0x1", "geopolitics")
        kalshi.yes_ask = [lvl("0.13", "100")]
        kalshi.no_ask = [lvl("0.88", "80")]
        poly.yes_ask = [lvl("0.11", "60")]
        poly.no_ask = [lvl("0.84", "50")]
        quotes = evaluate_pair(kalshi, poly, target_size=D("500"), fee_registry=registry)
        assert [q.direction for q in quotes] == [
            Direction.YES_KALSHI_NO_POLY,
            Direction.NO_KALSHI_YES_POLY,
        ]
        assert quotes[0].net_per_pair > quotes[1].net_per_pair

    def test_platform_arguments_enforced(self, registry):
        poly = make_market(Platform.POLYMARKET, "0x1", "geopolitics")
        with pytest.raises(ValueError, match="expected a kalshi market"):
            evaluate_pair(poly, poly, target_size=D("500"), fee_registry=registry)


GOOD_BOOKS = dict(
    kalshi_yes=[lvl("0.13", "100"), lvl("0.15", "100")],
    kalshi_no=[lvl("0.88", "80")],
    poly_yes=[lvl("0.11", "60")],
    poly_no=[lvl("0.84", "120"), lvl("0.86", "80")],
)


class TestRunPrice:
    def test_drop_accounting_across_reasons(self, registry):
        pairs = [make_pair(n) for n in range(1, 5)]
        by_id = {
            matched_pair_id(pairs[0]): books(**GOOD_BOOKS),                      # priced
            matched_pair_id(pairs[1]): books(age_sec=120),                       # stale + empty
            matched_pair_id(pairs[2]): books(),                                  # empty books
            # pairs[3] missing -> fetch raises -> API_ERROR
        }
        priced, result = run_price(
            pairs,
            fetch_books=replay_fetcher(by_id),
            target_size=D("500"),
            min_size=D("1"),
            max_book_age_sec=30.0,
            fee_registry=registry,
            now=NOW,
        )
        assert len(priced) == 1 and priced[0][0] is pairs[0]
        assert result.n_in == 4 and result.n_out == 1
        assert result.drops == {
            DropReason.STALE_BOOK: 1,
            DropReason.EMPTY_BOOK: 1,
            DropReason.API_ERROR: 1,
        }

    def test_insufficient_depth(self, registry):
        pair = make_pair()
        thin = books(
            kalshi_yes=[lvl("0.13", "0.5")],  # half a share on every book
            kalshi_no=[lvl("0.88", "0.5")],
            poly_yes=[lvl("0.11", "0.5")],
            poly_no=[lvl("0.84", "0.5")],
        )
        priced, result = run_price(
            [pair],
            fetch_books=lambda _: thin,
            target_size=D("500"),
            min_size=D("1"),
            max_book_age_sec=30.0,
            fee_registry=registry,
            now=NOW,
        )
        assert priced == []
        assert result.drops == {DropReason.INSUFFICIENT_DEPTH: 1}

    def test_inverted_pair_books_swapped_into_kalshi_frame(self, registry):
        # poly market has ONLY a yes book; on an inverted pair that becomes
        # the kalshi-frame NO book, so only YES@kalshi+NO@poly is quotable
        pair = make_pair(same_direction=False)
        raw = books(
            kalshi_yes=[lvl("0.13", "100")],
            kalshi_no=[lvl("0.88", "80")],
            poly_yes=[lvl("0.84", "50")],
            poly_no=[],
        )
        priced, _ = run_price(
            [pair],
            fetch_books=lambda _: raw,
            target_size=D("500"),
            min_size=D("1"),
            max_book_age_sec=30.0,
            fee_registry=registry,
            now=NOW,
        )
        assert len(priced) == 1
        assert priced[0][1].direction is Direction.YES_KALSHI_NO_POLY
        assert priced[0][1].fill_no == D("0.84")  # poly's raw YES book, swapped


class TestRunThreshold:
    def make_priced(self, registry):
        pair = make_pair()
        raw = books(**GOOD_BOOKS)
        priced, _ = run_price(
            [pair],
            fetch_books=lambda _: raw,
            target_size=D("500"),
            min_size=D("1"),
            max_book_age_sec=30.0,
            fee_registry=registry,
            now=NOW,
        )
        return priced

    def test_above_threshold_becomes_opportunity(self, registry):
        priced = self.make_priced(registry)  # best quote net/pair = 0.00355
        opportunities, result = run_threshold(
            priced, threshold=D("0.002"), detected_ts="2026-07-06T12:00:00+00:00"
        )
        assert result.n_in == 1 and result.n_out == 1
        opp = opportunities[0]
        assert opp.net_per_pair == D("0.00355")
        assert opp.detected_ts == "2026-07-06T12:00:00+00:00"
        # deterministic §9.2 id: pair + direction + ts
        assert opportunity_id(opp) == opportunity_id(opp)
        assert len(opportunity_id(opp)) == 12

    def test_below_threshold_and_negative_margin_drop(self, registry):
        priced = self.make_priced(registry)
        _, result = run_threshold(priced, threshold=D("0.02"))
        assert result.drops == {DropReason.BELOW_THRESHOLD: 1}

        pair, quote = priced[0]
        negative = quote.__class__(**{**quote.__dict__, "net_per_pair": D("-0.01")})
        _, result = run_threshold([(pair, negative)], threshold=D("0.02"))
        assert result.drops == {DropReason.NEGATIVE_MARGIN: 1}

    def test_threshold_is_strictly_greater_than(self, registry):
        priced = self.make_priced(registry)
        _, result = run_threshold(priced, threshold=D("0.00355"))  # exactly equal
        assert result.drops == {DropReason.BELOW_THRESHOLD: 1}


class TestRecordings:
    def test_round_trip_bit_exact(self, tmp_path, registry):
        pair = make_pair()
        original = {matched_pair_id(pair): books(**GOOD_BOOKS)}
        path = tmp_path / "books.json"
        dump_recordings(original, path)
        loaded = load_recordings(path)
        assert loaded == original

        # replayed books price identically to the originals
        priced, result = run_price(
            [pair],
            fetch_books=replay_fetcher(loaded),
            target_size=D("500"),
            min_size=D("1"),
            max_book_age_sec=10_000_000,  # recording is old by wall clock; allow it
            fee_registry=registry,
        )
        assert result.n_out == 1
        assert priced[0][1].net_per_pair == D("0.00355")
