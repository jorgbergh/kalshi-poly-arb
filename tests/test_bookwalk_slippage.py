"""Depth-walking: exact fills, partial fills, insufficient depth (plan §7, §12)."""

from decimal import Decimal

import pytest

from arbdetector.engine.bookwalk import Fill, walk_book
from arbdetector.schema import OrderBookLevel

D = Decimal


def lvl(price: str, size: str) -> OrderBookLevel:
    return OrderBookLevel(price=D(price), size=D(size))


BOOK = [lvl("0.10", "100"), lvl("0.12", "50"), lvl("0.20", "200")]


class TestExactFills:
    def test_multi_level_weighted_fill(self):
        # 100@0.10 + 20@0.12 = $12.40 for 120 shares
        fill = walk_book(BOOK, D("120"))
        assert fill.size == D("120")
        assert fill.cost == D("12.40")
        assert fill.avg_price == D("12.40") / D("120")

    def test_fill_exactly_at_level_boundary(self):
        fill = walk_book(BOOK, D("150"))
        assert fill.size == D("150")
        assert fill.cost == D("16.00")  # 10 + 6

    def test_single_level_partial_take(self):
        fill = walk_book(BOOK, D("40"))
        assert fill.size == D("40")
        assert fill.cost == D("4.00")
        assert fill.avg_price == D("0.10")

    def test_cost_is_exact_decimal_arithmetic(self):
        fill = walk_book([lvl("0.0100", "19741.00")], D("19741.00"))
        assert fill.cost == D("0.0100") * D("19741.00")


class TestPartialFills:
    def test_depth_exhausted_reports_max_achievable(self):
        # book holds 350 total; asking for 500 fills 350 (plan §7 partial-fill realism)
        fill = walk_book(BOOK, D("500"))
        assert fill.size == D("350")
        assert fill.cost == D("10") + D("6") + D("40")  # 100@.10 + 50@.12 + 200@.20
        assert not fill.is_empty

    def test_empty_book_fills_nothing(self):
        fill = walk_book([], D("10"))
        assert fill == Fill(size=D("0"), cost=D("0"), avg_price=D("0"))
        assert fill.is_empty


class TestGuards:
    def test_unsorted_book_refused(self):
        # target must exceed the first level so the walk actually reaches
        # the out-of-order one — untouched levels are legitimately unchecked
        with pytest.raises(ValueError, match="not sorted"):
            walk_book([lvl("0.12", "10"), lvl("0.10", "10")], D("15"))

    def test_equal_price_levels_allowed(self):
        fill = walk_book([lvl("0.10", "10"), lvl("0.10", "10")], D("20"))
        assert fill.size == D("20")

    def test_zero_or_negative_target_refused(self):
        with pytest.raises(ValueError):
            walk_book(BOOK, D("0"))
        with pytest.raises(ValueError):
            walk_book(BOOK, D("-5"))

    def test_float_target_refused(self):
        with pytest.raises(TypeError, match="float"):
            walk_book(BOOK, 120.0)  # type: ignore[arg-type]
