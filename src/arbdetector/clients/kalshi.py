"""Kalshi REST client (plan §2.1, milestone 2).

Public, unauthenticated market-data endpoints only — read-only (plan §14).

THE critical quirk (plan §2.1, §13): Kalshi's orderbook endpoint returns
**only bids** for both YES and NO — no asks. In a binary market, a resting
NO bid at price P fully collateralizes the YES side at (1 - P), so the ask
you'd PAY to buy YES is derived from the NO bids (and vice versa):

    best_yes_ask = 1.00 - best_no_bid
    best_no_ask  = 1.00 - best_yes_bid

That derivation lives in exactly one function, :func:`derive_ask_book`, with
its own test file (tests/test_kalshi_orderbook_reconstruction.py). Never
inline the arithmetic.

Live response shapes (verified against the real API on 2026-07-05):

- ``GET /events?status=open&with_nested_markets=true`` ->
  ``{"events": [{..., "category", "settlement_sources", "markets": [...]}], "cursor"}``
- ``GET /markets/{ticker}/orderbook`` ->
  ``{"orderbook_fp": {"yes_dollars": [["0.0100", "19741.00"], ...], "no_dollars": [...]}}``
  — both sides are BID arrays of ``[dollar-string price, fixed-point-string size]``,
  exactly the format plan §2.1 promises.

DELIBERATE DEVIATION from plan §2.1's discovery flow (flagged 2026-07-05):
the plan suggests paging /markets and calling /events/{event_ticker} per
event for the category. Live, /events with nested markets carries category +
settlement sources + full market objects in ONE paginated stream (~50x fewer
requests against the ~30 req/s public budget), and the /markets listing is
dominated by multivariate parlay legs. Discovery therefore pages /events;
``get_market`` still uses the plan's per-ticker endpoints.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from typing import Any, Iterable, Sequence

import httpx

from arbdetector.clients._http import RetryingJsonHttp
from arbdetector.clients.base import MarketDataClient
from arbdetector.clients.base import parse_fixed_point as _parse_decimal
from arbdetector.schema import NormalizedMarket, OrderBookLevel, Platform

KALSHI_BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

_ONE = Decimal("1")
_PAGE_LIMIT = 200


class KalshiApiError(RuntimeError):
    """The Kalshi API kept failing (429/5xx/transport) after all retries."""


# ---------------------------------------------------------------------------
# Pure functions — no I/O, fully unit-tested
# ---------------------------------------------------------------------------


def derive_ask_book(opposite_side_bids: Iterable[OrderBookLevel]) -> list[OrderBookLevel]:
    """Derive one side's ASK book from the OTHER side's bids (plan §2.1).

    Usage::

        yes_ask = derive_ask_book(no_bids)
        no_ask  = derive_ask_book(yes_bids)

    The best (highest-price) opposite bid becomes the best (lowest-price)
    ask; sizes carry over unchanged; the result is sorted best-first
    (ascending price). Prices outside [0, 1] are refused loudly — an
    out-of-range value almost certainly means an integer-cents payload that
    would otherwise be silently mis-scaled 100x.
    """
    asks: list[OrderBookLevel] = []
    for level in opposite_side_bids:
        if not Decimal(0) <= level.price <= _ONE:
            raise ValueError(
                f"bid price {level.price} outside [0, 1] dollars — refusing to derive asks"
            )
        if level.size < 0:
            raise ValueError(f"bid size {level.size} is negative")
        asks.append(OrderBookLevel(price=_ONE - level.price, size=level.size))
    asks.sort(key=lambda lvl: lvl.price)
    return asks


def parse_bid_levels(raw_levels: Any) -> list[OrderBookLevel]:
    """``[["0.0100", "19741.00"], ...]`` -> validated ``OrderBookLevel`` list."""
    levels: list[OrderBookLevel] = []
    for entry in raw_levels or []:
        if not isinstance(entry, (list, tuple)) or len(entry) < 2:
            raise ValueError(f"malformed order-book level: {entry!r}")
        price = _parse_decimal(entry[0], what="order-book price")
        size = _parse_decimal(entry[1], what="order-book size")
        if not Decimal(0) <= price <= _ONE:
            raise ValueError(
                f"bid price {price} outside [0, 1] dollars — looks like an "
                f"integer-cents payload; refusing to guess the scale"
            )
        if size < 0:
            raise ValueError(f"negative order-book size: {size}")
        levels.append(OrderBookLevel(price=price, size=size))
    return levels


def parse_orderbook_response(
    payload: dict,
) -> tuple[list[OrderBookLevel], list[OrderBookLevel]]:
    """Full orderbook payload -> ``(yes_ask, no_ask)``, both derived from bids.

    Live shape (2026-07)::

        {"orderbook_fp": {"yes_dollars": [[price, size], ...], "no_dollars": [...]}}
    """
    book = payload.get("orderbook_fp")
    if book is None:
        raise ValueError(
            f"no 'orderbook_fp' key in orderbook response (got keys {sorted(payload)}) — "
            f"has the API shape changed?"
        )
    yes_bids = parse_bid_levels(book.get("yes_dollars"))
    no_bids = parse_bid_levels(book.get("no_dollars"))
    return derive_ask_book(no_bids), derive_ask_book(yes_bids)


def normalize_market(
    raw_market: dict,
    *,
    category: str,
    settlement_sources: list[dict] | None = None,
) -> NormalizedMarket:
    """Map one raw Kalshi market (plus its event's category/sources) into the
    common schema. Books are left empty — they come from the orderbook
    endpoint on the fast cadence."""
    rules = " ".join(
        part
        for part in (raw_market.get("rules_primary"), raw_market.get("rules_secondary"))
        if part
    ).strip()
    title = (raw_market.get("title") or "").strip()
    subtitle = (raw_market.get("yes_sub_title") or raw_market.get("subtitle") or "").strip()
    if subtitle and subtitle not in title:
        title = f"{title} — {subtitle}" if title else subtitle
    source_names = (
        ", ".join(s.get("name", "") for s in settlement_sources or [] if s.get("name"))
        or None
    )
    return NormalizedMarket(
        platform=Platform.KALSHI,
        market_id=raw_market["ticker"],
        yes_token_id=None,
        no_token_id=None,
        title=title or raw_market["ticker"],
        category=category,
        resolution_criteria=rules,
        resolution_source=source_names,
        close_time=raw_market.get("close_time", ""),
        yes_ask=[],
        no_ask=[],
        raw=raw_market,
    )


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class KalshiClient(MarketDataClient):
    """Read-only Kalshi adapter: discovery + order books, no auth."""

    platform = Platform.KALSHI

    def __init__(
        self,
        *,
        base_url: str = KALSHI_BASE_URL,
        timeout_sec: float = 10.0,
        backoff_base_sec: float = 1.0,
        max_retries: int = 4,
        max_requests_per_sec: float = 25.0,  # stay under the ~30/s public budget
        http_client: httpx.Client | None = None,
    ) -> None:
        self._transport = RetryingJsonHttp(
            http_client or httpx.Client(base_url=base_url, timeout=timeout_sec),
            backoff_base_sec=backoff_base_sec,
            max_retries=max_retries,
            max_requests_per_sec=max_requests_per_sec,
            error_cls=KalshiApiError,
        )

    def close(self) -> None:
        self._transport.close()

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        return self._transport.get_json(path, params)

    # -- MarketDataClient port ----------------------------------------------

    def discover_markets(
        self, categories: Sequence[str], *, max_pages: int | None = None
    ) -> list[NormalizedMarket]:
        """Open binary markets in ``categories`` via the nested-events stream.

        Excluded: non-binary markets and multivariate parlay legs
        (``mve_collection_ticker`` set) — combination bets can't be matched to
        a single real-world event on another platform.

        ``max_pages`` bounds the scan for smoke tests; production discovery
        passes None and walks the full cursor.
        """
        wanted = {c.strip().lower() for c in categories}
        markets: list[NormalizedMarket] = []
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {
                "status": "open",
                "limit": _PAGE_LIMIT,
                "with_nested_markets": "true",
            }
            if cursor:
                params["cursor"] = cursor
            payload = self._get_json("/events", params)
            for event in payload.get("events") or []:
                if (event.get("category") or "").strip().lower() not in wanted:
                    continue
                for raw_market in event.get("markets") or []:
                    if raw_market.get("market_type") != "binary":
                        continue
                    if raw_market.get("mve_collection_ticker"):
                        continue
                    markets.append(
                        normalize_market(
                            raw_market,
                            category=event.get("category", ""),
                            settlement_sources=event.get("settlement_sources"),
                        )
                    )
            cursor = payload.get("cursor") or None
            pages += 1
            if cursor is None or (max_pages is not None and pages >= max_pages):
                break
        return markets

    def fetch_order_book(
        self, market: NormalizedMarket
    ) -> tuple[list[OrderBookLevel], list[OrderBookLevel]]:
        payload = self._get_json(f"/markets/{market.market_id}/orderbook")
        return parse_orderbook_response(payload)

    # -- extras (not part of the port) ---------------------------------------

    def get_market(self, ticker: str) -> NormalizedMarket:
        """One market by ticker, using the plan's per-ticker endpoints
        (/markets/{ticker} + /events/{event_ticker} for category/sources)."""
        market_payload = self._get_json(f"/markets/{ticker}")
        raw_market = market_payload.get("market") or market_payload
        event_payload = self._get_json(f"/events/{raw_market['event_ticker']}")
        event = event_payload.get("event") or {}
        return normalize_market(
            raw_market,
            category=event.get("category", ""),
            settlement_sources=event.get("settlement_sources"),
        )


# ---------------------------------------------------------------------------
# Milestone-2 acceptance smoke (plan §11): print best asks for live markets.
#   .venv/bin/python -m arbdetector.clients.kalshi --limit 3
# ---------------------------------------------------------------------------


def _fmt_best(levels: list[OrderBookLevel]) -> str:
    return f"${levels[0].price} x {levels[0].size}" if levels else "(empty book)"


def _smoke(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Print best derived YES/NO asks for live Kalshi markets (read-only)."
    )
    parser.add_argument("--ticker", help="specific market ticker; skips discovery")
    parser.add_argument("--categories", nargs="+", default=["World", "Politics"])
    parser.add_argument("--limit", type=int, default=3, help="max markets to print")
    parser.add_argument("--max-pages", type=int, default=2, help="discovery pages to scan")
    args = parser.parse_args(argv)

    with KalshiClient() as client:
        if args.ticker:
            markets = [client.get_market(args.ticker)]
        else:
            markets = client.discover_markets(args.categories, max_pages=args.max_pages)
            print(
                f"discovered {len(markets)} open binary markets in {args.categories} "
                f"(first {args.max_pages} event pages)"
            )
            markets = markets[: args.limit]
        for market in markets:
            yes_ask, no_ask = client.fetch_order_book(market)
            print(f"[{market.market_id}] {market.title[:72]}")
            print(f"    category={market.category}  close={market.close_time}")
            print(f"    best YES ask: {_fmt_best(yes_ask)}   best NO ask: {_fmt_best(no_ask)}")


if __name__ == "__main__":
    _smoke()
