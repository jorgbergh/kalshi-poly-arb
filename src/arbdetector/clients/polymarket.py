"""Polymarket client: Gamma discovery + CLOB order books (plan §2.2, milestone 3).

Public read-only endpoints — no auth, no wallet, ever (plan §14). Two of the
three Polymarket services are used here:

- **Gamma** (``gamma-api.polymarket.com``): discovery/metadata ONLY. Its
  ``outcomePrices`` lag the live book by seconds (plan §2.2 staleness gotcha),
  so prices are never read from Gamma.
- **CLOB** (``clob.polymarket.com``): the actual order books, queried by
  TOKEN id.

THE classic bug (plan §2.2, §13): a **condition id** identifies a *market*;
a **token id** identifies one *outcome* (YES or NO). Book queries take token
ids. The explicit mapping lives in :func:`parse_clob_token_ids` and is
matched by outcome LABEL, never by array position.

Live response shapes (verified against the real APIs on 2026-07-05):

- ``GET {gamma}/events?active=true&closed=false&tag_slug=geopolitics&limit=100&offset=N``
  -> bare JSON **array** of events. Several market fields (``clobTokenIds``,
  ``outcomes``, ``outcomePrices``) are STRINGIFIED JSON needing a second
  ``json.loads``. Pagination is limit/offset; a short page means done.
- ``GET {clob}/book?token_id=...`` ->
  ``{"asks": [{"price": "0.99", "size": "400673.6"}, ...], "bids": [...], ...}``
  — string prices/sizes, and asks arrive sorted WORST-first (descending
  price). Sorting best-first on parse is load-bearing, not cosmetic.
- The geopolitics tag slug is ``geopolitics`` (id 100265); geopolitics
  markets carry ``feesEnabled: false`` — the fee-free v1 leg (plan §3.3),
  confirmed live.
"""

from __future__ import annotations

import argparse
import json
from typing import Any, Sequence

import httpx

from arbdetector.clients._http import RetryingJsonHttp
from arbdetector.clients.base import MarketDataClient
from arbdetector.clients.base import parse_fixed_point as _parse_decimal
from arbdetector.schema import NormalizedMarket, OrderBookLevel, Platform

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"

_PAGE_LIMIT = 100


class PolymarketApiError(RuntimeError):
    """A Polymarket API kept failing (429/5xx/transport) after all retries."""


# ---------------------------------------------------------------------------
# Pure functions — no I/O, fully unit-tested
# ---------------------------------------------------------------------------


def _load_stringified(raw_market: dict, field: str) -> list:
    """Gamma quirk: list-valued market fields arrive as JSON-encoded strings."""
    value = raw_market.get(field)
    if value is None:
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(
                f"market {raw_market.get('conditionId', '?')}: unparseable "
                f"{field}: {value[:60]!r}"
            ) from exc
    if not isinstance(value, list):
        raise ValueError(
            f"market {raw_market.get('conditionId', '?')}: {field} is not a list"
        )
    return value


def parse_clob_token_ids(raw_market: dict) -> tuple[str, str]:
    """One Gamma market dict -> ``(yes_token_id, no_token_id)``.

    Token ids — NOT the condition id — are what ``/book`` queries take
    (plan §2.2, §13). Tokens are matched to outcomes by LABEL ("Yes"/"No",
    case-insensitive), never by array position, so an inverted
    ``["No", "Yes"]`` ordering cannot silently swap the legs. Anything that
    is not exactly a binary Yes/No market is refused.
    """
    outcomes = [str(o).strip().lower() for o in _load_stringified(raw_market, "outcomes")]
    tokens = [str(t) for t in _load_stringified(raw_market, "clobTokenIds")]
    condition_id = raw_market.get("conditionId", "<missing conditionId>")
    if sorted(outcomes) != ["no", "yes"] or len(tokens) != 2:
        raise ValueError(
            f"market {condition_id}: not a binary Yes/No market "
            f"(outcomes={outcomes!r}, {len(tokens)} token ids)"
        )
    by_label = dict(zip(outcomes, tokens))
    return by_label["yes"], by_label["no"]


