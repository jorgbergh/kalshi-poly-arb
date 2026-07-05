"""Fee models (plan §3): per-platform, per-category taker-fee curves.

Fees are first-class, pluggable objects keyed by ``(platform, category)``
(plan §4). The engine only ever calls ``FeeModel.fee_fn(price, size)`` —
it never hard-codes a formula, so schedule changes are config edits.
"""

from arbdetector.fees.base import FeeRegistry, build_fee_registry, validate_price_size
from arbdetector.fees.kalshi_fees import (
    DEFAULT_KALSHI_MULTIPLIER,
    kalshi_taker_fee,
    make_kalshi_fee_model,
)
from arbdetector.fees.polymarket_fees import (
    DEFAULT_FEE_RATES,
    make_polymarket_fee_model,
    polymarket_taker_fee,
)

__all__ = [
    "DEFAULT_FEE_RATES",
    "DEFAULT_KALSHI_MULTIPLIER",
    "FeeRegistry",
    "build_fee_registry",
    "kalshi_taker_fee",
    "make_kalshi_fee_model",
    "make_polymarket_fee_model",
    "polymarket_taker_fee",
    "validate_price_size",
]
