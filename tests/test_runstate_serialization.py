"""RunState round-trips through JSON; atomic write; board + JSONL renderers
(plan §9.5–§9.7, §12)."""

import json
from decimal import Decimal

from arbdetector.schema import (
    ArbOpportunity,
    Direction,
    MatchedPair,
    NormalizedMarket,
    Platform,
)
from arbdetector.tracking import DropReason, Stage, StageResult
from arbdetector.tracking.runstate import (
    RunState,
    opportunity_summary,
    read_runstate,
    to_dict,
    write_atomic,
)
from arbdetector.tracking.statusboard import render_board, write_board
from arbdetector.tracking.structlog import StructuredLogger

D = Decimal


def make_market(platform: Platform, market_id: str) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        market_id=market_id,
        yes_token_id=None,
        no_token_id=None,
        title=f"Will {market_id} happen?",
        category="geopolitics",
        resolution_criteria="rules",
        resolution_source=None,
        close_time="2026-12-31T00:00:00Z",
        yes_ask=[],
        no_ask=[],
        raw={},
    )


def make_opportunity() -> ArbOpportunity:
    pair = MatchedPair(
        kalshi=make_market(Platform.KALSHI, "KX-1"),
        polymarket=make_market(Platform.POLYMARKET, "0x1"),
        is_same_event=True,
        confidence=0.85,
        same_direction=True,
        resolution_caveats="evaluation times differ by 2h",
        verdict_ts="2026-07-06T00:00:00+00:00",
        rules_hash="ab" * 8,
    )
    return ArbOpportunity(
        pair=pair,
        direction=Direction.NO_KALSHI_YES_POLY,
        size=D("500"),
        fill_yes=D("0.0060"),
        fill_no=D("0.9416"),
        fee_yes=D("0"),
        fee_no=D("2.10"),
        net_per_pair=D("0.0486"),
        roi_pct=D("5.10"),
        detected_ts="2026-07-06T12:00:00+00:00",
    )


def make_state() -> RunState:
    funnel = [
        StageResult(stage=Stage.INGEST, n_in=14189, n_out=14189),
        StageResult.from_drops(
            Stage.RECALL,
            n_in=14189,
            drops={DropReason.NO_TIME_OVERLAP: 10639, DropReason.LOW_SIMILARITY: 3277},
            duration_ms=296.0,
        ),
        StageResult.from_drops(
            Stage.THRESHOLD,
            n_in=20,
            drops={DropReason.NEGATIVE_MARGIN: 13, DropReason.BELOW_THRESHOLD: 5},
            duration_ms=1.2,
        ),
    ]
    return RunState(
        schema_version=2,
        cycle_id=7,
        started_ts="2026-07-06T12:00:00+00:00",
        cycle_ts="2026-07-06T12:03:00+00:00",
        funnel=funnel,
        active_opportunities=[make_opportunity()],
        health={"kalshi": "ok", "polymarket": "ok"},
        cache_stats={"cache_hits": 244, "api_calls": 0},
        store_stats={"db_bytes": 1_200_000, "opportunities": 4},
    )


class TestToDict:
    def test_money_serializes_as_strings(self):
        payload = to_dict(make_state())
        opp = payload["active_opportunities"][0]
        assert opp["net_per_pair"] == "0.0486"
        assert opp["size"] == "500"
        assert opp["fill_no"] == "0.9416"

    def test_funnel_uses_enum_values(self):
        payload = to_dict(make_state())
        recall = payload["funnel"][1]
        assert recall["stage"] == "recall"
        assert recall["drops"] == {"no_time_overlap": 10639, "low_similarity": 3277}
        assert recall["n_out"] == 273

    def test_summary_carries_ids_and_caveats(self):
        summary = opportunity_summary(make_opportunity())
        assert len(summary["pair_id"]) == 8
        assert len(summary["opp_id"]) == 12
        assert summary["resolution_caveats"] == "evaluation times differ by 2h"
        assert summary["direction"] == "NO@kalshi+YES@poly"

    def test_whole_payload_is_json_serializable(self):
        json.dumps(to_dict(make_state()))  # would raise on Decimal leakage


class TestAtomicWrite:
    def test_round_trip(self, tmp_path):
        state = make_state()
        path = tmp_path / "state" / "latest.json"
        write_atomic(state, path)
        assert read_runstate(path) == to_dict(state)

    def test_replace_leaves_no_temp_files(self, tmp_path):
        path = tmp_path / "latest.json"
        write_atomic(make_state(), path)
        second = make_state()
        second.cycle_id = 8
        write_atomic(second, path)
        assert read_runstate(path)["cycle_id"] == 8
        assert [p.name for p in tmp_path.iterdir()] == ["latest.json"]


class TestStatusBoard:
    def test_renders_every_stage_generically(self):
        text = render_board(make_state())
        for stage_name in ("ingest", "recall", "threshold"):
            assert stage_name in text
        assert "NO_TIME_OVERLAP (10639)" in text  # top drop reason
        assert "ARB DETECTOR   cycle #00007" in text

    def test_opportunity_row_and_health(self):
        text = render_board(make_state())
        assert "$+0.0486" in text
        assert "NO@kalshi+YES@poly" in text
        assert "kalshi ok" in text

    def test_empty_state_renders(self):
        state = make_state()
        state.funnel = []
        state.active_opportunities = []
        text = render_board(state)
        assert "(none)" in text

    def test_write_board(self, tmp_path):
        path = tmp_path / "STATUS.txt"
        text = write_board(make_state(), path)
        assert path.read_text(encoding="utf-8") == text


class TestStructuredLogger:
    def test_mandatory_keys_and_append(self, tmp_path):
        path = tmp_path / "events.jsonl"
        logger = StructuredLogger(path)
        logger.log(Stage.ADJUDICATE, "verdict", pair_id="3f9a1b2c", confidence=0.41)
        StructuredLogger(path).log("infra", "backoff", lvl="warn", attempt=2)

        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert len(lines) == 2
        for record in lines:
            for key in ("ts", "lvl", "stage", "event"):
                assert key in record
        assert lines[0]["stage"] == "adjudicate"
        assert lines[0]["pair_id"] == "3f9a1b2c"
        assert lines[1]["lvl"] == "warn"

    def test_log_stage_emits_stage_and_drop_lines(self, tmp_path):
        path = tmp_path / "events.jsonl"
        result = StageResult.from_drops(
            Stage.THRESHOLD,
            n_in=20,
            drops={DropReason.BELOW_THRESHOLD: 5, DropReason.NEGATIVE_MARGIN: 13},
        )
        StructuredLogger(path).log_stage(result, cycle_id=7)
        lines = [json.loads(line) for line in path.read_text().splitlines()]
        assert [r["event"] for r in lines] == ["stage", "drop", "drop"]
        assert lines[0]["n_in"] == 20 and lines[0]["cycle_id"] == 7
        assert {r.get("reason") for r in lines[1:]} == {
            "below_threshold",
            "negative_margin",
        }
