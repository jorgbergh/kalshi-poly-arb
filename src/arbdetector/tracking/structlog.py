"""JSON-lines structured logger (Milestone 8, plan §9.7).

STUB. Will emit one typed JSON object per line to ``state/events.jsonl``.
Mandatory keys on every line: ``ts``, ``lvl``, ``stage``, ``event``, plus
``entity_id``/``pair_id`` and ``reason`` where applicable — so one grep on a
pair_id traces its entire life. No freeform string logs for anything that is
part of the pipeline.

(Named per the plan's layout; unrelated to the PyPI ``structlog`` package.)
"""
