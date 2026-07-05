"""Funnel invariant: drops sum to n_in - n_out for every stage (plan §9.3, §12)."""

import pytest

from arbdetector.tracking import DropReason, Stage, StageResult


class TestInvariantEnforced:
    def test_valid_result_constructs(self):
        result = StageResult(
            stage=Stage.RECALL,
            n_in=500,
            n_out=137,
            drops={DropReason.LOW_SIMILARITY: 351, DropReason.NO_TIME_OVERLAP: 12},
            duration_ms=41.5,
        )
        assert result.n_dropped == 363

    def test_drops_must_sum_to_n_in_minus_n_out(self):
        with pytest.raises(ValueError, match="funnel invariant"):
            StageResult(
                stage=Stage.RECALL,
                n_in=500,
                n_out=137,
                drops={DropReason.LOW_SIMILARITY: 351},  # 351 != 363
            )

    def test_untracked_loss_rejected_even_with_empty_drops(self):
        # 10 in, 7 out, no reasons given: 3 items vanished untracked
        with pytest.raises(ValueError, match="funnel invariant"):
            StageResult(stage=Stage.PRICE, n_in=10, n_out=7)

    def test_no_drop_stage_is_valid(self):
        result = StageResult(stage=Stage.PRICE, n_in=24, n_out=24)
        assert result.n_dropped == 0

    def test_n_out_cannot_exceed_n_in(self):
        with pytest.raises(ValueError):
            StageResult(stage=Stage.ALERT, n_in=3, n_out=5)

    def test_negative_counts_rejected(self):
        with pytest.raises(ValueError):
            StageResult(stage=Stage.ALERT, n_in=-1, n_out=-1)
        with pytest.raises(ValueError):
            StageResult(stage=Stage.ALERT, n_in=2, n_out=3, drops={DropReason.DUPLICATE: -1})

    def test_negative_duration_rejected(self):
        with pytest.raises(ValueError):
            StageResult(stage=Stage.ALERT, n_in=1, n_out=1, duration_ms=-0.1)


class TestEnumsNotStrings:
    def test_freeform_drop_reason_rejected(self):
        with pytest.raises(TypeError, match="never freeform"):
            StageResult(
                stage=Stage.RECALL,
                n_in=2,
                n_out=1,
                drops={"low_similarity": 1},  # type: ignore[dict-item]
            )

    def test_freeform_stage_rejected(self):
        with pytest.raises(TypeError):
            StageResult(stage="recall", n_in=1, n_out=1)  # type: ignore[arg-type]


class TestDroppedIdsConsistency:
    def test_matching_ids_accepted(self):
        result = StageResult(
            stage=Stage.ADJUDICATE,
            n_in=3,
            n_out=1,
            drops={DropReason.LLM_NOT_SAME_EVENT: 2},
            dropped_ids={DropReason.LLM_NOT_SAME_EVENT: ["3f9a1b2c", "7c12aa01"]},
        )
        assert result.dropped_ids[DropReason.LLM_NOT_SAME_EVENT] == ["3f9a1b2c", "7c12aa01"]

    def test_id_count_must_match_drop_count(self):
        with pytest.raises(ValueError, match="dropped_ids"):
            StageResult(
                stage=Stage.ADJUDICATE,
                n_in=3,
                n_out=1,
                drops={DropReason.LLM_NOT_SAME_EVENT: 2},
                dropped_ids={DropReason.LLM_NOT_SAME_EVENT: ["3f9a1b2c"]},
            )

    def test_ids_for_uncounted_reason_rejected(self):
        with pytest.raises(ValueError, match="dropped_ids"):
            StageResult(
                stage=Stage.ADJUDICATE,
                n_in=3,
                n_out=1,
                drops={DropReason.LLM_NOT_SAME_EVENT: 2},
                dropped_ids={DropReason.LOW_CONFIDENCE: []},
            )

    def test_empty_dropped_ids_is_fine(self):
        # keep_dropped_ids=false in config -> stages simply omit the ids
        StageResult(
            stage=Stage.ADJUDICATE, n_in=3, n_out=1, drops={DropReason.LLM_NOT_SAME_EVENT: 2}
        )


class TestFromDrops:
    def test_derives_n_out(self):
        result = StageResult.from_drops(
            Stage.THRESHOLD,
            n_in=24,
            drops={DropReason.BELOW_THRESHOLD: 19, DropReason.NEGATIVE_MARGIN: 2},
        )
        assert result.n_out == 3
        assert result.n_dropped == 21

    def test_strips_zero_counts(self):
        result = StageResult.from_drops(
            Stage.THRESHOLD,
            n_in=5,
            drops={DropReason.BELOW_THRESHOLD: 0, DropReason.NEGATIVE_MARGIN: 5},
        )
        assert DropReason.BELOW_THRESHOLD not in result.drops
        assert result.n_out == 0


class TestVocabularyComplete:
    def test_stages_in_funnel_order(self):
        assert [s.value for s in Stage] == [
            "ingest", "recall", "adjudicate", "price", "threshold", "alert",
        ]

    def test_all_plan_94_reasons_exist(self):
        expected = {
            "CATEGORY_MISMATCH", "NO_TIME_OVERLAP", "LOW_SIMILARITY",
            "LLM_NOT_SAME_EVENT", "LOW_CONFIDENCE", "MANUAL_REJECT",
            "EMPTY_BOOK", "STALE_BOOK", "INSUFFICIENT_DEPTH",
            "NEGATIVE_MARGIN", "BELOW_THRESHOLD", "DUPLICATE", "API_ERROR",
        }
        assert {r.name for r in DropReason} == expected

    def test_reason_values_match_structured_log_convention(self):
        # plan §9.7 examples use lowercase snake_case in the "reason" field
        assert DropReason.LLM_NOT_SAME_EVENT.value == "llm_not_same_event"
        assert DropReason.BELOW_THRESHOLD.value == "below_threshold"