def parse_ask_levels(raw_asks: Any) -> list[OrderBookLevel]:
    """CLOB ask array -> validated levels, sorted BEST (lowest price) first.

    The live CLOB returns asks descending (worst first); relying on input
    order would make every "best ask" read the worst level in the book.
    """
    levels: list[OrderBookLevel] = []
    for entry in raw_asks or []:
        if not isinstance(entry, dict):
            raise ValueError(f"malformed CLOB level: {entry!r}")
        price = _parse_decimal(entry.get("price"), what="CLOB ask price")
        size = _parse_decimal(entry.get("size"), what="CLOB ask size")
        if not 0 <= price <= 1:
            raise ValueError(f"ask price {price} outside [0, 1] dollars")
        if size < 0:
            raise ValueError(f"negative ask size: {size}")
        levels.append(OrderBookLevel(price=price, size=size))
    levels.sort(key=lambda lvl: lvl.price)
    return levels


def normalize_market(
    raw_market: dict,
    *,
    category: str,
    resolution_source: str | None = None,
) -> NormalizedMarket:
    """Map one Gamma market (plus event-level context) into the common schema.

    ``resolution_criteria`` is the market's full ``description`` — Polymarket
    embeds the resolution rules there, and it is what the LLM adjudicator
    (M6) will read. Books are left empty; they come from the CLOB on the
    fast cadence.
    """
    yes_token_id, no_token_id = parse_clob_token_ids(raw_market)
    condition_id = raw_market["conditionId"]
    return NormalizedMarket(
        platform=Platform.POLYMARKET,
        market_id=condition_id,
        yes_token_id=yes_token_id,
        no_token_id=no_token_id,
        title=(raw_market.get("question") or "").strip() or condition_id,
        category=category,
        resolution_criteria=(raw_market.get("description") or "").strip(),
        resolution_source=(resolution_source or "").strip() or None,
        close_time=raw_market.get("endDate", ""),
        yes_ask=[],
        no_ask=[],
        raw=raw_market,
    )


