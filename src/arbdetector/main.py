"""Orchestration loop (plan §11, milestone 10).

Turns the one-shot sweep into a long-running detector with two cadences from
config (§15 ``poll``): a slow **discovery** cadence that refreshes the
LLM-blessed pair set, and a fast **price** cadence that re-prices that set and
alerts. The blessed set is held in memory between discoveries; the discovery
stage-results are carried forward onto price-only ticks so the board always
shows the full six-stage funnel.

Detector only (plan §14): read-only market data, alerts out. No trading.

Run it::

    python -m arbdetector.main                 # unbounded daemon
    python -m arbdetector.main --once          # a single cycle
    python -m arbdetector.main --max-cycles 3  # bounded (used for verification)
    python -m arbdetector.main --replay FILE   # price from recorded books (offline)
"""

from __future__ import annotations

import argparse
import signal
import time
from datetime import datetime
from typing import Callable

from arbdetector.config import AppConfig, load_config
from arbdetector.engine.signal import BookFetcher
from arbdetector.pipeline import (
    DiscoveryOutcome,
    now_iso,
    persist_cycle,
    run_price_alert,
)
from arbdetector.tracking.structlog import StructuredLogger


def _fmt_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _fmt_ago(seconds: float | None) -> str:
    if seconds is None:
        return "never"
    return f"{_fmt_uptime(seconds)} ago"


def run_loop(
    *,
    config: AppConfig,
    store,
    alerters,
    fee_registry,
    fetch_books: BookFetcher,
    discover: Callable[[], DiscoveryOutcome],
    logger: StructuredLogger | None = None,
    price_now: datetime | None = None,
    books_mode: str = "live",
    max_cycles: int | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop_check: Callable[[], bool] | None = None,
) -> int:
    """The detector's core loop. Returns the number of cycles completed.

    Everything time- and IO-related is injectable (``clock``, ``sleep``,
    ``fetch_books``, ``discover``) so the loop is unit-testable with no network
    and no real waiting. ``discover`` refreshes the blessed set on the slow
    cadence; the fast cadence re-prices it every ``poll.price_interval_sec``.
    """
    logger = logger or StructuredLogger(config.tracking.structured_log_path)
    process_start = clock()
    process_start_ts = now_iso()
    last_discovery_at: float | None = None
    discovery: DiscoveryOutcome | None = None
    failures = 0
    completed = 0

    while max_cycles is None or completed < max_cycles:
        if stop_check is not None and stop_check():
            break
        tick = clock()
        cycle_ts = now_iso()
        try:
            if (
                last_discovery_at is None
                or tick - last_discovery_at >= config.poll.discovery_interval_sec
            ):
                discovery = discover()
                last_discovery_at = tick

            assert discovery is not None
            cycle_id = store.begin_cycle(cycle_ts)
            price = run_price_alert(
                discovery.blessed,
                fetch_books=fetch_books,
                store=store,
                alerters=alerters,
                fee_registry=fee_registry,
                config=config,
                cycle_id=cycle_id,
                ts=cycle_ts,
                price_now=price_now,
            )
            health = {
                "kalshi": "ok",
                "polymarket": "ok",
                "books": books_mode,
                "last_discovery": _fmt_ago(tick - last_discovery_at),
                "errors": failures,
            }
            persist_cycle(
                store,
                logger,
                cycle_id=cycle_id,
                funnel=discovery.stages + price.stages,
                discovery=discovery,
                price=price,
                config=config,
                started_ts=process_start_ts,
                cycle_ts=cycle_ts,
                duration_ms=(clock() - tick) * 1000,
                health=health,
                uptime=_fmt_uptime(clock() - process_start),
            )
            failures = 0
            completed += 1
        except Exception as exc:  # network flakiness must not kill the daemon
            failures += 1
            logger.log("infra", "error", lvl="error", error=repr(exc), failures=failures)
            # cap the exponent so a persistent failure saturates at max_backoff
            # instead of overflowing — the daemon keeps retrying, never crashes
            delay = min(
                config.poll.backoff_base_sec * (2 ** min(failures - 1, 30)),
                config.poll.max_backoff_sec,
            )
            sleep(delay)
            continue

        if max_cycles is None or completed < max_cycles:
            sleep(max(0.0, config.poll.price_interval_sec - (clock() - tick)))

    return completed


def main(argv: list[str] | None = None) -> None:
    from dotenv import load_dotenv

    from arbdetector.alerting import build_alerters
    from arbdetector.clients.kalshi import KalshiClient
    from arbdetector.clients.polymarket import PolymarketClient
    from arbdetector.engine.signal import (
        live_book_fetcher,
        load_recordings,
        replay_fetcher,
    )
    from arbdetector.fees import build_fee_registry
    from arbdetector.matching.cache import VerdictCache
    from arbdetector.pipeline import run_discovery
    from arbdetector.store.sqlite import Store
    from arbdetector.tracking.statusboard import write_board

    parser = argparse.ArgumentParser(
        description="Cross-platform prediction-market arbitrage DETECTOR — the "
        "orchestration loop (read-only; never trades)."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--max-cycles", type=int, help="stop after N cycles")
    parser.add_argument(
        "--replay", metavar="FILE", help="price from recorded books (discovery still live)"
    )
    args = parser.parse_args(argv)

    load_dotenv()
    config = load_config(args.config)
    max_cycles = 1 if args.once else args.max_cycles

    kalshi_client = KalshiClient()
    poly_client = PolymarketClient()
    store = Store(config.tracking.sqlite_path, schema_version=config.tracking.schema_version)
    cache = VerdictCache(
        config.tracking.sqlite_path, schema_version=config.tracking.schema_version
    )
    alerters = build_alerters(config.alerting)
    fee_registry = build_fee_registry(config.fees)
    logger = StructuredLogger(config.tracking.structured_log_path)

    price_now = None
    books_mode = "live"
    if args.replay:
        recordings = load_recordings(args.replay)
        fetch_books = replay_fetcher(recordings)
        books_mode = "replay"
        if recordings:  # freshness relative to the recording's own clock
            price_now = max(b.fetched_at for b in recordings.values())
    else:
        fetch_books = live_book_fetcher(kalshi_client, poly_client)

    def discover() -> DiscoveryOutcome:
        return run_discovery(kalshi_client, poly_client, cache=cache, config=config)

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.__setitem__("flag", True))

    print(f"arbdetector loop starting (discovery {config.poll.discovery_interval_sec}s / "
          f"price {config.poll.price_interval_sec}s, books={books_mode}). Ctrl-C to stop.")
    try:
        completed = run_loop(
            config=config,
            store=store,
            alerters=alerters,
            fee_registry=fee_registry,
            fetch_books=fetch_books,
            discover=discover,
            logger=logger,
            price_now=price_now,
            books_mode=books_mode,
            max_cycles=max_cycles,
            stop_check=lambda: stop["flag"],
        )
        print(f"\nloop finished — {completed} cycle(s) completed.")
    except KeyboardInterrupt:
        print("\nshutting down (Ctrl-C).")
    finally:
        for closeable in (kalshi_client, poly_client, cache, store, *alerters):
            close = getattr(closeable, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    main()
