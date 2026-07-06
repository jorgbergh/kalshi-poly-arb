"""KalshiClient: discovery filtering, pagination, normalization, retry/backoff.

All offline — httpx.MockTransport plays the API using response shapes
captured from the live service on 2026-07-05.
"""

from decimal import Decimal

import httpx
import pytest

from arbdetector.clients.kalshi import KalshiApiError, KalshiClient
from arbdetector.schema import Platform

D = Decimal


def make_client(handler, **kwargs) -> KalshiClient:
    http_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://kalshi.test/trade-api/v2",
    )
    kwargs.setdefault("backoff_base_sec", 0.0)
    kwargs.setdefault("max_requests_per_sec", 0)  # no throttling in tests
    return KalshiClient(http_client=http_client, **kwargs)


def market_fixture(ticker: str, **overrides) -> dict:
    raw = {
        "ticker": ticker,
        "event_ticker": ticker.rsplit("-", 1)[0],
        "market_type": "binary",
        "title": "Will the US issue a Level 4 travel advisory for Taiwan?",
        "yes_sub_title": "Before Jan 1, 2027",
        "rules_primary": "If the State Department issues a Level 4 advisory, resolves YES.",
        "rules_secondary": "Source: travel.state.gov.",
        "close_time": "2026-12-31T23:59:00Z",
        "status": "open",
    }
    raw.update(overrides)
    return raw


EVENTS_PAGE_1 = {
    "cursor": "next-page",
    "events": [
        {
            "event_ticker": "KXTAIWANLVL4",
            "category": "World",
            "settlement_sources": [{"name": "the U.S. State Department", "url": "https://x"}],
            "markets": [
                market_fixture("KXTAIWANLVL4-27JAN01"),
                # excluded: multivariate parlay leg
                market_fixture("KXMVE-COMBO-1", mve_collection_ticker="KXMVE-R"),
                # excluded: not binary
                market_fixture("KXTAIWANLVL4-SCALAR", market_type="scalar"),
            ],
        },
        {
            "event_ticker": "KXNBAFINALS",
            "category": "Sports",  # excluded: category not configured
            "markets": [market_fixture("KXNBAFINALS-BOS")],
        },
    ],
}

EVENTS_PAGE_2 = {
    "cursor": "",
    "events": [
        {
            "event_ticker": "KXG7LEADEROUT",
            "category": "Politics",
            "settlement_sources": [],
            "markets": [market_fixture("KXG7LEADEROUT-MACRON", title="Which G7 leader will leave next?", yes_sub_title="Macron")],
        },
    ],
}

ORDERBOOK_PAYLOAD = {
    "orderbook_fp": {
        "yes_dollars": [["0.4000", "150.00"]],
        "no_dollars": [["0.5500", "100.00"], ["0.5400", "200.00"]],
    }
}


class TestDiscovery:
    def make_paging_client(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.url.path.endswith("/events")
            assert request.url.params["with_nested_markets"] == "true"
            assert request.url.params["status"] == "open"
            if request.url.params.get("cursor") == "next-page":
                return httpx.Response(200, json=EVENTS_PAGE_2)
            return httpx.Response(200, json=EVENTS_PAGE_1)

        return make_client(handler), calls

    def test_filters_categories_and_market_types_across_pages(self):
        client, calls = self.make_paging_client()
        markets = client.discover_markets(["World", "Politics"])
        assert [m.market_id for m in markets] == [
            "KXTAIWANLVL4-27JAN01",
            "KXG7LEADEROUT-MACRON",
        ]
        assert len(calls) == 2  # followed the cursor exactly once

    def test_category_matching_is_case_insensitive(self):
        client, _ = self.make_paging_client()
        markets = client.discover_markets(["world", "POLITICS"])
        assert len(markets) == 2
        # normalized category keeps the API's label, not the config casing
        assert markets[0].category == "World"

    def test_max_pages_bounds_the_scan(self):
        client, calls = self.make_paging_client()
        markets = client.discover_markets(["World", "Politics"], max_pages=1)
        assert [m.market_id for m in markets] == ["KXTAIWANLVL4-27JAN01"]
        assert len(calls) == 1

    def test_normalization_fields(self):
        client, _ = self.make_paging_client()
        market = client.discover_markets(["World"])[0]
        assert market.platform is Platform.KALSHI
        assert market.yes_token_id is None and market.no_token_id is None
        assert market.title == (
            "Will the US issue a Level 4 travel advisory for Taiwan? — Before Jan 1, 2027"
        )
        assert market.resolution_criteria == (
            "If the State Department issues a Level 4 advisory, resolves YES. "
            "Source: travel.state.gov."
        )
        assert market.resolution_source == "the U.S. State Department"
        assert market.close_time == "2026-12-31T23:59:00Z"
        assert market.yes_ask == [] and market.no_ask == []
        assert market.raw["ticker"] == "KXTAIWANLVL4-27JAN01"


class TestFetchOrderBook:
    def test_end_to_end_parse_and_derivation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path.endswith("/markets/KXTAIWANLVL4-27JAN01/orderbook")
            return httpx.Response(200, json=ORDERBOOK_PAYLOAD)

        from arbdetector.clients.kalshi import normalize_market

        client = make_client(handler)
        normalized = normalize_market(
            market_fixture("KXTAIWANLVL4-27JAN01"), category="World"
        )
        yes_ask, no_ask = client.fetch_order_book(normalized)
        assert yes_ask[0].price == D("0.4500") and yes_ask[0].size == D("100.00")
        assert no_ask[0].price == D("0.6000") and no_ask[0].size == D("150.00")


class TestRetries:
    def test_backs_off_on_429_then_succeeds(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) < 3:
                return httpx.Response(429)
            return httpx.Response(200, json=ORDERBOOK_PAYLOAD)

        client = make_client(handler)
        payload = client._get_json("/markets/X/orderbook")
        assert len(attempts) == 3
        assert "orderbook_fp" in payload

    def test_gives_up_after_max_retries(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(500)

        client = make_client(handler, max_retries=2)
        with pytest.raises(KalshiApiError, match="failed after 3 attempts"):
            client._get_json("/events")
        assert len(attempts) == 3

    def test_client_errors_raise_immediately_without_retry(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(404)

        client = make_client(handler)
        with pytest.raises(httpx.HTTPStatusError):
            client._get_json("/markets/NOSUCH/orderbook")
        assert len(attempts) == 1


class TestGetMarket:
    def test_combines_market_and_event_endpoints(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/markets/KXTAIWANLVL4-27JAN01"):
                return httpx.Response(
                    200, json={"market": market_fixture("KXTAIWANLVL4-27JAN01")}
                )
            if request.url.path.endswith("/events/KXTAIWANLVL4"):
                return httpx.Response(
                    200,
                    json={
                        "event": {
                            "event_ticker": "KXTAIWANLVL4",
                            "category": "World",
                            "settlement_sources": [{"name": "the U.S. State Department"}],
                        }
                    },
                )
            raise AssertionError(f"unexpected path {request.url.path}")

        client = make_client(handler)
        market = client.get_market("KXTAIWANLVL4-27JAN01")
        assert market.market_id == "KXTAIWANLVL4-27JAN01"
        assert market.category == "World"
        assert market.resolution_source == "the U.S. State Department"
