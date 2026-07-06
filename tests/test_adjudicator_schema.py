"""Adjudicator: verdict JSON parses & validates; cache-first; drop accounting.

All offline — the Anthropic client is a fake; no tokens are spent here.
"""

from types import SimpleNamespace

import pytest

from arbdetector.matching.adjudicator import (
    AdjudicationError,
    Adjudicator,
    load_overrides,
    parse_verdict,
    run_adjudicate,
)
from arbdetector.matching.cache import Verdict, VerdictCache
from arbdetector.matching.recall import CandidatePair
from arbdetector.schema import NormalizedMarket, Platform
from arbdetector.tracking import DropReason, entity_id, pair_id
from arbdetector.tracking.ids import rules_hash

VALID_JSON = (
    '{"is_same_event": true, "confidence": 0.92, '
    '"resolution_caveats": "close times differ by 29h", "same_direction": false}'
)


def make_market(platform: Platform, market_id: str, title: str, rules: str) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        market_id=market_id,
        yes_token_id=None,
        no_token_id=None,
        title=title,
        category="geopolitics",
        resolution_criteria=rules,
        resolution_source="Reuters",
        close_time="2026-12-31T00:00:00Z",
        yes_ask=[],
        no_ask=[],
        raw={},
    )


def make_pair(n: int = 1) -> CandidatePair:
    kalshi = make_market(Platform.KALSHI, f"KX-{n}", f"Will event {n} happen?", f"rules K{n}")
    poly = make_market(Platform.POLYMARKET, f"0x{n}", f"Event {n} happens?", f"rules P{n}")
    return CandidatePair(
        pair_id=pair_id(
            entity_id(Platform.KALSHI, kalshi.market_id),
            entity_id(Platform.POLYMARKET, poly.market_id),
        ),
        kalshi=kalshi,
        polymarket=poly,
        similarity=0.9,
    )


