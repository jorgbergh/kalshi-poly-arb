"""Orchestration loop mechanics (plan §11, §12) — fully offline.

A fake clock/sleep and a stub `discover` drive `run_loop` with no network and
no real waiting; books come from the unit-tested replay fetcher.
"""

from decimal import Decimal

import pytest

from arbdetector.config import load_config
from arbdetector.engine.signal import PairBooks, replay_fetcher
from arbdetector.fees import build_fee_registry
from arbdetector.main import _fmt_uptime, run_loop
from arbdetector.pipeline import DiscoveryOutcome
from arbdetector.schema import MatchedPair, NormalizedMarket, OrderBookLevel, Platform
from arbdetector.store.sqlite import Store
from arbdetector.tracking import Stage, StageResult
from arbdetector.tracking.ids import matched_pair_id
from tests.conftest import CONFIG_PATH

D = Decimal


class FakeClock:
    """Monotonic clock we advance by hand; `sleep` just adds to it."""

    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def lvl(price, size):
    return OrderBookLevel(price=D(price), size=D(size))


def make_market(platform, market_id, category):
    return NormalizedMarket(
        platform=platform, market_id=market_id, yes_token_id=None, no_token_id=None,
        title=f"Will {market_id}?", category=category, resolution_criteria="rules",
        resolution_source=None, close_time="2026-12-31T00:00:00Z",
        yes_ask=[], no_ask=[], raw={},
    )


def make_pair(n=1):
    return MatchedPair(
        kalshi=make_market(Platform.KALSHI, f"KX-{n}", "World"),
        polymarket=make_market(Platform.POLYMARKET, f"0x{n}", "geopolitics"),
        is_same_event=True, confidence=0.9, same_direction=True,
        resolution_caveats="", verdict_ts="2026-07-07T00:00:00+00:00",
        rules_hash="ab" * 8,
    )


def make_discovery(blessed):
    # trivially funnel-valid stages (n_in == n_out); the loop tests care about
    # cycle mechanics, not the discovery counts
    ingest = StageResult(stage=Stage.INGEST, n_in=100, n_out=100)
    recall = StageResult(stage=Stage.RECALL, n_in=len(blessed), n_out=len(blessed))
    adjud = StageResult(stage=Stage.ADJUDICATE, n_in=len(blessed), n_out=len(blessed))
    markets = [m for mp in blessed for m in (mp.kalshi, mp.polymarket)]
    return DiscoveryOutcome(list(blessed), ingest, recall, adjud, markets,
                          {"api_calls": 0, "cache_hits": 0, "verdicts_in_db": 0})


# a book deep enough for a positive-margin opportunity (NO@kalshi + YES@poly)
def good_books(now):
    return PairBooks(
        kalshi_yes_ask=[lvl("0.13", "1000")], kalshi_no_ask=[lvl("0.06", "1000")],
        poly_yes_ask=[lvl("0.90", "1000")], poly_no_ask=[lvl("0.87", "1000")],
        fetched_at=now,
    )


@pytest.fixture()
def deps(tmp_path):
    config = load_config(CONFIG_PATH).model_copy(update={})
    # point state at tmp
    config = config.model_copy(update={
        "tracking": config.tracking.model_copy(update={
            "state_dir": tmp_path, "sqlite_path": tmp_path / "arb.db",
            "structured_log_path": tmp_path / "events.jsonl",
        })
    })
    store = Store(config.tracking.sqlite_path, schema_version=config.tracking.schema_version)
    registry = build_fee_registry(config.fees)
    return config, store, registry, tmp_path


class RecordingAlerter:
    name = "rec"
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, summary, *, is_update):
        self.sent.append(summary["pair_id"])


