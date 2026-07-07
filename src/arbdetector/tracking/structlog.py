"""JSON-lines structured logger (plan §9.7, milestone 8).

One typed event per line in ``state/events.jsonl`` — greppable, jq-able,
loadable into the store. Mandatory keys on every line: ``ts``, ``lvl``,
``stage``, ``event``; ``entity_id``/``pair_id``/``reason`` where applicable.
No freeform string logs for anything that is part of the pipeline.

(Named per the plan's layout; unrelated to the PyPI ``structlog`` package.)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from arbdetector.tracking.stages import Stage, StageResult


class StructuredLogger:
    """Append-only JSONL writer. Cheap to construct; safe to reuse."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, stage: Stage | str, event: str, *, lvl: str = "info", **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "lvl": lvl,
            "stage": stage.value if isinstance(stage, Stage) else str(stage),
            "event": event,
            **fields,
        }
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")

    def log_stage(self, result: StageResult, **fields) -> None:
        """One ``stage`` line with the counts, plus one ``drop`` line per
        reason — the uniformity that makes a pair_id traceable by grep."""
        self.log(
            result.stage,
            "stage",
            n_in=result.n_in,
            n_out=result.n_out,
            duration_ms=round(result.duration_ms, 1),
            **fields,
        )
        for reason, count in result.drops.items():
            self.log(result.stage, "drop", reason=reason.value, count=count, **fields)
