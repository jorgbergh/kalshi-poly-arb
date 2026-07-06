"""Order-book depth walking (plan §7, milestone 7).

One pure function used for BOTH platforms (flagged deviation from plan §7's
"prefer py-clob-client's calculate_market_price for Polymarket": Kalshi needs
a hand-rolled walker regardless, one tested implementation beats two code
paths, and the books are already in memory — an SDK call would add a
dependency plus a network round trip per evaluation).

Partial-fill realism (plan §7): when depth runs out before the target size,
the returned Fill reports the maximum achievable size and the cost at that
size — the caller compares ``fill.size`` against what it asked for.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from arbdetector.schema import OrderBookLevel

_ZERO = Decimal("0")


@dataclass(frozen=True)
class Fill:
    """Result of walking one ask book for up to a target number of shares."""

    size: Decimal       # shares filled; < requested means partial (or 0: empty book)
    cost: Decimal       # total dollars paid across the consumed levels
    avg_price: Decimal  # size-weighted fill price; 0 when nothing filled

    @property
    def is_empty(self) -> bool:
        return self.size == 0


def walk_book(levels: Sequence[OrderBookLevel], target_size: Decimal) -> Fill:
    """Walk an ask book (best/cheapest level first) for up to ``target_size``.

    The book must be sorted best-first — both platform adapters guarantee
    this; an unsorted book raises rather than silently mispricing.
    """
    if not isinstance(target_size, Decimal):
        raise TypeError(
            f"target_size must be Decimal, got {type(target_size).__name__} "
            f"— never use float for sizes (plan §4)"
        )
    if target_size <= 0:
        raise ValueError(f"target_size must be > 0, got {target_size}")

    filled = _ZERO
    cost = _ZERO
    previous_price: Decimal | None = None
    for level in levels:
        if previous_price is not None and level.price < previous_price:
            raise ValueError(
                f"book not sorted best-first ({level.price} after {previous_price}) "
                f"— refusing to walk a malformed book"
            )
        previous_price = level.price
        take = min(level.size, target_size - filled)
        if take <= 0:
            break
        filled += take
        cost += take * level.price
        if filled == target_size:
            break

    avg_price = (cost / filled) if filled > 0 else _ZERO
    return Fill(size=filled, cost=cost, avg_price=avg_price)
