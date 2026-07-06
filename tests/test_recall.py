"""Recall filter: golden cases from live specimens, drop accounting, determinism.

Titles/close times are real captures from both platforms (2026-07-05).
"""

import pytest

from arbdetector.config import CategoriesConfig, MatchingConfig
from arbdetector.matching.recall import normalize_tokens, run_recall
from arbdetector.schema import NormalizedMarket, Platform
from arbdetector.tracking import DropReason


def make_market(platform: Platform, market_id: str, title: str, close_time: str,
                category: str) -> NormalizedMarket:
    return NormalizedMarket(
        platform=platform,
        market_id=market_id,
        yes_token_id=None if platform is Platform.KALSHI else "yes-token",
        no_token_id=None if platform is Platform.KALSHI else "no-token",
        title=title,
        category=category,
        resolution_criteria="rules text",
        resolution_source=None,
        close_time=close_time,
        yes_ask=[],
        no_ask=[],
        raw={},
    )


def kalshi(market_id, title, close, category="Politics"):
    return make_market(Platform.KALSHI, market_id, title, close, category)


def poly(market_id, title, close, category="geopolitics"):
    return make_market(Platform.POLYMARKET, market_id, title, close, category)


# real specimens (2026-07-05)
K_ZP_MEET = kalshi(
    "KXZELENSKYPUTIN-29-27",
    "Volodymyr Zelenskyy and Vladimir Putin meet before Jan 1, 2027? — Before 2027",
    "2027-01-01T04:59:00Z",
)
P_ZP_NOT_MEET = poly(
    "0x032e40c7",
    "Will Zelenskyy and Putin not meet before 2027?",
    "2026-12-31T00:00:00Z",
)
K_TURKEY = kalshi(
    "KXPUTINZELENSKYYLOCATION-28-TUR",
    "Will Putin and Zelenskyy meet next in Turkey?",
    "2028-12-31T04:59:00Z",  # two-year window mismatch vs poly
)
P_TURKEY = poly(
    "0x9af4aa93",
    "Will Zelenskyy and Putin meet next in Turkey before 2027?",
    "2026-12-31T00:00:00Z",
)
K_FED = kalshi("KXFEDCUT-27MAR", "Will the Fed cut rates in March 2027?", "2027-01-01T00:00:00Z")


CONFIG = MatchingConfig(
    recall_top_k=5,
    recall_min_similarity=0.30,
    close_time_tolerance_days=30,
    llm_model="test-model",
    min_confidence=0.8,
)
CATEGORIES = CategoriesConfig(
    kalshi=["World", "Politics", "Elections"], polymarket=["geopolitics"]
)


def recall(kalshi_markets, poly_markets, *, matching=CONFIG, categories=CATEGORIES):
    return run_recall(kalshi_markets, poly_markets, matching=matching, categories=categories)


class TestNormalizeTokens:
    def test_spelling_variants_share_a_token(self):
        # Zelensky vs Zelenskyy is a real cross-platform wart
        assert set(normalize_tokens("Zelensky meets")) & set(
            normalize_tokens("Zelenskyy meeting")
        ) == {"zelens"}

    def test_stopwords_removed_but_not_polarity(self):
        tokens = normalize_tokens("Will they not meet before the end?")
        assert "will" not in tokens and "the" not in tokens
        assert "not" in tokens  # polarity must never block an inverted pair