def _event_category(event: dict, preferred: str = "geopolitics") -> str:
    tag_slugs = [t.get("slug", "") for t in event.get("tags") or []]
    if preferred in tag_slugs:
        return preferred
    return tag_slugs[0] if tag_slugs else ""


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class PolymarketClient(MarketDataClient):
    """Read-only Polymarket adapter: Gamma discovery + CLOB books, no auth."""

    platform = Platform.POLYMARKET

    def __init__(
        self,
        *,
        gamma_base_url: str = GAMMA_BASE_URL,
        clob_base_url: str = CLOB_BASE_URL,
        timeout_sec: float = 10.0,
        backoff_base_sec: float = 1.0,
        max_retries: int = 4,
        max_requests_per_sec: float = 10.0,  # no published budget; be polite (plan §2.2)
        gamma_client: httpx.Client | None = None,
        clob_client: httpx.Client | None = None,
    ) -> None:
        def transport(client: httpx.Client | None, base_url: str) -> RetryingJsonHttp:
            return RetryingJsonHttp(
                client or httpx.Client(base_url=base_url, timeout=timeout_sec),
                backoff_base_sec=backoff_base_sec,
                max_retries=max_retries,
                max_requests_per_sec=max_requests_per_sec,
                error_cls=PolymarketApiError,
            )

        self._gamma = transport(gamma_client, gamma_base_url)
        self._clob = transport(clob_client, clob_base_url)

    def close(self) -> None:
        self._gamma.close()
        self._clob.close()

    # -- MarketDataClient port ----------------------------------------------

    def discover_markets(
        self, categories: Sequence[str], *, max_pages: int | None = None
    ) -> list[NormalizedMarket]:
        """Open binary markets tagged with any of ``categories`` (Gamma tag
        slugs, e.g. ``geopolitics``).

        Skipped: closed/inactive markets, markets without an order book
        (``enableOrderBook`` false), and non-binary/malformed markets.
        Duplicate condition ids across categories are de-duplicated. The
        ingest-stage funnel will count these skips once the pipeline exists
        (M5+); discovery itself stays silent.
        """
        markets: list[NormalizedMarket] = []
        seen: set[str] = set()
        for category in categories:
            tag_slug = category.strip().lower()
            offset = 0
            pages = 0
            while True:
                page = self._gamma.get_json(
                    "/events",
                    {
                        "active": "true",
                        "closed": "false",
                        "tag_slug": tag_slug,
                        "limit": _PAGE_LIMIT,
                        "offset": offset,
                    },
                )
                if not isinstance(page, list):
                    raise PolymarketApiError(
                        f"expected a JSON array from /events, got {type(page).__name__}"
                    )
                for event in page:
                    source = (event.get("resolutionSource") or "").strip() or None
                    for raw_market in event.get("markets") or []:
                        if raw_market.get("closed") or not raw_market.get("active"):
                            continue
                        if not raw_market.get("enableOrderBook"):
                            continue
                        condition_id = raw_market.get("conditionId")
                        if not condition_id or condition_id in seen:
                            continue
                        try:
                            market = normalize_market(
                                raw_market, category=tag_slug, resolution_source=source
                            )
                        except ValueError:
                            continue  # non-binary/malformed
                        seen.add(condition_id)
                        markets.append(market)
                pages += 1
                if len(page) < _PAGE_LIMIT or (
                    max_pages is not None and pages >= max_pages
                ):
                    break
                offset += _PAGE_LIMIT
        return markets

    def fetch_order_book(
        self, market: NormalizedMarket
    ) -> tuple[list[OrderBookLevel], list[OrderBookLevel]]:
        """Live CLOB ask books for both outcomes — one query per TOKEN id."""
        if not market.yes_token_id or not market.no_token_id:
            raise ValueError(
                f"market {market.market_id} lacks outcome token ids — /book takes "
                f"TOKEN ids, not condition ids (plan §2.2)"
            )
        yes_book = self._clob.get_json("/book", {"token_id": market.yes_token_id})
        no_book = self._clob.get_json("/book", {"token_id": market.no_token_id})
        return parse_ask_levels(yes_book.get("asks")), parse_ask_levels(no_book.get("asks"))

    # -- extras (not part of the port) ---------------------------------------

    def get_event_markets(self, slug: str) -> list[NormalizedMarket]:
        """All parseable markets of one event by slug (plan §2.2:
        ``/events?slug=...``). No open/closed filtering — the caller asked
        for this event explicitly."""
        payload = self._gamma.get_json("/events", {"slug": slug})
        markets: list[NormalizedMarket] = []
        for event in payload or []:
            source = (event.get("resolutionSource") or "").strip() or None
            category = _event_category(event)
            for raw_market in event.get("markets") or []:
                try:
                    markets.append(
                        normalize_market(
                            raw_market, category=category, resolution_source=source
                        )
                    )
                except ValueError:
                    continue
        return markets


# ---------------------------------------------------------------------------
# Milestone-3 acceptance smoke (plan §11): print best asks for live markets.
#   .venv/bin/python -m arbdetector.clients.polymarket --limit 3
# ---------------------------------------------------------------------------


def _fmt_best(levels: list[OrderBookLevel]) -> str:
    return f"${levels[0].price} x {levels[0].size}" if levels else "(empty book)"


def _smoke(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Print best YES/NO asks for live Polymarket markets (read-only)."
    )
    parser.add_argument("--slug", help="specific event slug; skips discovery")
    parser.add_argument("--categories", nargs="+", default=["geopolitics"])
    parser.add_argument("--limit", type=int, default=3, help="max markets to print")
    parser.add_argument("--max-pages", type=int, default=1, help="discovery pages per category")
    args = parser.parse_args(argv)

    with PolymarketClient() as client:
        if args.slug:
            markets = client.get_event_markets(args.slug)
        else:
            markets = client.discover_markets(args.categories, max_pages=args.max_pages)
            print(
                f"discovered {len(markets)} open binary markets tagged "
                f"{args.categories} (first {args.max_pages} page(s) per category)"
            )
        for market in markets[: args.limit]:
            yes_ask, no_ask = client.fetch_order_book(market)
            print(f"[{market.market_id[:18]}…] {market.title[:70]}")
            print(f"    category={market.category}  close={market.close_time}")
            print(f"    best YES ask: {_fmt_best(yes_ask)}   best NO ask: {_fmt_best(no_ask)}")


if __name__ == "__main__":
    _smoke()
