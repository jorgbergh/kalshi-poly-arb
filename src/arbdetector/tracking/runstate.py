"""RunState — the single source of truth per cycle (Milestone 8, plan §9.5).

STUB. Will implement the ``RunState`` dataclass (schema_version, cycle_id,
timestamps, ordered funnel of StageResults, active opportunities, health,
cache/store stats) plus atomic JSON serialization to ``state/latest.json``
(temp file + rename, so a reader never sees a half-written file). Every view
— status board, logs, any future dashboard — renders from this one object.
"""