class TestGoldenPairs:
    def test_inverted_phrasing_is_recalled(self):
        # the same_direction=false case MUST survive recall (plan §6)
        candidates, result = recall([K_ZP_MEET], [P_ZP_NOT_MEET])
        assert len(candidates) == 1
        assert candidates[0].similarity >= CONFIG.recall_min_similarity
        assert result.n_in == 2 and result.n_out == 2
        assert result.drops == {}

    def test_window_mismatch_drops_as_no_time_overlap(self):
        # Turkey pair: same wording, close times two years apart
        candidates, result = recall([K_TURKEY], [P_TURKEY])
        assert candidates == []
        assert result.drops == {DropReason.NO_TIME_OVERLAP: 2}

    def test_unrelated_titles_drop_as_low_similarity(self):
        # same close window, nothing in common
        candidates, result = recall([K_FED], [P_ZP_NOT_MEET])
        assert candidates == []
        assert result.drops == {DropReason.LOW_SIMILARITY: 2}

    def test_mixed_universe(self):
        candidates, result = recall(
            [K_ZP_MEET, K_TURKEY, K_FED], [P_ZP_NOT_MEET, P_TURKEY]
        )
        # K_ZP_MEET recalls BOTH poly markets: the inverted twin AND the
        # time-compatible "meet in Turkey" SUBSET event. Correct: recall
        # over-generates plausible candidates; rejecting the subset relation
        # is the adjudicator's job (plan §6 stage 2).
        assert {c.kalshi.market_id for c in candidates} == {"KXZELENSKYPUTIN-29-27"}
        assert {c.polymarket.market_id for c in candidates} == {"0x032e40c7", "0x9af4aa93"}
        # survivors: K_ZP_MEET + both poly markets; K_TURKEY drops on time
        # (its 2028 close matches nothing), K_FED on similarity
        assert result.n_in == 5 and result.n_out == 3
        assert result.drops == {
            DropReason.NO_TIME_OVERLAP: 1,
            DropReason.LOW_SIMILARITY: 1,
        }


class TestBlocking:
    def test_unconfigured_category_drops_defensively(self):
        stray = kalshi("KXNBA-1", "Zelenskyy and Putin meet before 2027?",
                       "2026-12-31T00:00:00Z", category="Sports")
        candidates, result = recall([stray], [P_ZP_NOT_MEET])
        assert candidates == []
        assert result.drops[DropReason.CATEGORY_MISMATCH] == 1

    def test_unparseable_close_time_drops_on_time(self):
        broken = kalshi("KXBROKEN", "Zelenskyy and Putin meet before 2027?", "")
        candidates, result = recall([broken], [P_ZP_NOT_MEET])
        assert candidates == []
        assert result.drops[DropReason.NO_TIME_OVERLAP] == 2  # both sides unmatched

    def test_tolerance_is_configurable(self):
        tight = CONFIG.model_copy(update={"close_time_tolerance_days": 0})
        candidates, _ = recall([K_ZP_MEET], [P_ZP_NOT_MEET], matching=tight)
        assert candidates == []  # 29h apart > 0-day tolerance


class TestTopKAndDeterminism:
    def test_top_k_caps_candidates_per_kalshi_market(self):
        variants = [
            poly(f"0xvar{i}", f"Will Zelenskyy and Putin meet in location {i} before 2027?",
                 "2026-12-31T00:00:00Z")
            for i in range(8)
        ]
        matching = CONFIG.model_copy(update={"recall_top_k": 3})
        candidates, result = recall([K_ZP_MEET], variants, matching=matching)
        assert len(candidates) == 3
        # non-selected variants had time overlap but didn't make the cut
        assert result.drops[DropReason.LOW_SIMILARITY] == 5

    def test_deterministic_output(self):
        markets = ([K_ZP_MEET, K_TURKEY, K_FED], [P_ZP_NOT_MEET, P_TURKEY])
        first, _ = recall(*markets)
        second, _ = recall(*markets)
        assert [(c.pair_id, c.similarity) for c in first] == [
            (c.pair_id, c.similarity) for c in second
        ]

    def test_dropped_ids_lengths_match_counts(self):
        _, result = recall([K_ZP_MEET, K_TURKEY, K_FED], [P_ZP_NOT_MEET, P_TURKEY])
        for reason, count in result.drops.items():
            assert len(result.dropped_ids[reason]) == count

    def test_funnel_invariant_holds(self):
        # StageResult would raise at construction if it didn't; assert anyway
        _, result = recall([K_ZP_MEET, K_TURKEY, K_FED], [P_ZP_NOT_MEET, P_TURKEY])
        assert sum(result.drops.values()) == result.n_in - result.n_out
