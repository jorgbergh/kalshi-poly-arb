"""Every v_* view returns the expected shape from a seeded store
(plan §9.8, §12)."""

from decimal import Decimal

import pytest

from arbdetector.matching.cache import Verdict, VerdictCache
from arbdetector.schema import Platform
from arbdetector.store.sqlite import Store
from arbdetector.tracking import DropReason, Stage, StageResult, entity_id
from tests.test_runstate_serialization import make_opportunity

D = Decimal

TS = "2026-07-06T12:00:00+00:00"


@pytest.fixture()
def seeded(tmp_path):
    """A store holding one full cycle: markets, pair, verdict, stage stats,
    drops (both shapes), and one opportunity."""
    db = tmp_path / "arb.db"
    opportunity = make_opportunity()
    pair = opportunity.pair
    kalshi_eid = entity_id(Platform.KALSHI, pair.kalshi.market_id)
    poly_eid = entity_id(Platform.POLYMARKET, pair.polymarket.market_id)
    from arbdetector.tracking.ids import matched_pair_id

    pid = matched_pair_id(pair)

    store = Store(db, schema_version=2)
    cycle_id = store.begin_cycle(TS)
    store.upsert_markets([pair.kalshi, pair.polymarket], seen_ts=TS)
    store.upsert_pair(
        pid,
        kalshi_entity_id=kalshi_eid,
        poly_entity_id=poly_eid,
        rules_hash=pair.rules_hash,
        first_seen_ts=TS,
    )
    with VerdictCache(db, schema_version=2) as cache:
        cache.put(
            pid,
            pair.rules_hash,
            Verdict(True, 0.85, True, "evaluation times differ by 2h", TS),
            model="claude-sonnet-5",
        )
    store.record_stage_result(
        cycle_id,
        StageResult.from_drops(
            Stage.RECALL,
            n_in=100,
            drops={DropReason.LOW_SIMILARITY: 60},
            dropped_ids={DropReason.LOW_SIMILARITY: [f"id{i:06d}" for i in range(60)]},
            duration_ms=12.0,
        ),
        ts=TS,
        keep_dropped_ids=True,  # per-item rows
    )
    store.record_stage_result(
        cycle_id,
        StageResult.from_drops(
            Stage.THRESHOLD,
            n_in=20,
            drops={DropReason.BELOW_THRESHOLD: 5},
            duration_ms=1.0,
        ),
        ts=TS,
        keep_dropped_ids=False,  # one aggregate row, count=5
    )
    store.record_opportunity(
        cycle_id,
        opportunity,
        opp_id="abc123def456",
        pair_id=pid,
        book_snapshot_json='{"kalshi_yes_ask": []}',
    )
    store.end_cycle(cycle_id, ended_ts=TS, duration_ms=6410.0)
    yield store, cycle_id, pid
    store.close()


class TestViews:
    def test_v_funnel_latest(self, seeded):
        store, _, _ = seeded
        rows = {row["stage"]: row for row in store.view("v_funnel_latest")}
        assert rows["recall"]["n_in"] == 100
        assert rows["recall"]["dropped"] == 60
        assert rows["threshold"]["n_out"] == 15

    def test_v_active_opportunities_joins_titles_and_verdict(self, seeded):
        store, cycle_id, pid = seeded
        rows = store.view("v_active_opportunities")
        assert len(rows) == 1
        row = rows[0]
        assert row["pair_id"] == pid and row["cycle_id"] == cycle_id
        assert row["kalshi_title"] == "Will KX-1 happen?"
        assert row["poly_title"] == "Will 0x1 happen?"
        assert row["confidence"] == 0.85
        assert "differ by 2h" in row["caveats"]
        assert row["net_per_pair"] == "0.0486"  # money stays TEXT

    def test_v_drop_breakdown_sums_both_row_shapes(self, seeded):
        store, _, _ = seeded
        rows = {(r["stage"], r["reason"]): r["n"] for r in store.view("v_drop_breakdown_24h")}
        assert rows[("recall", "low_similarity")] == 60   # 60 per-item rows
        assert rows[("threshold", "below_threshold")] == 5  # 1 aggregate row

    def test_v_pair_trace_interleaves_kinds(self, seeded):
        store, _, pid = seeded
        rows = store.view("v_pair_trace", "WHERE pair_id = ?", (pid,))
        kinds = {row["kind"] for row in rows}
        assert kinds == {"verdict", "opportunity"}
        verdict_row = next(r for r in rows if r["kind"] == "verdict")
        assert "model=claude-sonnet-5" in verdict_row["detail"]

    def test_v_opportunity_history(self, seeded):
        store, _, _ = seeded
        rows = store.view("v_opportunity_history")
        assert len(rows) == 1
        assert rows[0]["kalshi_title"] == "Will KX-1 happen?"

    def test_v_cycle_health_counts_opportunities(self, seeded):
        store, cycle_id, _ = seeded
        rows = store.view("v_cycle_health")
        assert rows[0]["cycle_id"] == cycle_id
        assert rows[0]["n_opportunities"] == 1
        assert rows[0]["duration_ms"] == 6410.0


class TestStoreMechanics:
    def test_market_upsert_updates_last_seen(self, seeded):
        store, _, _ = seeded
        market = make_opportunity().pair.kalshi
        store.upsert_markets([market], seen_ts="2026-07-07T00:00:00+00:00")
        row = store._conn.execute(
            "SELECT first_seen_ts, last_seen_ts FROM markets WHERE market_id = 'KX-1'"
        ).fetchone()
        assert row == (TS, "2026-07-07T00:00:00+00:00")

    def test_trim_keeps_aggregate_rows(self, seeded):
        store, _, _ = seeded
        deleted = store.trim_dropped_ids(keep_cycles=0)
        # cycle 1 is the latest cycle, so nothing is older than the window
        assert deleted == 0
        remaining = store._conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0]
        assert remaining == 61

    def test_stats_shape(self, seeded):
        store, _, _ = seeded
        stats = store.stats()
        assert stats["markets"] == 2
        assert stats["opportunities"] == 1
        assert stats["db_bytes"] > 0

    def test_view_name_guard(self, seeded):
        store, _, _ = seeded
        with pytest.raises(ValueError):
            store.view("markets")
