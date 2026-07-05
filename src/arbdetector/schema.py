"""Common normalized domain schema (plan §5).

Every platform adapter maps its raw payloads into these types; everything
downstream (matching, signal engine, tracking, alerting) depends only on this
module, never on platform-specific shapes.

Conventions (plan §4, §13):
- All money and sizes are ``decimal.Decimal`` — never float.
- All timestamps are ISO 8601 strings.
- Categorical values are enums, never freeform strings.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Callable


class Platform(StrEnum):
    """A supported prediction-market platform (plan §5).

    Adding a third platform later means adding one value here plus one
    adapter — nothing downstream changes (plan §9.11).
    """

    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Direction(StrEnum):
    """Which leg is bought on which platform.

    Plan §3.4/§7: both directions are evaluated each cycle and the better one
    is reported. The values are the exact display strings used in the plan's
    status board and JSONL examples.
    """

    YES_KALSHI_NO_POLY = "YES@kalshi+NO@poly"
    NO_KALSHI_YES_POLY = "NO@kalshi+YES@poly"


@dataclass(frozen=True)
class OrderBookLevel:
    """One price level of an ask book: what you would PAY to buy that side."""

    price: Decimal  # $/share, 0..1 (an ASK price to BUY that side)
    size: Decimal   # shares/contracts available at this level


@dataclass
class NormalizedMarket:
    """A single binary market on a single platform, in the common shape.

    On Kalshi the ask levels are *derived* from the opposite side's bids
    (best_yes_ask = 1 - best_no_bid, plan §2.1) — that reconstruction lives in
    the Kalshi client, never here.
    """

    platform: Platform
    market_id: str                    # kalshi ticker OR polymarket condition_id
    yes_token_id: str | None          # polymarket token id for YES (None on kalshi)
    no_token_id: str | None
    title: str                        # human question text
    category: str                     # normalized category label
    resolution_criteria: str          # FULL rules text — critical for LLM matching
    resolution_source: str | None     # who/what adjudicates
    close_time: str                   # ISO 8601
    yes_ask: list[OrderBookLevel]     # levels to BUY YES, best first (derived on Kalshi!)
    no_ask: list[OrderBookLevel]      # levels to BUY NO, best first
    raw: dict                         # original payload, for debugging


@dataclass
class FeeModel:
    """A per-(platform, category) taker-fee curve (plan §3, §5).

    Convention: ``fee_fn(price, size)`` returns the dollar fee for buying
    ``size`` shares at fill price ``price``. Both arguments and the result are
    ``Decimal``. Instances are built in :mod:`arbdetector.fees` and looked up
    via the registry there — the engine never hard-codes a fee formula.
    """

    platform: Platform
    category: str
    fee_fn: Callable[[Decimal, Decimal], Decimal]


@dataclass
class MatchedPair:
    """An LLM-adjudicated cross-platform pair (plan §5, §6 stage 2)."""

    kalshi: NormalizedMarket
    polymarket: NormalizedMarket
    is_same_event: bool
    confidence: float                 # 0..1 from the LLM
    resolution_caveats: str           # LLM notes on any subtle differences
    verdict_ts: str
    rules_hash: str                   # hash of both rule texts; re-adjudicate if it changes


@dataclass
class ArbOpportunity:
    """One detected net-of-fee arbitrage opportunity (plan §3.4, §5)."""

    pair: MatchedPair
    direction: Direction
    size: Decimal                     # share-pairs achievable at these levels
    fill_yes: Decimal
    fill_no: Decimal
    fee_yes: Decimal
    fee_no: Decimal
    net_per_pair: Decimal
    roi_pct: Decimal
    detected_ts: str
