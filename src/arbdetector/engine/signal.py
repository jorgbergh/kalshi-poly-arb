"""Net-margin signal engine (plan §3.4, §7, milestone 7).

Prices every blessed pair by WALKING the ask books (engine/bookwalk.py) for
the configured target size — never top-of-book, never Gamma prices. Both
§3.4 directions are always evaluated; partial fills are first-class: when
depth runs out, the quote reports the truly achievable size and the fill at
that size.

Two funnel stages live here (units: pairs):

- ``run_price``   — books fetched/replayed, staleness checked, directions
  walked. Drops: API_ERROR, STALE_BOOK, EMPTY_BOOK, INSUFFICIENT_DEPTH.
- ``run_threshold`` — the §3.4 alert rule (strictly greater than the
  configured threshold). Drops: NEGATIVE_MARGIN, BELOW_THRESHOLD. Survivors
  become §5 ``ArbOpportunity`` objects.

Fee note: fees are evaluated at the size-weighted fill price exactly as
§3.4 prescribes. When a fill spans several price levels this approximates
per-level fee assessment; the curve is smooth, so the error is sub-cent at
realistic sizes.

Recordings: ``dump_recordings``/``load_recordings`` + ``replay_fetcher``
run the whole engine offline from captured books — the seed of the plan's
``--simulate`` mode (§7) and the regression-fixture mechanism (§12).
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Mapping, Sequence

from arbdetector.engine.bookwalk import walk_book
from arbdetector.fees.base import FeeRegistry
from arbdetector.schema import (
    ArbOpportunity,
    Direction,
    MatchedPair,
    NormalizedMarket,
    OrderBookLevel,
    Platform,
)
from arbdetector.tracking import DropReason, Stage, StageResult, entity_id, pair_id
from arbdetector.tracking.ids import opp_id

_HUNDRED = Decimal("100")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def matched_pair_id(pair: MatchedPair) -> str:
    """The deterministic §9.2 pair id for an adjudicated pair."""
    return pair_id(
        entity_id(Platform.KALSHI, pair.kalshi.market_id),
        entity_id(Platform.POLYMARKET, pair.polymarket.market_id),
    )


def opportunity_id(opportunity: ArbOpportunity) -> str:
    """The §9.2 opp id (ArbOpportunity itself stays the pure §5 shape)."""
    return opp_id(
        matched_pair_id(opportunity.pair),
        opportunity.direction,
        opportunity.detected_ts,
    )


# ---------------------------------------------------------------------------
# Walked direction quotes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectionQuote:
    """One direction of a pair, priced by walking both legs' books."""

    direction: Direction
    size: Decimal          # share-pairs actually achievable (may be < target)
    fill_yes: Decimal      # size-weighted walked fill price of the YES leg
    fill_no: Decimal
    fee_yes: Decimal       # total dollar fee for the whole YES-leg order
    fee_no: Decimal
    net_total: Decimal     # dollars across the whole position (exact)
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
    """Walk both legs for one direction; None when either book is empty.

    Achievable size is the min of the two walks; the deeper leg is then
    RE-WALKED at that size — it stops at better levels when the shallow leg
    constrains, and pricing it at the full walk would overstate cost.
    Totals use exact per-level costs; only the reported average fill prices
    involve division.
    """
    if not yes_market.yes_ask or not no_market.no_ask:
        return None
    yes_probe = walk_book(yes_market.yes_ask, target_size)
    no_probe = walk_book(no_market.no_ask, target_size)
    size = min(yes_probe.size, no_probe.size)
    if size <= 0:
        return None
    yes_fill = yes_probe if yes_probe.size == size else walk_book(yes_market.yes_ask, size)
    no_fill = no_probe if no_probe.size == size else walk_book(no_market.no_ask, size)

    fee_yes = fee_registry.get(yes_market.platform, yes_market.category).fee_fn(
        yes_fill.avg_price, size
    )
    fee_no = fee_registry.get(no_market.platform, no_market.category).fee_fn(
        no_fill.avg_price, size
    )
    gross_total = size - yes_fill.cost - no_fill.cost  # size * $1 payout - costs
    net_total = gross_total - fee_yes - fee_no
    capital = yes_fill.cost + no_fill.cost + fee_yes + fee_no
    roi_pct = (net_total / capital * _HUNDRED) if capital > 0 else Decimal("0")

    return DirectionQuote(
        direction=direction,
        size=size,
        fill_yes=yes_fill.avg_price,
        fill_no=no_fill.avg_price,
        fee_yes=fee_yes,
        fee_no=fee_no,
        net_total=net_total,
        net_per_pair=net_total / size,
        roi_pct=roi_pct,
    )


