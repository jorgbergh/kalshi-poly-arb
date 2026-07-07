"""RunState — the single source of truth per cycle (plan §9.5, milestone 8).

Serialized atomically to ``state/latest.json`` every cycle (temp file +
``os.replace`` — a reader never sees a half-written file). Every view — the
status board, any future dashboard — renders from THIS object; no subsystem
maintains its own divergent idea of "what's happening" (§9.1 principle 1).

Serialization notes:
- Decimals serialize as strings, so money precision survives the round trip.
- ``ArbOpportunity`` serializes as a board-ready SUMMARY (ids, titles,
  numbers, caveats) — not the full nested §5 object graph with raw payloads
  and books. The full book snapshot lives in the store
  (``opportunities.book_snapshot_json``), where §8 requires it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from arbdetector.schema import ArbOpportunity
from arbdetector.tracking.ids import matched_pair_id, opp_id
from arbdetector.tracking.stages import StageResult


@dataclass
class RunState:
    """Canonical state of the world for one cycle (§9.5). If it's worth
    showing, it lives here."""

    schema_version: int
    cycle_id: int
    started_ts: str                      # process start (for uptime)
    cycle_ts: str                        # this cycle's timestamp
    funnel: list[StageResult]            # ordered ingest -> alert
    active_opportunities: list[ArbOpportunity]
    health: dict = field(default_factory=dict)       # per-source status
    cache_stats: dict = field(default_factory=dict)  # verdict-cache size + hit rate
    store_stats: dict = field(default_factory=dict)  # db size, row counts


def _stage_to_dict(result: StageResult) -> dict:
    return {
        "stage": result.stage.value,
        "n_in": result.n_in,
        "n_out": result.n_out,
        "drops": {reason.value: count for reason, count in result.drops.items()},
        "dropped_ids": {
            reason.value: list(ids) for reason, ids in result.dropped_ids.items()
        },
        "duration_ms": round(result.duration_ms, 3),
    }


def opportunity_summary(opportunity: ArbOpportunity) -> dict:
    """Board-ready summary of one opportunity — ids, titles, exact numbers."""
    pair = opportunity.pair
    pid = matched_pair_id(pair)
    return {
        "opp_id": opp_id(pid, opportunity.direction, opportunity.detected_ts),
        "pair_id": pid,
        "kalshi_title": pair.kalshi.title,
        "poly_title": pair.polymarket.title,
        "direction": opportunity.direction.value,
        "same_direction": pair.same_direction,
        "size": str(opportunity.size),
        "fill_yes": str(opportunity.fill_yes),
        "fill_no": str(opportunity.fill_no),
        "fee_yes": str(opportunity.fee_yes),
        "fee_no": str(opportunity.fee_no),
        "net_per_pair": str(opportunity.net_per_pair),
        "roi_pct": str(opportunity.roi_pct),
        "confidence": pair.confidence,
        "resolution_caveats": pair.resolution_caveats,
        "detected_ts": opportunity.detected_ts,
    }


def to_dict(state: RunState) -> dict:
    """The exact JSON shape of ``state/latest.json``."""
    return {
        "schema_version": state.schema_version,
        "cycle_id": state.cycle_id,
        "started_ts": state.started_ts,
        "cycle_ts": state.cycle_ts,
        "funnel": [_stage_to_dict(result) for result in state.funnel],
        "active_opportunities": [
            opportunity_summary(opportunity)
            for opportunity in state.active_opportunities
        ],
        "health": state.health,
        "cache_stats": state.cache_stats,
        "store_stats": state.store_stats,
    }


def write_atomic(state: RunState, path: str | Path) -> None:
    """Write ``latest.json`` atomically: temp file in the same directory,
    then ``os.replace`` — a concurrent reader sees the old file or the new
    one, never a torn write (§9.5)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    temp.write_text(
        json.dumps(to_dict(state), indent=1, default=str) + "\n", encoding="utf-8"
    )
    os.replace(temp, target)


def read_runstate(path: str | Path) -> dict:
    """Load a persisted RunState as a plain dict (renderers don't need the
    live objects back — the JSON is the contract)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))
