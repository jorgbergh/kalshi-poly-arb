"""Pipeline stages, drop-reason vocabulary, and the per-stage funnel report
(plan §9.3–§9.4).

Every stage emits a :class:`StageResult` with the same shape, and the funnel
invariant ``sum(drops.values()) == n_in - n_out`` is enforced at construction:
the funnel can never silently lose an item. Drop reasons are a fixed,
versioned enum — never ad-hoc strings (plan §9.1 principle 4, "the heart of
the whole design").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class Stage(StrEnum):
    """The pipeline stages, in funnel order (plan §9.3).

    Adding a stage = add a value here and emit a StageResult; the board,
    store, and funnel views pick it up with zero display changes.
    """

    INGEST = "ingest"
    RECALL = "recall"
    ADJUDICATE = "adjudicate"
    PRICE = "price"
    THRESHOLD = "threshold"
    ALERT = "alert"


class DropReason(StrEnum):
    """The complete, versioned drop vocabulary (plan §9.4).

    Values are lowercase to match the structured-log examples in plan §9.7.
    New reasons are added HERE, never invented inline.
    """

    # recall
    CATEGORY_MISMATCH = "category_mismatch"    # not in a configured category on both sides
    NO_TIME_OVERLAP = "no_time_overlap"        # close-time windows don't overlap
    LOW_SIMILARITY = "low_similarity"          # below the recall similarity floor
    # adjudicate
    LLM_NOT_SAME_EVENT = "llm_not_same_event"  # LLM judged the events not equivalent
    LOW_CONFIDENCE = "low_confidence"          # same-event but below min_confidence
    MANUAL_REJECT = "manual_reject"            # force-rejected via manual_overrides.yaml
    # price
    EMPTY_BOOK = "empty_book"                  # one side had no orders
    STALE_BOOK = "stale_book"                  # book older than freshness bound
    INSUFFICIENT_DEPTH = "insufficient_depth"  # can't fill even minimum size
    # threshold
    NEGATIVE_MARGIN = "negative_margin"        # net margin <= 0 after fees
    BELOW_THRESHOLD = "below_threshold"        # positive but under alert threshold
    # alert
    DUPLICATE = "duplicate"                    # already alerted, no material change
    # infra (usable from any stage)
    API_ERROR = "api_error"                    # upstream fetch failed this cycle


@dataclass
class StageResult:
    """Standardized per-stage funnel report (plan §9.3).

    Invariants enforced at construction:
    - ``0 <= n_out <= n_in``
    - ``sum(drops.values()) == n_in - n_out``  (nothing vanishes untracked)
    - drop keys are :class:`DropReason` members, counts are non-negative ints
    - ``dropped_ids`` keys must appear in ``drops`` with matching lengths
      (``dropped_ids`` may be empty when id-tracking is disabled)
    """

    stage: Stage
    n_in: int
    n_out: int
    drops: dict[DropReason, int] = field(default_factory=dict)
    dropped_ids: dict[DropReason, list[str]] = field(default_factory=dict)
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.stage, Stage):
            raise TypeError(f"stage must be a Stage enum value, got {self.stage!r}")
        if self.n_in < 0 or self.n_out < 0:
            raise ValueError(f"n_in/n_out must be >= 0, got {self.n_in}/{self.n_out}")
        if self.n_out > self.n_in:
            raise ValueError(f"n_out ({self.n_out}) exceeds n_in ({self.n_in})")
        if self.duration_ms < 0:
            raise ValueError(f"duration_ms must be >= 0, got {self.duration_ms}")

        for reason, count in self.drops.items():
            if not isinstance(reason, DropReason):
                raise TypeError(
                    f"drop key {reason!r} is not a DropReason — reasons are enum "
                    f"values, never freeform strings (plan §9.1)"
                )
            if not isinstance(count, int) or count < 0:
                raise ValueError(f"drop count for {reason} must be an int >= 0, got {count!r}")

        dropped_total = sum(self.drops.values())
        if dropped_total != self.n_in - self.n_out:
            raise ValueError(
                f"funnel invariant violated at stage {self.stage.value!r}: "
                f"sum(drops) = {dropped_total} but n_in - n_out = {self.n_in - self.n_out}"
            )

        for reason, ids in self.dropped_ids.items():
            if not isinstance(reason, DropReason):
                raise TypeError(f"dropped_ids key {reason!r} is not a DropReason")
            if reason not in self.drops:
                raise ValueError(f"dropped_ids lists {reason} but drops does not count it")
            if len(ids) != self.drops[reason]:
                raise ValueError(
                    f"dropped_ids[{reason}] has {len(ids)} ids but drops counts "
                    f"{self.drops[reason]}"
                )

    @property
    def n_dropped(self) -> int:
        return self.n_in - self.n_out

    @classmethod
    def from_drops(
        cls,
        stage: Stage,
        n_in: int,
        drops: Mapping[DropReason, int] | None = None,
        dropped_ids: Mapping[DropReason, list[str]] | None = None,
        duration_ms: float = 0.0,
    ) -> "StageResult":
        """Build a StageResult with ``n_out`` derived from the drop counts.

        Zero-count entries are stripped so persisted funnels stay clean.
        """
        clean_drops = {reason: count for reason, count in (drops or {}).items() if count != 0}
        n_out = n_in - sum(clean_drops.values())
        return cls(
            stage=stage,
            n_in=n_in,
            n_out=n_out,
            drops=clean_drops,
            dropped_ids=dict(dropped_ids or {}),
            duration_ms=duration_ms,
        )
