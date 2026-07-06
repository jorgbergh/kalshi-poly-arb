"""Net-margin signal engine (plan §3.4, §7).

MILESTONE STATUS — partial, deliberate pull-forward (flagged 2026-07-05):
this implements the §3.4 net-margin formula for BOTH directions at
**top-of-book only**, sized to the smaller best level. It exists now (ahead
of milestone 7) to give the project its earliest possible end-to-end reality
check: a real cross-platform net-of-fee number from live books.

Still to come in milestone 7 proper: depth walking for the full target size
(bookwalk.py), partial-fill reporting, staleness checks, thresholding, and
the stage's ``StageResult`` with EMPTY_BOOK / INSUFFICIENT_DEPTH /
NEGATIVE_MARGIN / BELOW_THRESHOLD drops.

The core formula (plan §3.4), per share-pair, buying YES on one platform and
NO on the other::

    net_per_pair = 1 - fill_yes - fill_no - fee_yes/size - fee_no/size

Both directions are always evaluated; the caller gets them best-first.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from arbdetector.fees.base import FeeRegistry
from arbdetector.schema import Direction, NormalizedMarket, Platform

_ONE = Decimal("1")
_HUNDRED = Decimal("100")


@dataclass(frozen=True)
class DirectionQuote:
    """One direction of a pair, priced at top-of-book (pre-ArbOpportunity:
    the full object with ids/timestamps arrives with milestone 7)."""

    direction: Direction
    size: Decimal          # share-pairs quotable at the two best levels
    fill_yes: Decimal      # price paid per share on the YES leg
    fill_no: Decimal
    fee_yes: Decimal       # total dollar fee for the whole YES-leg order
    fee_no: Decimal
    net_total: Decimal     # dollars across the whole position
    net_per_pair: Decimal
    roi_pct: Decimal       # net / capital deployed (incl. fees), in percent


def quote_direction(
    *,
    direction: Direction,
    yes_market: NormalizedMarket,
    no_market: NormalizedMarket,
    target_size: Decimal,
    fee_registry: FeeRegistry,
) -> DirectionQuote | None:
    """Price one direction at top-of-book; None if either book is empty.

    ``yes_market`` is the market whose YES we buy, ``no_market`` the one
    whose NO we buy. Size is capped by both best levels (no depth walking
    yet — see module docstring).
    """
    if not yes_market.yes_ask or not no_market.no_ask:
        return None
    yes_level = yes_market.yes_ask[0]
    no_level = no_market.no_ask[0]
    size = min(target_size, yes_level.size, no_level.size)
    if size <= 0:
        return None

    fee_yes = fee_registry.get(yes_market.platform, yes_market.category).fee_fn(
        yes_level.price, size
    )
    fee_no = fee_registry.get(no_market.platform, no_market.category).fee_fn(
        no_level.price, size
    )
    gross_total = (_ONE - yes_level.price - no_level.price) * size
    net_total = gross_total - fee_yes - fee_no
    capital = (yes_level.price + no_level.price) * size + fee_yes + fee_no
    roi_pct = (net_total / capital * _HUNDRED) if capital > 0 else Decimal("0")

    return DirectionQuote(
        direction=direction,
        size=size,
        fill_yes=yes_level.price,
        fill_no=no_level.price,
        fee_yes=fee_yes,
        fee_no=fee_no,
        net_total=net_total,
        net_per_pair=net_total / size,
        roi_pct=roi_pct,
    )


def evaluate_pair_top_of_book(
    kalshi_market: NormalizedMarket,
    poly_market: NormalizedMarket,
    *,
    target_size: Decimal,
    fee_registry: FeeRegistry,
) -> list[DirectionQuote]:
    """Both §3.4 directions for one hand-/LLM-matched pair, best net first.

    Directions with an empty relevant book are omitted (the M7 funnel will
    count them as EMPTY_BOOK drops).
    """
    if kalshi_market.platform is not Platform.KALSHI:
        raise ValueError(f"expected a kalshi market, got {kalshi_market.platform}")
    if poly_market.platform is not Platform.POLYMARKET:
        raise ValueError(f"expected a polymarket market, got {poly_market.platform}")

    quotes = [
        quote_direction(
            direction=Direction.YES_KALSHI_NO_POLY,
            yes_market=kalshi_market,
            no_market=poly_market,
            target_size=target_size,
            fee_registry=fee_registry,
        ),
        quote_direction(
            direction=Direction.NO_KALSHI_YES_POLY,
            yes_market=poly_market,
            no_market=kalshi_market,
            target_size=target_size,
            fee_registry=fee_registry,
        ),
    ]
    present = [q for q in quotes if q is not None]
    present.sort(key=lambda q: q.net_per_pair, reverse=True)
    return present


# ---------------------------------------------------------------------------
# Spot check: price one hand-matched cross-platform pair from LIVE books.
#   .venv/bin/python -m arbdetector.engine.signal \
#       --kalshi-ticker KX... --poly-slug some-event-slug
# ---------------------------------------------------------------------------


def _spot_check(argv: list[str] | None = None) -> None:
    import argparse

    # Deferred imports: the engine module itself must stay pure (no client/
    # network dependency at import time).
    from arbdetector.clients.kalshi import KalshiClient
    from arbdetector.clients.polymarket import PolymarketClient
    from arbdetector.config import load_config
    from arbdetector.fees import build_fee_registry

    parser = argparse.ArgumentParser(
        description="Price one HAND-MATCHED Kalshi/Polymarket pair live, both directions."
    )
    parser.add_argument("--kalshi-ticker", required=True)
    parser.add_argument("--poly-slug", required=True, help="Gamma event slug")
    parser.add_argument(
        "--poly-question",
        help="substring to pick one market when the event has several",
    )
    parser.add_argument(
        "--poly-inverted",
        action="store_true",
        help="the Polymarket market is the INVERSE phrasing of the Kalshi one "
        "(its YES = Kalshi's NO); swaps the poly books accordingly — the "
        "manual preview of the adjudicator's same_direction=false (plan §6)",
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    registry = build_fee_registry(config.fees)

    with KalshiClient() as kalshi_client, PolymarketClient() as poly_client:
        kalshi_market = kalshi_client.get_market(args.kalshi_ticker)
        kalshi_market.yes_ask, kalshi_market.no_ask = kalshi_client.fetch_order_book(
            kalshi_market
        )

        candidates = poly_client.get_event_markets(args.poly_slug)
        if args.poly_question:
            needle = args.poly_question.lower()
            candidates = [m for m in candidates if needle in m.title.lower()]
        if len(candidates) != 1:
            print(f"need exactly one Polymarket market, found {len(candidates)}:")
            for m in candidates:
                print(f"  --poly-question '...' to pick: {m.title}")
            raise SystemExit(2)
        poly_market = candidates[0]
        poly_market.yes_ask, poly_market.no_ask = poly_client.fetch_order_book(poly_market)
        if args.poly_inverted:
            poly_market.yes_ask, poly_market.no_ask = (
                poly_market.no_ask,
                poly_market.yes_ask,
            )
            print("NOTE: poly books swapped (--poly-inverted); all YES/NO below are "
                  "in the KALSHI market's frame")

    print("PAIR (hand-matched — NOT adjudicated; rules equivalence unverified until M6):")
    for m in (kalshi_market, poly_market):
        print(f"  [{m.platform.value:10s}] {m.title}")
        print(f"    close={m.close_time}  category={m.category}  source={m.resolution_source}")

    quotes = evaluate_pair_top_of_book(
        kalshi_market,
        poly_market,
        target_size=config.engine.target_size_pairs,
        fee_registry=registry,
    )
    if not quotes:
        print("no quotable direction (empty book on at least one leg)")
        return
    for q in quotes:
        print(
            f"  {q.direction.value:22s} size={q.size:>10.2f}  "
            f"fills: YES@{q.fill_yes} + NO@{q.fill_no}  "
            f"fees: {q.fee_yes}/{q.fee_no}  "
            f"net/pair: ${q.net_per_pair:+.4f}  roi: {q.roi_pct:+.2f}%"
        )
    threshold = config.engine.net_threshold_per_pair
    verdict = "ABOVE" if quotes[0].net_per_pair > threshold else "below"
    print(f"best direction is {verdict} the ${threshold} alert threshold")


if __name__ == "__main__":
    _spot_check()