class TestRunLoop:
    def test_bounded_run_persists_each_cycle(self, deps):
        config, store, registry, tmp = deps
        clock = FakeClock()
        pair = make_pair()
        from datetime import datetime, timezone
        fetch = replay_fetcher({matched_pair_id(pair): good_books(datetime.now(timezone.utc))})

        completed = run_loop(
            config=config, store=store, alerters=[RecordingAlerter()],
            fee_registry=registry, fetch_books=fetch,
            discover=lambda: make_discovery([pair]),
            max_cycles=3, clock=clock, sleep=clock.sleep,
            price_now=datetime.now(timezone.utc),
        )
        assert completed == 3
        rows = store._conn.execute("SELECT COUNT(*) FROM cycles").fetchone()[0]
        assert rows == 3
        # latest.json rewritten (last cycle_id present)
        assert (tmp / "latest.json").exists() and (tmp / "STATUS.txt").exists()
        cycle_ids = [r[0] for r in store._conn.execute("SELECT cycle_id FROM cycles ORDER BY cycle_id")]
        assert cycle_ids == [1, 2, 3]

    def test_discovery_runs_once_when_interval_not_elapsed(self, deps):
        config, store, registry, tmp = deps
        # price interval 5s, discovery 300s: 3 fast ticks share one discovery
        clock = FakeClock()
        pair = make_pair()
        from datetime import datetime, timezone
        fetch = replay_fetcher({matched_pair_id(pair): good_books(datetime.now(timezone.utc))})
        calls = {"n": 0}

        def discover():
            calls["n"] += 1
            return make_discovery([pair])

        run_loop(
            config=config, store=store, alerters=[RecordingAlerter()],
            fee_registry=registry, fetch_books=fetch, discover=discover,
            max_cycles=3, clock=clock, sleep=clock.sleep,
            price_now=datetime.now(timezone.utc),
        )
        assert calls["n"] == 1  # only the initial discovery; ticks < 300s apart

    def test_discovery_reruns_after_interval(self, deps):
        config, store, registry, tmp = deps
        # force discovery every tick by making the fake clock jump 400s per sleep
        clock = FakeClock()

        def big_sleep(_seconds):
            clock.t += 400  # advance past discovery_interval each tick

        pair = make_pair()
        from datetime import datetime, timezone
        fetch = replay_fetcher({matched_pair_id(pair): good_books(datetime.now(timezone.utc))})
        calls = {"n": 0}

        def discover():
            calls["n"] += 1
            return make_discovery([pair])

        run_loop(
            config=config, store=store, alerters=[RecordingAlerter()],
            fee_registry=registry, fetch_books=fetch, discover=discover,
            max_cycles=3, clock=clock, sleep=big_sleep,
            price_now=datetime.now(timezone.utc),
        )
        assert calls["n"] == 3  # re-discovered each tick (clock jumped 400s)

    def test_discovery_failure_backs_off_and_survives(self, deps):
        config, store, registry, tmp = deps
        clock = FakeClock()
        pair = make_pair()
        from datetime import datetime, timezone
        fetch = replay_fetcher({matched_pair_id(pair): good_books(datetime.now(timezone.utc))})
        attempts = {"n": 0}
        sleeps = []

        def discover():
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("kalshi down")
            return make_discovery([pair])

        def rec_sleep(seconds):
            sleeps.append(seconds)
            clock.t += seconds

        completed = run_loop(
            config=config, store=store, alerters=[RecordingAlerter()],
            fee_registry=registry, fetch_books=fetch, discover=discover,
            max_cycles=1, clock=clock, sleep=rec_sleep,
            price_now=datetime.now(timezone.utc),
        )
        assert completed == 1            # survived the first failure
        assert attempts["n"] == 2        # retried
        assert sleeps[0] == config.poll.backoff_base_sec  # first backoff = base * 2^0

    def test_alert_dedup_across_ticks(self, deps):
        config, store, registry, tmp = deps
        clock = FakeClock()
        pair = make_pair()
        from datetime import datetime, timezone
        fetch = replay_fetcher({matched_pair_id(pair): good_books(datetime.now(timezone.utc))})
        sink = RecordingAlerter()

        run_loop(
            config=config, store=store, alerters=[sink],
            fee_registry=registry, fetch_books=fetch,
            discover=lambda: make_discovery([pair]),
            max_cycles=3, clock=clock, sleep=clock.sleep,
            price_now=datetime.now(timezone.utc),
        )
        # opportunity is identical every tick -> alerted once, then DUPLICATE
        assert len(sink.sent) == 1
        alert_rows = store._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        assert alert_rows == 1

    def test_stop_check_halts_loop(self, deps):
        config, store, registry, tmp = deps
        clock = FakeClock()
        pair = make_pair()
        from datetime import datetime, timezone
        fetch = replay_fetcher({matched_pair_id(pair): good_books(datetime.now(timezone.utc))})
        completed = run_loop(
            config=config, store=store, alerters=[RecordingAlerter()],
            fee_registry=registry, fetch_books=fetch,
            discover=lambda: make_discovery([pair]),
            max_cycles=None, clock=clock, sleep=clock.sleep,
            stop_check=lambda: True,  # stop before the first cycle
            price_now=datetime.now(timezone.utc),
        )
        assert completed == 0


class TestUptimeFormat:
    def test_formats(self):
        assert _fmt_uptime(5) == "5s"
        assert _fmt_uptime(65) == "1m05s"
        assert _fmt_uptime(3 * 3600 + 12 * 60) == "3h12m"
