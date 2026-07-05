"""Observability & state tracking (plan §9) — the stable spine.

The pipeline is a reason-coded funnel: every stage emits a
:class:`~arbdetector.tracking.stages.StageResult`, every item carries a
deterministic ID from :mod:`~arbdetector.tracking.ids`, and every drop uses a
:class:`~arbdetector.tracking.stages.DropReason` enum value. Everything else
(RunState, status board, JSONL log, SQLite store) renders from these.
"""

from arbdetector.tracking.ids import entity_id, opp_id, pair_id
from arbdetector.tracking.stages import DropReason, Stage, StageResult

__all__ = ["DropReason", "Stage", "StageResult", "entity_id", "opp_id", "pair_id"]
