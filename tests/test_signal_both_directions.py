"""Both §3.4 directions evaluated; better one first; fees per leg (plan §7).

Covers the top-of-book milestone-7 pull-forward in engine/signal.py.
Numbers are hand-computed in the comments.
"""

from decimal import Decimal

import pytest

from arbdetector.config import load_config
from arbdetector.engine.signal import evaluate_pair_top_of_book, quote_direction
from arbdetector.fees import build_fee_registry
from arbdetector.schema import Direction, NormalizedMarket, OrderBookLevel, Platform
from tests.conftest import CONFIG_PATH

D = Decimal


def lvl(price: str, size: str) -> OrderBookLevel:
    return OrderBookLevel(price=D(price), size=D(size))


def make_market(platform: Platform, yes_ask, no_ask, category: str) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        market_id="K1" if platform is Platform.KALSHI else "0xabc",
        yes_token_id=None if platform is Platform.KALSHI else "yes-token",
        no_token_id=None if platform is Platform.KALSHI else "no-token",
        title="Will X happen?",
        category=category,
        resolution_criteria="rules",
        resolution_source=None,
        close_time="2026-12-31T00:00:00Z",
        yes_ask=yes_ask,
        no_ask=no_ask,
        raw={},
    )


@pytest.fixture(scope="module")
def registry():
    return build_fee_registry(load_config(CONFIG_PATH).fees)


@pytest.fixture()
def kalshi():
    return make_market(
        Platform.KALSHI, [lvl("0.13", "100")], [lvl("0.88", "80")], "World"
    )


@pytest.fixture()
def poly():
    return make_market(
        Platform.POLYMARKET, [lvl("0.11", "60")], [lvl("0.84", "50")], "geopolitics"
    )


class TestQuoteDirection:
    def test_yes_kalshi_no_poly_hand_computed(self, kalshi, poly, registry):
        # size = min(500, 100, 50) = 50
        # gross/pair = 1 - 0.13 - 0.84 = 0.03 -> total 1.50
        # kalshi fee @0.13 x50: 0.07*0.13*0.87*50 = 0.39585 -> ceil -> $0.40
        # poly geopolitics fee = 0
        # net_total = 1.50 - 0.40 = 1.10 ; net/pair = 0.022
        # capital = 0.97*50 + 0.40 = 48.90 ; roi = 1.10/48.90 = 2.2495%
        q = quote_direction(
            direction=Direction.YES_KALSHI_NO_POLY,
            yes_market=kalshi,
            no_market=poly,
            target_size=D("500"),
            fee_registry=registry,
        )
        assert q.size == D("50")
        assert q.fill_yes == D("0.13") and q.fill_no == D("0.84")
        assert q.fee_yes == D("0.40") and q.fee_no == D("0")
        assert q.net_total == D("1.10")
        assert q.net_per_pair == D("0.022")
        assert q.roi_pct.quantize(D("0.0001")) == D("2.2495")

    def test_no_kalshi_yes_poly_hand_computed(self, kalshi, poly, registry):
        # YES leg on poly @0.11 x min(500,60,80)=60 ; NO leg on kalshi @0.88
        # gross/pair = 1 - 0.11 - 0.88 = 0.01 -> total 0.60
        # kalshi fee @0.88 x60: 0.07*0.88*0.12*60 = 0.44352 -> ceil -> $0.45
        # net_total = 0.60 - 0.45 = 0.15 ; net/pair = 0.0025
        q = quote_direction(
            direction=Direction.NO_KALSHI_YES_POLY,
            yes_market=poly,
            no_market=kalshi,
            target_size=D("500"),
            fee_registry=registry,
        )
        assert q.size == D("60")
        assert q.fill_yes == D("0.11") and q.fill_no == D("0.88")
        assert q.fee_yes == D("0") and q.fee_no == D("0.45")
        assert q.net_per_pair == D("0.0025")

    def test_target_size_caps_below_book(self, kalshi, poly, registry):
        q = quote_direction(
            direction=Direction.YES_KALSHI_NO_POLY,
            yes_market=kalshi,
            no_market=poly,
            target_size=D("30"),
            fee_registry=registry,
        )
        assert q.size == D("30")

    def test_empty_book_returns_none(self, kalshi, poly, registry):
        poly.no_ask = []
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

    def test_negative_margin_reported_honestly(self, kalshi, poly, registry):
        # thresholding is a later stage; the quote itself must not hide losses
        poly.no_ask = [lvl("0.90", "50")]  # gross/pair = 1-0.13-0.90 = -0.03
        q = quote_direction(
            direction=Direction.YES_KALSHI_NO_POLY,
            yes_market=kalshi,
            no_market=poly,
            target_size=D("500"),
            fee_registry=registry,
        )
        assert q.net_per_pair < 0
        assert q.roi_pct < 0


class TestEvaluatePair:
    def test_both_directions_best_first(self, kalshi, poly, registry):
        quotes = evaluate_pair_top_of_book(
            kalshi, poly, target_size=D("500"), fee_registry=registry
        )
        assert [q.direction for q in quotes] == [
            Direction.YES_KALSHI_NO_POLY,   # net/pair 0.022
            Direction.NO_KALSHI_YES_POLY,   # net/pair 0.0025
        ]
        assert quotes[0].net_per_pair > quotes[1].net_per_pair

    def test_empty_book_direction_omitted(self, kalshi, poly, registry):
        poly.no_ask = []  # kills YES@kalshi+NO@poly only
        quotes = evaluate_pair_top_of_book(
            kalshi, poly, target_size=D("500"), fee_registry=registry
        )
        assert [q.direction for q in quotes] == [Direction.NO_KALSHI_YES_POLY]

    def test_platform_arguments_enforced(self, kalshi, poly, registry):
        with pytest.raises(ValueError, match="expected a kalshi market"):
            evaluate_pair_top_of_book(
                poly, poly, target_size=D("500"), fee_registry=registry
            )
