"""LLM verdict cache (plan §6, §9.8, milestone 6).

This IS the ``verdicts`` table from §9.8, doubling as the stage-2 cache:
keyed ``(pair_id, rules_hash)``, so a pair is re-adjudicated only when either
market's rules text changes, and restarts never re-spend tokens. The price
loop reads this table; it never calls the LLM (plan §4).

Rows are append-only in spirit (INSERT OR REPLACE only fires when a rules
change legitimately produces a new verdict for the same key). Milestone 8
builds the rest of the store around this table.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

# Canonical verdicts DDL lives in the store (§9.8) — one source of truth.
from arbdetector.store.sqlite import VERDICTS_TABLE_SQL as _CREATE_TABLE

# Additive schema-v2 migration (plan §9.10): verdicts written before
# 2026-07-06 predate the column and read back as '' (they were claude-fable-5).
_ADD_MODEL_COLUMN = "ALTER TABLE verdicts ADD COLUMN model TEXT NOT NULL DEFAULT ''"


@dataclass(frozen=True)
class Verdict:
    """One adjudication verdict (the §6 JSON, minus prose)."""

    is_same_event: bool
    confidence: float
    same_direction: bool              # False: YES on kalshi == NO on polymarket
    resolution_caveats: str
    verdict_ts: str


class VerdictCache:
    """SQLite-backed verdict store. One connection, context-managed."""

    def __init__(self, db_path: str | Path, *, schema_version: int = 1) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute(_CREATE_TABLE)
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(verdicts)")}
        if "model" not in columns:
            self._conn.execute(_ADD_MODEL_COLUMN)
        self._conn.commit()
        self._schema_version = schema_version

    def get(self, pair_id: str, rules_hash: str) -> Verdict | None:
        row = self._conn.execute(
            "SELECT is_same_event, confidence, same_direction, caveats, verdict_ts "
            "FROM verdicts WHERE pair_id = ? AND rules_hash = ?",
            (pair_id, rules_hash),
        ).fetchone()
        if row is None:
            return None
        return Verdict(
            is_same_event=bool(row[0]),
            confidence=row[1],
            same_direction=bool(row[2]),
            resolution_caveats=row[3],
            verdict_ts=row[4],
        )

    def put(self, pair_id: str, rules_hash: str, verdict: Verdict, *, model: str) -> None:
        """``model`` records which LLM produced the verdict — audit trail for
        mixed-model caches (verdicts persist across config model changes)."""
        self._conn.execute(
            "INSERT OR REPLACE INTO verdicts "
            "(pair_id, rules_hash, is_same_event, confidence, same_direction, "
            " caveats, verdict_ts, model, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pair_id,
                rules_hash,
                int(verdict.is_same_event),
                verdict.confidence,
                int(verdict.same_direction),
                verdict.resolution_caveats,
                verdict.verdict_ts,
                model,
                self._schema_version,
            ),
        )
        self._conn.commit()

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "VerdictCache":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False