def evaluate_pair(
    kalshi_market: NormalizedMarket,
    poly_market: NormalizedMarket,
    *,
    target_size: Decimal,
    fee_registry: FeeRegistry,
) -> list[DirectionQuote]:
    """Both §3.4 directions for one matched pair, best net first.

    Directions with an empty relevant book are omitted; ``run_price`` counts
    a pair with zero quotable directions as an EMPTY_BOOK drop.
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
# Books plumbing: live fetch, record, replay
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PairBooks:
    """The four ask books needed to price one pair, plus fetch time.

    Polymarket books are stored in the POLY market's own frame; ``run_price``
    applies the ``same_direction`` swap (plan §6), so recordings stay raw.
    """

    kalshi_yes_ask: list[OrderBookLevel]
    kalshi_no_ask: list[OrderBookLevel]
    poly_yes_ask: list[OrderBookLevel]
    poly_no_ask: list[OrderBookLevel]
    fetched_at: datetime


BookFetcher = Callable[[MatchedPair], PairBooks]


def live_book_fetcher(kalshi_client, poly_client) -> BookFetcher:
    """Fresh books straight from both platforms' live endpoints (plan §7)."""

    def fetch(pair: MatchedPair) -> PairBooks:
        kalshi_yes, kalshi_no = kalshi_client.fetch_order_book(pair.kalshi)
        poly_yes, poly_no = poly_client.fetch_order_book(pair.polymarket)
        return PairBooks(kalshi_yes, kalshi_no, poly_yes, poly_no, fetched_at=_now_utc())

    return fetch


def recording_fetcher(fetch: BookFetcher, sink: dict[str, "PairBooks"]) -> BookFetcher:
    """Wrap a fetcher so every fetched book lands in ``sink`` keyed by pair id."""

    def wrapped(pair: MatchedPair) -> PairBooks:
        books = fetch(pair)
        sink[matched_pair_id(pair)] = books
        return books

    return wrapped


def replay_fetcher(recordings: Mapping[str, "PairBooks"]) -> BookFetcher:
    """Serve books from a recording; a missing pair raises (→ API_ERROR drop)."""

    def fetch(pair: MatchedPair) -> PairBooks:
        return recordings[matched_pair_id(pair)]

    return fetch


def _levels_to_json(levels: Sequence[OrderBookLevel]) -> list[list[str]]:
    return [[str(level.price), str(level.size)] for level in levels]


def _levels_from_json(raw: Sequence[Sequence[str]]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=Decimal(p), size=Decimal(s)) for p, s in raw]


_BOOK_KEYS = ("kalshi_yes_ask", "kalshi_no_ask", "poly_yes_ask", "poly_no_ask")


def dump_recordings(recordings: Mapping[str, PairBooks], path: str | Path) -> None:
    """Decimals serialize as strings — recordings must replay bit-exact."""
    payload = {
        pid: {
            "fetched_at": books.fetched_at.isoformat(),
            **{key: _levels_to_json(getattr(books, key)) for key in _BOOK_KEYS},
        }
        for pid, books in recordings.items()
    }
    Path(path).write_text(json.dumps(payload, indent=1), encoding="utf-8")


def load_recordings(path: str | Path) -> dict[str, PairBooks]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        pid: PairBooks(
            fetched_at=datetime.fromisoformat(entry["fetched_at"]),
            **{key: _levels_from_json(entry[key]) for key in _BOOK_KEYS},
        )
        for pid, entry in payload.items()
    }


# ---------------------------------------------------------------------------
# The price and threshold funnel stages (plan §7, §9.4). Units: pairs.
# ---------------------------------------------------------------------------


