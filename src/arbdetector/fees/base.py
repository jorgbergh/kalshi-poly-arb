"""Fee-model registry keyed by (platform, category) — plan §3/§4.

Lookup rules:
- Exact ``(platform, category)`` match wins (categories are case-insensitive).
- Kalshi registers a *platform default* (the configurable 0.07 multiplier
  applies across categories until per-category overrides are needed).
- Polymarket has **no default**: rates differ materially by category
  (0.00–0.07), so an unknown category raises rather than guessing a fee —
  a wrong guess is exactly the silent failure the plan warns about (§13).
"""

from __future__ import annotations

from decimal import Decimal

from arbdetector.config import FeesConfig
from arbdetector.schema import FeeModel, Platform


def validate_price_size(price: Decimal, size: Decimal) -> None:
    """Shared argument guard for every fee function.

    Rejects non-Decimal inputs outright: a float sneaking into money math is
    the failure mode the plan bans (§4, §13), and catching it at the fee
    boundary keeps the whole engine honest.
    """
    if not isinstance(price, Decimal) or not isinstance(size, Decimal):
        raise TypeError(
            f"price and size must be Decimal, got {type(price).__name__}/"
            f"{type(size).__name__} — never use float for money (plan §4)"
        )
    if not Decimal(0) <= price <= Decimal(1):
        raise ValueError(f"price must be in [0, 1] dollars, got {price}")
    if size < 0:
        raise ValueError(f"size must be >= 0, got {size}")


class FeeRegistry:
    """Holds one :class:`FeeModel` per (platform, category), plus optional
    per-platform defaults."""

    def __init__(self) -> None:
        self._models: dict[tuple[Platform, str], FeeModel] = {}
        self._defaults: dict[Platform, FeeModel] = {}

    def register(self, model: FeeModel, *, platform_default: bool = False) -> None:
        key = (model.platform, model.category.lower())
        if key in self._models:
            raise ValueError(f"fee model already registered for {key}")
        self._models[key] = model
        if platform_default:
            self._defaults[model.platform] = model

    def get(self, platform: Platform | str, category: str) -> FeeModel:
        platform = Platform(platform)
        key = (platform, category.lower())
        if key in self._models:
            return self._models[key]
        if platform in self._defaults:
            return self._defaults[platform]
        raise KeyError(
            f"no fee model for platform={platform.value!r} category={category!r} "
            f"and no platform default — refusing to guess a fee schedule"
        )


def build_fee_registry(fees: FeesConfig) -> FeeRegistry:
    """Build the registry from validated config (plan §15 ``fees:`` block)."""
    # Deferred: the formula modules import validate_price_size from this
    # module, so a top-level import here would be circular.
    from arbdetector.fees.kalshi_fees import make_kalshi_fee_model
    from arbdetector.fees.polymarket_fees import make_polymarket_fee_model

    registry = FeeRegistry()
    registry.register(
        make_kalshi_fee_model("*", multiplier=fees.kalshi_multiplier_default),
        platform_default=True,
    )
    for category, rate in fees.polymarket_fee_rates.items():
        registry.register(make_polymarket_fee_model(category, fee_rate=rate))
    return registry
