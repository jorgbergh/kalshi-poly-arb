"""Shared pipeline stages (milestone 10).

Extracted so the orchestration loop (:mod:`arbdetector.main`) and the
adjudicator sweep share ONE implementation of discovery, pricing/alerting, and
— most importantly — cycle persistence (the correctness-critical store +
RunState + board + JSONL block that both must get right).

These functions are print-free: the only user-facing side effect is whatever
alerter sinks are injected (the console sink prints each alert). Callers that
want verbose stage logging (the sweep) print around these calls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from arbdetector.alerting import Alerter, run_alert
from arbdetector.config import AppConfig
from arbdetector.engine.signal import (
    BookFetcher,
    DirectionQuote,
    run_price,
    run_threshold,
)
from arbdetector.fees.base import FeeRegistry
from arbdetector.matching.adjudicator import Adjudicator, load_overrides, run_adjudicate
from arbdetector.matching.cache import VerdictCache
from arbdetector.matching.recall import run_recall
from arbdetector.schema import ArbOpportunity, MatchedPair, NormalizedMarket, Platform
from arbdetector.store.sqlite import Store
from arbdetector.tracking import Stage, StageResult, entity_id
from arbdetector.tracking.ids import matched_pair_id
from arbdetector.tracking.runstate import RunState, write_atomic
from arbdetector.tracking.statusboard import write_board
from arbdetector.tracking.structlog import StructuredLogger
from arbdetector.tracking.ids import opp_id


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Discovery (slow cadence): ingest -> recall -> adjudicate
# ---------------------------------------------------------------------------


@dataclass
class DiscoveryOutcome:
    blessed: list[MatchedPair]
    ingest_result: StageResult
    recall_result: StageResult
    adjudicate_result: StageResult
    markets: list[NormalizedMarket]  # both platforms, for the store
    cache_stats: dict

    @property
    def stages(self) -> list[StageResult]:
        return [self.ingest_result, self.recall_result, self.adjudicate_result]


def run_discovery(
    kalshi_client,
    poly_client,
    *,
    cache: VerdictCache,
    config: AppConfig,
    overrides_path: str | Path = "manual_overrides.yaml",
    title_filter: str | None = None,
    candidate_limit: int | None = None,
) -> DiscoveryOutcome:
    """Refresh the LLM-blessed pair set from live market data.

    Runs on the slow cadence (``poll.discovery_interval_sec``): discover both
    platforms, recall candidates, adjudicate (cache-first). Verdicts persist
    in ``cache``, so re-runs only spend tokens on genuinely new/changed pairs.

    ``title_filter`` / ``candidate_limit`` are debug conveniences (the sweep's
    ``--filter`` / ``--limit``): they narrow the candidate set before
    adjudication, so the ``recall`` stage counts stay pre-filter while
    ``adjudicate`` counts reflect the narrowed set.
    """
    kalshi_markets = kalshi_client.discover_markets(config.categories.kalshi)
    poly_markets = poly_client.discover_markets(config.categories.polymarket)
    markets = list(kalshi_markets) + list(poly_markets)
    ingest = StageResult(stage=Stage.INGEST, n_in=len(markets), n_out=len(markets))

    candidates, recall_result = run_recall(
        kalshi_markets, poly_markets, matching=config.matching, categories=config.categories
    )
    if title_filter:
        needle = title_filter.lower()
        candidates = [
            c for c in candidates
            if needle in c.kalshi.title.lower() or needle in c.polymarket.title.lower()
        ]
    if candidate_limit is not None:
        candidates = candidates[:candidate_limit]
    adjudicator = Adjudicator(model=config.matching.llm_model, cache=cache)
    blessed, adjudicate_result = run_adjudicate(
        candidates,
        adjudicator=adjudicator,
        min_confidence=config.matching.min_confidence,
        overrides=load_overrides(overrides_path),
    )
    cache_stats = {
        "api_calls": adjudicator.api_calls,
        "cache_hits": adjudicator.cache_hits,
        "verdicts_in_db": cache.count(),
    }
    return DiscoveryOutcome(
        blessed, ingest, recall_result, adjudicate_result, markets, cache_stats
    )


# ---------------------------------------------------------------------------
# Price / threshold / alert (fast cadence)
# ---------------------------------------------------------------------------


@dataclass
class PriceOutcome:
    opportunities: list[ArbOpportunity]
    emitted: list[ArbOpportunity]
    priced: list[tuple[MatchedPair, DirectionQuote]]
    price_result: StageResult
    threshold_result: StageResult
    alert_result: StageResult
    send_errors: int

    @property
    def stages(self) -> list[StageResult]:
        return [self.price_result, self.threshold_result, self.alert_result]


def run_price_alert(
    blessed: Sequence[MatchedPair],
    *,
    fetch_books: BookFetcher,
    store: Store,
    alerters: Sequence[Alerter],
    fee_registry: FeeRegistry,
    config: AppConfig,
    cycle_id: int,
    ts: str,
    price_now: datetime | None = None,
) -> PriceOutcome:
    """Walk books, threshold, and alert the blessed set for one cycle."""
    priced, price_result = run_price(
        blessed,
        fetch_books=fetch_books,
        target_size=config.engine.target_size_pairs,
        min_size=config.engine.min_size_pairs,
        max_book_age_sec=config.engine.max_book_age_sec,
        fee_registry=fee_registry,
        now=price_now,
    )
    opportunities, threshold_result = run_threshold(
        priced, threshold=config.engine.net_threshold_per_pair, detected_ts=ts
    )
    emitted, alert_result, send_errors = run_alert(
        opportunities,
        store=store,
        alerters=alerters,
        material_delta=config.alerting.material_change_per_pair,
        cycle_id=cycle_id,
        ts=ts,
    )
    return PriceOutcome(
        opportunities, emitted, priced, price_result, threshold_result, alert_result,
        send_errors,
    )


# ---------------------------------------------------------------------------
# Cycle persistence — the one correctness-critical block both callers share
# ---------------------------------------------------------------------------


def _book_snapshot_json(opportunity: ArbOpportunity) -> str:
    p = opportunity.pair
    return json.dumps({
        "kalshi_yes_ask": [[str(l.price), str(l.size)] for l in p.kalshi.yes_ask],
        "kalshi_no_ask": [[str(l.price), str(l.size)] for l in p.kalshi.no_ask],
        "poly_yes_ask": [[str(l.price), str(l.size)] for l in p.polymarket.yes_ask],
        "poly_no_ask": [[str(l.price), str(l.size)] for l in p.polymarket.no_ask],
    })


def persist_cycle(
    store: Store,
    logger: StructuredLogger,
    *,
    cycle_id: int,
    funnel: Sequence[StageResult],
    discovery: DiscoveryOutcome,
    price: PriceOutcome,
    config: AppConfig,
    started_ts: str,
    cycle_ts: str,
    duration_ms: float,
    health: dict,
    uptime: str = "—",
) -> dict:
    """Write the whole cycle: store ledgers, RunState, board, JSONL log.

    Returns ``store_stats`` (for the caller's next RunState / logging).
    """
    store.upsert_markets(discovery.markets, seen_ts=cycle_ts)
    for mp in discovery.blessed:
        store.upsert_pair(
            matched_pair_id(mp),
            kalshi_entity_id=entity_id(Platform.KALSHI, mp.kalshi.market_id),
            poly_entity_id=entity_id(Platform.POLYMARKET, mp.polymarket.market_id),
            rules_hash=mp.rules_hash,
            first_seen_ts=cycle_ts,
        )
    for stage_result in funnel:
        store.record_stage_result(
            cycle_id, stage_result, ts=cycle_ts,
            keep_dropped_ids=config.tracking.keep_dropped_ids,
        )
    for opp in price.opportunities:
        store.record_opportunity(
            cycle_id, opp,
            opp_id=opp_id(matched_pair_id(opp.pair), opp.direction, opp.detected_ts),
            pair_id=matched_pair_id(opp.pair),
            book_snapshot_json=_book_snapshot_json(opp),
        )
    store.trim_dropped_ids(keep_cycles=config.tracking.drop_id_retention_cycles)
    store.end_cycle(
        cycle_id, ended_ts=now_iso(), duration_ms=duration_ms,
        error_count=price.send_errors,
    )
    store_stats = store.stats()

    state = RunState(
        schema_version=config.tracking.schema_version,
        cycle_id=cycle_id,
        started_ts=started_ts,
        cycle_ts=cycle_ts,
        funnel=list(funnel),
        active_opportunities=price.opportunities,
        health=health,
        cache_stats=discovery.cache_stats,
        store_stats=store_stats,
    )
    state_dir = Path(config.tracking.state_dir)
    write_atomic(state, state_dir / "latest.json")
    write_board(
        state, state_dir / "STATUS.txt",
        stdout=config.tracking.status_board_stdout, uptime=uptime,
    )
    for stage_result in funnel:
        logger.log_stage(stage_result, cycle_id=cycle_id)
    for opp in price.emitted:  # only actually-delivered alerts (§9.7)
        logger.log(
            Stage.ALERT, "emit",
            pair_id=matched_pair_id(opp.pair),
            opp_id=opp_id(matched_pair_id(opp.pair), opp.direction, opp.detected_ts),
            direction=opp.direction.value,
            net_per_pair=str(opp.net_per_pair),
            roi_pct=str(opp.roi_pct),
        )
    return store_stats
