"""PolymarketClient: token-id mapping, ask sorting, discovery, books.

All offline — httpx.MockTransport plays Gamma and CLOB using response shapes
captured from the live services on 2026-07-05.
"""

import json
from decimal import Decimal

import httpx
import pytest

import arbdetector.clients.polymarket as pm
from arbdetector.clients.polymarket import (
    PolymarketClient,
    normalize_market,
    parse_ask_levels,
    parse_clob_token_ids,
)
from arbdetector.schema import OrderBookLevel, Platform

D = Decimal

YES_TOKEN = "35097776985291732938"
NO_TOKEN = "87073899845581463485"


def market_fixture(condition_id: str = "0x6bd5", **overrides) -> dict:
    raw = {
        "conditionId": condition_id,
        "question": "Putin out as President of Russia by December 31, 2026?",
        "description": "This market will resolve to Yes if Vladimir Putin ceases to be President...",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps([YES_TOKEN, NO_TOKEN]),
        "endDate": "2026-12-31T12:00:00Z",
        "active": True,
        "closed": False,
        "enableOrderBook": True,
    }
    raw.update(overrides)
    return raw


def event_fixture(slug: str, markets: list[dict], **overrides) -> dict:
    event = {
        "slug": slug,
        "title": slug.replace("-", " "),
        "resolutionSource": "",
        "tags": [{"id": "100265", "slug": "geopolitics"}],
        "markets": markets,
    }
    event.update(overrides)
    return event


class TestParseClobTokenIds:
    def test_maps_by_label_not_position(self):
        # inverted array order must NOT swap the legs
        inverted = market_fixture(
            outcomes=json.dumps(["No", "Yes"]),
            clobTokenIds=json.dumps([NO_TOKEN, YES_TOKEN]),
        )
        assert parse_clob_token_ids(inverted) == (YES_TOKEN, NO_TOKEN)
        assert parse_clob_token_ids(market_fixture()) == (YES_TOKEN, NO_TOKEN)

    def test_accepts_already_decoded_lists(self):
        raw = market_fixture(outcomes=["Yes", "No"], clobTokenIds=[YES_TOKEN, NO_TOKEN])
        assert parse_clob_token_ids(raw) == (YES_TOKEN, NO_TOKEN)

    def test_non_binary_market_refused(self):
        with pytest.raises(ValueError, match="not a binary"):
            parse_clob_token_ids(
                market_fixture(outcomes=json.dumps(["Up", "Down"]))
            )
        with pytest.raises(ValueError, match="not a binary"):
            parse_clob_token_ids(
                market_fixture(clobTokenIds=json.dumps([YES_TOKEN]))
            )

    def test_garbage_stringified_json_refused(self):
        with pytest.raises(ValueError, match="unparseable"):
            parse_clob_token_ids(market_fixture(clobTokenIds="not json"))


class TestParseAskLevels:
    def test_live_descending_order_is_resorted_best_first(self):
        # captured live: CLOB serves asks WORST-first
        raw = [
            {"price": "0.99", "size": "400673.6"},
            {"price": "0.98", "size": "19907.2"},
            {"price": "0.97", "size": "500"},
        ]
        levels = parse_ask_levels(raw)
        assert levels[0] == OrderBookLevel(price=D("0.97"), size=D("500"))
        assert [l.price for l in levels] == [D("0.97"), D("0.98"), D("0.99")]

    def test_empty_and_none(self):
        assert parse_ask_levels(None) == []
        assert parse_ask_levels([]) == []

    def test_float_refused(self):
        with pytest.raises(TypeError, match="float"):
            parse_ask_levels([{"price": 0.97, "size": "500"}])

    def test_out_of_range_refused(self):
        with pytest.raises(ValueError, match="outside"):
            parse_ask_levels([{"price": "97", "size": "500"}])


class TestNormalizeMarket:
    def test_field_mapping(self):
        market = normalize_market(
            market_fixture(), category="geopolitics", resolution_source="Reuters"
        )
        assert market.platform is Platform.POLYMARKET
        assert market.market_id == "0x6bd5"
        assert market.yes_token_id == YES_TOKEN
        assert market.no_token_id == NO_TOKEN
        assert market.title.startswith("Putin out as President")
        assert market.category == "geopolitics"
        assert market.resolution_criteria.startswith("This market will resolve")
        assert market.resolution_source == "Reuters"
        assert market.close_time == "2026-12-31T12:00:00Z"
        assert market.yes_ask == [] and market.no_ask == []

    def test_empty_resolution_source_becomes_none(self):
        market = normalize_market(market_fixture(), category="geopolitics")
        assert market.resolution_source is None


