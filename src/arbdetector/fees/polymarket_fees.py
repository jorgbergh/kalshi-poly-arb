"""Polymarket taker-fee model, post-2026 international schedule (plan §3.2).

    fee = n_shares * feeRate * P * (1 - P)                                # USDC

- Makers are never charged; only takers. v1 conservatively assumes taker
  fills on both legs.
- ``feeRate`` is category-specific (table below); **geopolitics = 0.00**,
  which is the whole reason v1 targets it (plan §3.3).
- Fees are rounded to 5 decimal places. ASSUMPTION: the public schedule does
  not name a rounding mode, so ``ROUND_HALF_UP`` is used; at 5 dp the
  difference is <= $0.000005/order and cannot flip a signal.
- Minimum charge of 0.00001 USDC applies only to fee-bearing trades
  (``feeRate > 0`` and a nonzero raw fee). A zero rate short-circuits to
  exactly ``Decimal("0")`` — no minimum, no rounding.
- The separate US (QCX) flat-fee schedule (0.05 taker / -0.0125 maker rebate)
  is NOT modeled; it would be a different FeeModel plugged into the same
  registry (plan §3.2).
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from arbdetector.fees.base import validate_price_size
from arbdetector.schema import FeeModel, Platform

_FIVE_DP = Decimal("0.00001")

# Plan §3.2 category table. Config (fees.polymarket_fee_rates) overrides these
# for the registry; this module-level table is the schedule of record.
DEFAULT_FEE_RATES: dict[str, Decimal] = {
    "crypto": Decimal("0.07"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "other": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "mentions": Decimal("0.04"),
    "sports": Decimal("0.03"),
    "geopolitics": Decimal("0.00"),
}


def polymarket_taker_fee(price: Decimal, size: Decimal, fee_rate: Decimal) -> Decimal:
    """USDC taker fee for buying ``size`` shares at ``price`` under ``fee_rate``."""
    validate_price_size(price, size)
    if not isinstance(fee_rate, Decimal):
        raise TypeError(f"fee_rate must be Decimal, got {type(fee_rate).__name__}")
    if fee_rate < 0:
        raise ValueError(f"fee_rate must be >= 0, got {fee_rate}")
    if fee_rate == 0:
        return Decimal("0")
    raw = size * fee_rate * price * (Decimal(1) - price)
    if raw == 0:
        return Decimal("0")
    fee = raw.quantize(_FIVE_DP, rounding=ROUND_HALF_UP)
    return max(fee, _FIVE_DP)


def make_polymarket_fee_model(category: str, fee_rate: Decimal | None = None) -> FeeModel:
    """Bind a category's rate into a :class:`FeeModel`.

    With ``fee_rate=None`` the rate comes from :data:`DEFAULT_FEE_RATES`;
    an unknown category raises ``KeyError`` — never guess a fee schedule.
    """
    rate = DEFAULT_FEE_RATES[category.lower()] if fee_rate is None else fee_rate

    def fee_fn(price: Decimal, size: Decimal) -> Decimal:
        return polymarket_taker_fee(price, size, rate)

    return FeeModel(platform=Platform.POLYMARKET, category=category, fee_fn=fee_fn)