class FakeClient:
    """Stands in for anthropic.Anthropic: scripted text replies (or exceptions)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])


class TestParseVerdict:
    def test_valid_json(self):
        verdict = parse_verdict(VALID_JSON, verdict_ts="2026-07-06T00:00:00+00:00")
        assert verdict.is_same_event is True
        assert verdict.confidence == 0.92
        assert verdict.same_direction is False
        assert verdict.resolution_caveats == "close times differ by 29h"

    def test_fenced_or_prose_wrapped_json_is_extracted(self):
        assert parse_verdict(f"```json\n{VALID_JSON}\n```").confidence == 0.92
        assert parse_verdict(f"Here is my verdict: {VALID_JSON} Thank you.").confidence == 0.92

    @pytest.mark.parametrize(
        "bad",
        [
            "the events are the same",                                     # no JSON at all
            '{"is_same_event": true, "confidence": 0.9}',                  # missing keys
            VALID_JSON.replace("0.92", "1.5"),                             # confidence out of range
            VALID_JSON[:-1] + ', "extra_key": 1}',                         # unknown key
            '{"is_same_event": "yes", "confidence": 0.9, "resolution_caveats": "", "same_direction": true}',
        ],
    )
    def test_bad_verdicts_never_guessed(self, bad):
        with pytest.raises(AdjudicationError):
            parse_verdict(bad)


class TestVerdictCache:
    def test_round_trip_and_persistence(self, tmp_path):
        db = tmp_path / "arb.db"
        verdict = Verdict(True, 0.9, False, "caveat", "2026-07-06T00:00:00+00:00")
        with VerdictCache(db) as cache:
            assert cache.get("aaaa", "hash1") is None
            cache.put("aaaa", "hash1", verdict, model="test-model")
            assert cache.get("aaaa", "hash1") == verdict
        # reopen: restarts never re-spend tokens (plan §6)
        with VerdictCache(db) as cache:
            assert cache.get("aaaa", "hash1") == verdict
            assert cache.count() == 1

    def test_rules_change_is_a_miss(self, tmp_path):
        with VerdictCache(tmp_path / "arb.db") as cache:
            cache.put("aaaa", "hash1", Verdict(True, 0.9, True, "", "ts"), model="m")
            assert cache.get("aaaa", "hash2") is None

    def test_model_recorded_and_pre_v2_dbs_migrated(self, tmp_path):
        import sqlite3

        db = tmp_path / "arb.db"
        # simulate a pre-v2 database (no model column)
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE verdicts (pair_id TEXT NOT NULL, rules_hash TEXT NOT NULL,"
            " is_same_event INTEGER NOT NULL, confidence REAL NOT NULL,"
            " same_direction INTEGER NOT NULL, caveats TEXT NOT NULL,"
            " verdict_ts TEXT NOT NULL, schema_version INTEGER NOT NULL,"
            " PRIMARY KEY (pair_id, rules_hash))"
        )
        conn.execute(
            "INSERT INTO verdicts VALUES ('old1', 'h', 1, 0.9, 1, '', 'ts', 1)"
        )
        conn.commit()
        conn.close()

        with VerdictCache(db, schema_version=2) as cache:
            assert cache.get("old1", "h") is not None  # old rows survive migration
            cache.put("new1", "h", Verdict(True, 0.9, True, "", "ts"), model="claude-sonnet-5")
            row = cache._conn.execute(
                "SELECT model, schema_version FROM verdicts WHERE pair_id='new1'"
            ).fetchone()
            assert row == ("claude-sonnet-5", 2)


class TestRulesHash:
    def test_deterministic_and_separator_safe(self):
        assert rules_hash("a", "b") == rules_hash("a", "b")
        assert rules_hash("ab", "") != rules_hash("a", "b")
        assert len(rules_hash("a", "b")) == 16


class TestAdjudicatorCacheFirst:
    def test_second_call_hits_cache_not_api(self, tmp_path):
        client = FakeClient([VALID_JSON])
        with VerdictCache(tmp_path / "arb.db") as cache:
            adj = Adjudicator(model="test-model", cache=cache, client=client)
            pair = make_pair()
            verdict1, from_cache1 = adj.adjudicate(pair)
            verdict2, from_cache2 = adj.adjudicate(pair)
        assert (from_cache1, from_cache2) == (False, True)
        assert len(client.calls) == 1
        assert verdict1 == verdict2
        assert adj.api_calls == 1 and adj.cache_hits == 1

    def test_prompt_contains_both_markets_facts(self, tmp_path):
        client = FakeClient([VALID_JSON])
        with VerdictCache(tmp_path / "arb.db") as cache:
            adj = Adjudicator(model="test-model", cache=cache, client=client)
            adj.adjudicate(make_pair())
        call = client.calls[0]
        assert call["model"] == "test-model"
        prompt = call["messages"][0]["content"]
        for fragment in ("Will event 1 happen?", "Event 1 happens?", "rules K1", "rules P1"):
            assert fragment in prompt

    def test_malformed_reply_raises_not_guesses(self, tmp_path):
        client = FakeClient(["I think they are the same event."])
        with VerdictCache(tmp_path / "arb.db") as cache:
            adj = Adjudicator(model="test-model", cache=cache, client=client)
            with pytest.raises(AdjudicationError):
                adj.adjudicate(make_pair())
            assert cache.count() == 0  # bad replies are never cached


def verdict_json(is_same: bool, confidence: float, same_direction: bool = True) -> str:
    return (
        f'{{"is_same_event": {str(is_same).lower()}, "confidence": {confidence}, '
        f'"resolution_caveats": "", "same_direction": {str(same_direction).lower()}}}'
    )


class TestRunAdjudicate:
    def test_funnel_accounting(self, tmp_path):
        pairs = [make_pair(n) for n in range(1, 6)]
        overrides = {pairs[3].pair_id: "reject"}
        client = FakeClient(
            [
                verdict_json(True, 0.92, same_direction=False),  # blessed, inverted
                verdict_json(False, 0.95),                       # not same event
                verdict_json(True, 0.55),                        # low confidence
                RuntimeError("api down"),                        # api error (pair 5)
            ]
        )
        with VerdictCache(tmp_path / "arb.db") as cache:
            adj = Adjudicator(model="test-model", cache=cache, client=client)
            blessed, result = run_adjudicate(
                pairs, adjudicator=adj, min_confidence=0.80, overrides=overrides
            )
        assert len(blessed) == 1
        assert blessed[0].same_direction is False
        assert blessed[0].confidence == 0.92
        assert blessed[0].rules_hash == rules_hash("rules K1", "rules P1")
        assert result.n_in == 5 and result.n_out == 1
        assert result.drops == {
            DropReason.LLM_NOT_SAME_EVENT: 1,
            DropReason.LOW_CONFIDENCE: 1,
            DropReason.MANUAL_REJECT: 1,
            DropReason.API_ERROR: 1,
        }

    def test_approve_override_skips_api(self, tmp_path):
        pair = make_pair()
        client = FakeClient([])  # any API call would pop from an empty list
        with VerdictCache(tmp_path / "arb.db") as cache:
            adj = Adjudicator(model="test-model", cache=cache, client=client)
            blessed, result = run_adjudicate(
                [pair],
                adjudicator=adj,
                min_confidence=0.80,
                overrides={pair.pair_id: "approve_inverted"},
            )
        assert len(blessed) == 1
        assert blessed[0].same_direction is False
        assert "manual override" in blessed[0].resolution_caveats
        assert client.calls == []


class TestLoadOverrides:
    def test_missing_file_is_empty(self, tmp_path):
        assert load_overrides(tmp_path / "nope.yaml") == {}

    def test_valid_file(self, tmp_path):
        p = tmp_path / "manual_overrides.yaml"
        p.write_text("overrides:\n  aaaa1111: approve\n  bbbb2222: reject\n")
        assert load_overrides(p) == {"aaaa1111": "approve", "bbbb2222": "reject"}

    def test_unknown_value_fails_loudly(self, tmp_path):
        p = tmp_path / "manual_overrides.yaml"
        p.write_text("overrides:\n  aaaa1111: maybe\n")
        with pytest.raises(ValueError, match="maybe"):
            load_overrides(p)
