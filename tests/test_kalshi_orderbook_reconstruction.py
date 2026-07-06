"""The NO-bid -> YES-ask reconstruction invariant (plan §2.1, §13).

Kalshi returns only bids for both sides; every ask book is derived. Getting
this wrong makes every computed spread meaningless, so the derivation lives
in one dedicated function and is tested here in isolation.
"""

from decimal import Decimal

import pytest

from arbdetector.clients.kalshi import (
    derive_ask_book,
    parse_bid_levels,
    parse_orderbook_response,
)
from arbdetector.schema import OrderBookLevel

D = Decimal


def lvl(price: str, size: str) -> OrderBookLevel:
    return OrderBookLevel(price=D(price), size=D(size))


class TestDeriveAskBook:
    def test_best_yes_ask_is_one_minus_best_no_bid(self):
        no_bids = [lvl("0.55", "100"), lvl("0.54", "200"), lvl("0.50", "50")]
        yes_ask = derive_ask_book(no_bids)
        # the BEST (highest) NO bid, 0.55, yields the BEST (lowest) YES ask
        assert yes_ask[0] == lvl("0.45", "100")

    def test_result_sorted_best_first_regardless_of_input_order(self):
        shuffled = [lvl("0.50", "50"), lvl("0.55", "100"), lvl("0.54", "200")]
        prices = [level.price for level in derive_ask_book(shuffled)]
        assert prices == [D("0.45"), D("0.46"), D("0.50")]

    def test_per_level_sum_invariant_and_size_preservation(self):
        # every derived ask pairs with exactly one source bid: prices sum to 1,
        # sizes carry over unchanged
        bids = [lvl("0.5500", "100"), lvl("0.5400", "200"), lvl("0.0100", "19741.00")]
        asks = derive_ask_book(bids)
        assert {(D(1) - a.price, a.size) for a in asks} == {(b.price, b.size) for b in bids}

    def test_involution_round_trips(self):
        bids = sorted([lvl("0.13", "7"), lvl("0.87", "3"), lvl("0.5", "1")],
                      key=lambda l: l.price)
        assert derive_ask_book(derive_ask_book(bids)) == bids

    def test_decimal_exactness(self):
        # "0.5500" -> exactly 0.4500; a float path would leave residue
        assert derive_ask_book([lvl("0.5500", "100")])[0].price == D("0.4500")

    def test_empty_book(self):
        assert derive_ask_book([]) == []

    def test_boundary_prices_allowed(self):
        assert derive_ask_book([lvl("1", "5")])[0].price == D("0")
        assert derive_ask_book([lvl("0", "5")])[0].price == D("1")

    def test_cents_scale_price_rejected(self):
        # integer-cents payload (42 instead of 0.42) must fail loudly,
        # never be silently mis-scaled 100x
        with pytest.raises(ValueError, match="outside"):
            derive_ask_book([lvl("42", "10")])

    def test_negative_size_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            derive_ask_book([lvl("0.42", "-10")])


class TestParseBidLevels:
    def test_dollar_strings_parse_exactly(self):
        levels = parse_bid_levels([["0.0100", "19741.00"], ["0.0200", "7660.00"]])
        assert levels == [lvl("0.0100", "19741.00"), lvl("0.0200", "7660.00")]

    def test_none_and_empty_are_empty(self):
        assert parse_bid_levels(None) == []
        assert parse_bid_levels([]) == []

    def test_float_rejected(self):
        with pytest.raises(TypeError, match="float"):
            parse_bid_levels([[0.01, "100"]])

    def test_cents_integer_rejected(self):
        with pytest.raises(ValueError, match="integer-cents"):
            parse_bid_levels([[42, "100"]])

    def test_garbage_rejected(self):
        with pytest.raises(ValueError):
            parse_bid_levels([["not-a-price", "100"]])
        with pytest.raises(ValueError, match="malformed"):
            parse_bid_levels([["0.42"]])


class TestParseOrderbookResponse:
    # exact live shape captured from the real API on 2026-07-05
    PAYLOAD = {
        "orderbook_fp": {
            "yes_dollars": [["0.4000", "150.00"]],
            "no_dollars": [["0.5500", "100.00"], ["0.5400", "200.00"]],
        }
    }

    def test_yes_ask_derived_from_no_bids(self):
        yes_ask, _ = parse_orderbook_response(self.PAYLOAD)
        assert yes_ask == [lvl("0.4500", "100.00"), lvl("0.4600", "200.00")]

    def test_no_ask_derived_from_yes_bids(self):
        _, no_ask = parse_orderbook_response(self.PAYLOAD)
        assert no_ask == [lvl("0.6000", "150.00")]

    def test_empty_sides(self):
        yes_ask, no_ask = parse_orderbook_response(
            {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
        )
        assert yes_ask == [] and no_ask == []

    def test_missing_book_key_fails_loudly(self):
        with pytest.raises(ValueError, match="orderbook_fp"):
            parse_orderbook_response({"orderbook": {"yes": [[42, 100]]}})
