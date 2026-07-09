"""Kalshi taker-fee model (plan §3.1).

    fee_per_order = ceil(multiplier * P * (1 - P) * n_contracts * 100) / 100   # dollars

- Bernoulli-variance curve: peaks at P = 0.50 ($0.0175/contract raw at the
  default 0.07 multiplier), symmetric in P <-> (1 - P), vanishing at the tails.
- The ceiling is applied ONCE PER ORDER, to the cent: a 1-contract order at
  P = 0.50 costs $0.02, not $0.0175. The per-contract peak is only observable
  on larger orders (100 contracts at 0.50 -> exactly $1.75).
- ``multiplier`` is configurable (config ``fees.kalshi_multiplier_default``);
  some special-event markets carry different multipliers, so it is a
  parameter, never a constant (plan §3.1).
- Maker fees (~25% of taker) are not modeled in v1: the detector assumes
  taker fills on both legs, which is the conservative choice.

Live fee-schedule check (2026-07-08): the general taker formula and 0.07
coefficient above were confirmed against Kalshi's published schedule (peak
1.75c/contract at P=0.50, maker = 25% of taker). CAVEAT: some Kalshi series
(notably sports and financial-index markets) use a different multiplier that
this default does not model — verify per-series before trusting a
non-standard category. Polymarket's schedule was verified exactly, incl.
geopolitics = 0. See fees/polymarket_fees.py.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

from arbdetector.fees.base import validate_price_size
from arbdetector.schema import FeeModel, Platform

DEFAULT_KALSHI_MULTIPLIER = Decimal("0.07")

_CENT = Decimal("0.01")


def kalshi_taker_fee(
    price: Decimal,
    size: Decimal,
    multiplier: Decimal = DEFAULT_KALSHI_MULTIPLIER,
) -> Decimal:
    """Dollar taker fee for one order of ``size`` contracts at ``price``.

    Implements ``ceil(multiplier * P * (1-P) * n * 100) / 100`` exactly, via
    Decimal quantization with ``ROUND_CEILING`` (identical for the always-
    non-negative values involved).
    """
    validate_price_size(price, size)
    if not isinstance(multiplier, Decimal):
        raise TypeError(f"multiplier must be Decimal, got {type(multiplier).__name__}")
    raw = multiplier * price * (Decimal(1) - price) * size
    return raw.quantize(_CENT, rounding=ROUND_CEILING)


def make_kalshi_fee_model(
    category: str,
    multiplier: Decimal = DEFAULT_KALSHI_MULTIPLIER,
) -> FeeModel:
    """Bind a multiplier into a :class:`FeeModel` for ``category``."""

    def fee_fn(price: Decimal, size: Decimal) -> Decimal:
        return kalshi_taker_fee(price, size, multiplier)

    return FeeModel(platform=Platform.KALSHI, category=category, fee_fn=fee_fn)