def run_price(
    blessed: Sequence[MatchedPair],
    *,
    fetch_books: BookFetcher,
    target_size: Decimal,
    min_size: Decimal,
    max_book_age_sec: float,
    fee_registry: FeeRegistry,
    now: datetime | None = None,
) -> tuple[list[tuple[MatchedPair, DirectionQuote]], StageResult]:
    """Price stage: blessed pairs in, ``(pair, best walked quote)`` out.

    Inverted pairs (``same_direction=False``) get their Polymarket books
    swapped into the Kalshi frame here — the engine's one §6 responsibility.
    A pair survives only if some direction fills at least ``min_size``;
    among the deep-enough directions the best net wins.
    """
    started = time.perf_counter()
    priced: list[tuple[MatchedPair, DirectionQuote]] = []
    dropped: dict[DropReason, list[str]] = defaultdict(list)

    for pair in blessed:
        pid = matched_pair_id(pair)
        try:
            books = fetch_books(pair)
        except Exception:
            dropped[DropReason.API_ERROR].append(pid)
            continue
        current = now or _now_utc()
        if (current - books.fetched_at).total_seconds() > max_book_age_sec:
            dropped[DropReason.STALE_BOOK].append(pid)
            continue

        pair.kalshi.yes_ask = books.kalshi_yes_ask
        pair.kalshi.no_ask = books.kalshi_no_ask
        poly_yes, poly_no = books.poly_yes_ask, books.poly_no_ask
        if not pair.same_direction:
            poly_yes, poly_no = poly_no, poly_yes
        pair.polymarket.yes_ask = poly_yes
        pair.polymarket.no_ask = poly_no

        quotes = evaluate_pair(
            pair.kalshi, pair.polymarket, target_size=target_size, fee_registry=fee_registry
        )
        if not quotes:
            dropped[DropReason.EMPTY_BOOK].append(pid)
            continue
        deep_enough = [q for q in quotes if q.size >= min_size]
        if not deep_enough:
            dropped[DropReason.INSUFFICIENT_DEPTH].append(pid)
            continue
        priced.append((pair, deep_enough[0]))

    result = StageResult(
        stage=Stage.PRICE,
        n_in=len(blessed),
        n_out=len(priced),
        drops={reason: len(ids) for reason, ids in dropped.items()},
        dropped_ids=dict(dropped),
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return priced, result


def run_threshold(
    priced: Sequence[tuple[MatchedPair, DirectionQuote]],
    *,
    threshold: Decimal,
    detected_ts: str | None = None,
) -> tuple[list[ArbOpportunity], StageResult]:
    """Threshold stage: the §3.4 alert rule, strictly greater-than.

    Survivors become §5 ArbOpportunity objects carrying the walked fills;
    ``opportunity_id`` derives their §9.2 id.
    """
    started = time.perf_counter()
    ts = detected_ts or _now_iso()
    opportunities: list[ArbOpportunity] = []
    dropped: dict[DropReason, list[str]] = defaultdict(list)

    for pair, quote in priced:
        pid = matched_pair_id(pair)
        if quote.net_per_pair <= 0:
            dropped[DropReason.NEGATIVE_MARGIN].append(pid)
        elif quote.net_per_pair <= threshold:
            dropped[DropReason.BELOW_THRESHOLD].append(pid)
        else:
            opportunities.append(
                ArbOpportunity(
                    pair=pair,
                    direction=quote.direction,
                    size=quote.size,
                    fill_yes=quote.fill_yes,
                    fill_no=quote.fill_no,
                    fee_yes=quote.fee_yes,
                    fee_no=quote.fee_no,
                    net_per_pair=quote.net_per_pair,
                    roi_pct=quote.roi_pct,
                    detected_ts=ts,
                )
            )

    result = StageResult(
        stage=Stage.THRESHOLD,
        n_in=len(priced),
        n_out=len(opportunities),
        drops={reason: len(ids) for reason, ids in dropped.items()},
        dropped_ids=dict(dropped),
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return opportunities, result


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
        description="Price one HAND-MATCHED Kalshi/Polymarket pair live, both "
        "directions, walking full book depth."
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

    print("PAIR (hand-matched — NOT adjudicated; rules equivalence unverified):")
    for m in (kalshi_market, poly_market):
        print(f"  [{m.platform.value:10s}] {m.title}")
        print(f"    close={m.close_time}  category={m.category}  source={m.resolution_source}")

    quotes = evaluate_pair(
        kalshi_market,
        poly_market,
        target_size=config.engine.target_size_pairs,
        fee_registry=registry,
    )
    if not quotes:
        print("no quotable direction (empty book on at least one leg)")
        return
    for q in quotes:
        partial = "" if q.size >= config.engine.target_size_pairs else "  [partial fill]"
        print(
            f"  {q.direction.value:22s} size={q.size:>10.2f}{partial}  "
            f"fills: YES@{q.fill_yes:.4f} + NO@{q.fill_no:.4f}  "
            f"fees: {q.fee_yes}/{q.fee_no}  "
            f"net/pair: ${q.net_per_pair:+.4f}  roi: {q.roi_pct:+.2f}%"
        )
    threshold = config.engine.net_threshold_per_pair
    verdict = "ABOVE" if quotes[0].net_per_pair > threshold else "below"
    print(f"best direction is {verdict} the ${threshold} alert threshold")


if __name__ == "__main__":
    _spot_check()