def make_client(gamma_handler=None, clob_handler=None, **kwargs) -> PolymarketClient:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request to {request.url}")

    kwargs.setdefault("backoff_base_sec", 0.0)
    kwargs.setdefault("max_requests_per_sec", 0)
    return PolymarketClient(
        gamma_client=httpx.Client(
            transport=httpx.MockTransport(gamma_handler or unexpected),
            base_url="https://gamma.test",
        ),
        clob_client=httpx.Client(
            transport=httpx.MockTransport(clob_handler or unexpected),
            base_url="https://clob.test",
        ),
        **kwargs,
    )


class TestDiscovery:
    def test_pagination_filtering_and_dedupe(self, monkeypatch):
        monkeypatch.setattr(pm, "_PAGE_LIMIT", 2)
        calls = []

        page_full = [
            event_fixture(
                "putin-out-before-2027",
                [
                    market_fixture("0xaaa"),
                    market_fixture("0xbbb", closed=True),            # skipped
                    market_fixture("0xccc", enableOrderBook=False),  # skipped
                    market_fixture("0xddd", outcomes=json.dumps(["Up", "Down"])),  # non-binary
                ],
                resolutionSource="Kremlin announcements",
            ),
            event_fixture("second-event", [market_fixture("0xaaa")]),  # dup condition id
        ]
        page_short = [event_fixture("third-event", [market_fixture("0xeee")])]

        def gamma(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.url.path == "/events"
            assert request.url.params["tag_slug"] == "geopolitics"
            assert request.url.params["active"] == "true"
            assert request.url.params["closed"] == "false"
            offset = int(request.url.params["offset"])
            return httpx.Response(200, json=page_full if offset == 0 else page_short)

        client = make_client(gamma_handler=gamma)
        markets = client.discover_markets(["Geopolitics"])  # case-insensitive
        assert [m.market_id for m in markets] == ["0xaaa", "0xeee"]
        assert markets[0].resolution_source == "Kremlin announcements"
        assert markets[0].category == "geopolitics"
        assert len(calls) == 2  # short second page ended the walk

    def test_max_pages_bounds_the_scan(self, monkeypatch):
        monkeypatch.setattr(pm, "_PAGE_LIMIT", 1)
        calls = []

        def gamma(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            n = len(calls)
            return httpx.Response(
                200, json=[event_fixture(f"event-{n}", [market_fixture(f"0x{n}")])]
            )

        client = make_client(gamma_handler=gamma)
        markets = client.discover_markets(["geopolitics"], max_pages=3)
        assert len(markets) == 3
        assert len(calls) == 3


class TestFetchOrderBook:
    def test_queries_both_token_ids(self):
        requested = []

        def clob(request: httpx.Request) -> httpx.Response:
            token = request.url.params["token_id"]
            requested.append(token)
            asks = (
                [{"price": "0.99", "size": "10"}, {"price": "0.13", "size": "100"}]
                if token == YES_TOKEN
                else [{"price": "0.88", "size": "50"}]
            )
            return httpx.Response(200, json={"asks": asks, "bids": []})

        client = make_client(clob_handler=clob)
        market = normalize_market(market_fixture(), category="geopolitics")
        yes_ask, no_ask = client.fetch_order_book(market)
        assert requested == [YES_TOKEN, NO_TOKEN]
        assert yes_ask[0] == OrderBookLevel(price=D("0.13"), size=D("100"))
        assert no_ask[0] == OrderBookLevel(price=D("0.88"), size=D("50"))

    def test_missing_token_ids_refused(self):
        client = make_client()
        market = normalize_market(market_fixture(), category="geopolitics")
        market.yes_token_id = None
        with pytest.raises(ValueError, match="TOKEN ids"):
            client.fetch_order_book(market)


class TestGetEventMarkets:
    def test_fetches_by_slug_with_category_from_tags(self):
        def gamma(request: httpx.Request) -> httpx.Response:
            assert request.url.params["slug"] == "putin-out-before-2027"
            return httpx.Response(
                200,
                json=[
                    event_fixture(
                        "putin-out-before-2027",
                        [market_fixture()],
                        tags=[{"slug": "world"}, {"slug": "geopolitics"}],
                    )
                ],
            )

        client = make_client(gamma_handler=gamma)
        markets = client.get_event_markets("putin-out-before-2027")
        assert len(markets) == 1
        assert markets[0].category == "geopolitics"
