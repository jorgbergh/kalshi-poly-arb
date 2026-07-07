"""SQLite store (plan §9.8–§9.9, milestone 8).

One file, zero-ops: the append-only ledgers (`cycles`, `stage_stats`,
`drops`, `opportunities`), the entity tables (`markets`, `pairs`), the
`verdicts` table (whose DDL is canonical HERE and imported by the M6 verdict
cache — one source of truth), and the predefined ``v_*`` views loaded from
``views.sql``.

Conventions:
- Every table carries ``schema_version``; migrations are additive (§9.10).
- Money/sizes are stored as TEXT (Decimal strings) — never SQLite REAL.
- ``drops`` has a ``count`` column (additive deviation from the §9.8 sketch):
  per-item rows carry count=1 and an id; when ``keep_dropped_ids`` is off a
  single aggregate row per reason carries the count with a NULL id. Views
  SUM(count) so both shapes read identically.
- ``opportunities`` has a ``book_snapshot_json`` column (additive): plan §8
  requires persisting the full book snapshot with every flagged opportunity.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from arbdetector.schema import ArbOpportunity, NormalizedMarket
from arbdetector.tracking import StageResult, entity_id

VERDICTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS verdicts (
    pair_id        TEXT    NOT NULL,
    rules_hash     TEXT    NOT NULL,
    is_same_event  INTEGER NOT NULL,
    confidence     REAL    NOT NULL,
    same_direction INTEGER NOT NULL,
    caveats        TEXT    NOT NULL,
    verdict_ts     TEXT    NOT NULL,
    model          TEXT    NOT NULL DEFAULT '',
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (pair_id, rules_hash)
)
"""

_TABLES = [
    VERDICTS_TABLE_SQL,
    """
    CREATE TABLE IF NOT EXISTS markets (
        entity_id      TEXT PRIMARY KEY,
        platform       TEXT NOT NULL,
        market_id      TEXT NOT NULL,
        title          TEXT NOT NULL,
        category       TEXT NOT NULL,
        close_time     TEXT NOT NULL,
        first_seen_ts  TEXT NOT NULL,
        last_seen_ts   TEXT NOT NULL,
        schema_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pairs (
        pair_id          TEXT PRIMARY KEY,
        kalshi_entity_id TEXT NOT NULL REFERENCES markets(entity_id),
        poly_entity_id   TEXT NOT NULL REFERENCES markets(entity_id),
        rules_hash       TEXT NOT NULL,
        first_seen_ts    TEXT NOT NULL,
        schema_version   INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS opportunities (
        opp_id             TEXT PRIMARY KEY,
        pair_id            TEXT NOT NULL REFERENCES pairs(pair_id),
        cycle_id           INTEGER NOT NULL REFERENCES cycles(cycle_id),
        direction          TEXT NOT NULL,
        size               TEXT NOT NULL,
        fill_yes           TEXT NOT NULL,
        fill_no            TEXT NOT NULL,
        fee_yes            TEXT NOT NULL,
        fee_no             TEXT NOT NULL,
        net_per_pair       TEXT NOT NULL,
        roi_pct            TEXT NOT NULL,
        detected_ts        TEXT NOT NULL,
        book_snapshot_json TEXT,
        schema_version     INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS drops (
        id                 INTEGER PRIMARY KEY,
        cycle_id           INTEGER NOT NULL,
        stage              TEXT NOT NULL,
        reason             TEXT NOT NULL,
        entity_or_pair_id  TEXT,
        count              INTEGER NOT NULL DEFAULT 1,
        detail_json        TEXT,
        ts                 TEXT NOT NULL,
        schema_version     INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cycles (
        cycle_id       INTEGER PRIMARY KEY,
        started_ts     TEXT NOT NULL,
        ended_ts       TEXT,
        duration_ms    REAL,
        error_count    INTEGER NOT NULL DEFAULT 0,
        schema_version INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stage_stats (
        cycle_id       INTEGER NOT NULL,
        stage          TEXT NOT NULL,
        n_in           INTEGER NOT NULL,
        n_out          INTEGER NOT NULL,
        duration_ms    REAL NOT NULL,
        schema_version INTEGER NOT NULL,
        PRIMARY KEY (cycle_id, stage)
    )
    """,
    # Sent-alert ledger (M9): de-dup memory keyed by (pair_id, direction). One
    # row per delivered alert; `last_alert` reads the most recent per identity
    # so de-dup survives restarts (matters once M10 loops for hours).
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id             INTEGER PRIMARY KEY,
        pair_id        TEXT NOT NULL,
        direction      TEXT NOT NULL,
        opp_id         TEXT NOT NULL,
        net_per_pair   TEXT NOT NULL,
        roi_pct        TEXT NOT NULL,
        size           TEXT NOT NULL,
        cycle_id       INTEGER NOT NULL,
        alerted_ts     TEXT NOT NULL,
        channels       TEXT NOT NULL,
        schema_version INTEGER NOT NULL
    )
    """,
]

_VIEWS_PATH = Path(__file__).parent / "views.sql"


class Store:
    """The persistent tracking backbone. Context-managed, one connection."""

    def __init__(self, db_path: str | Path, *, schema_version: int) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._schema_version = schema_version
        for ddl in _TABLES:
            self._conn.execute(ddl)
        self._conn.executescript(_VIEWS_PATH.read_text(encoding="utf-8"))
        self._conn.commit()

    # -- cycle ledger (§9.9) --------------------------------------------------

    def begin_cycle(self, started_ts: str) -> int:
        cur = self._conn.execute(
            "INSERT INTO cycles (started_ts, schema_version) VALUES (?, ?)",
            (started_ts, self._schema_version),
        )
        self._conn.commit()
        return cur.lastrowid

    def end_cycle(
        self, cycle_id: int, *, ended_ts: str, duration_ms: float, error_count: int = 0
    ) -> None:
        self._conn.execute(
            "UPDATE cycles SET ended_ts = ?, duration_ms = ?, error_count = ? "
            "WHERE cycle_id = ?",
            (ended_ts, duration_ms, error_count, cycle_id),
        )
        self._conn.commit()

    # -- entities --------------------------------------------------------------

    def upsert_markets(
        self, markets: Iterable[NormalizedMarket], *, seen_ts: str
    ) -> None:
        rows = [
            (
                entity_id(m.platform, m.market_id),
                m.platform.value,
                m.market_id,
                m.title,
                m.category,
                m.close_time,
                seen_ts,
                seen_ts,
                self._schema_version,
            )
            for m in markets
        ]
        self._conn.executemany(
            "INSERT INTO markets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(entity_id) DO UPDATE SET "
            "  title = excluded.title, category = excluded.category, "
            "  close_time = excluded.close_time, last_seen_ts = excluded.last_seen_ts",
            rows,
        )
        self._conn.commit()

    def upsert_pair(
        self,
        pair_id: str,
        *,
        kalshi_entity_id: str,
        poly_entity_id: str,
        rules_hash: str,
        first_seen_ts: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO pairs VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pair_id) DO UPDATE SET rules_hash = excluded.rules_hash",
            (
                pair_id,
                kalshi_entity_id,
                poly_entity_id,
                rules_hash,
                first_seen_ts,
                self._schema_version,
            ),
        )
        self._conn.commit()

    # -- funnel ledgers ---------------------------------------------------------

    def record_stage_result(
        self,
        cycle_id: int,
        result: StageResult,
        *,
        ts: str,
        keep_dropped_ids: bool = True,
    ) -> None:
        self._conn.execute(
            "INSERT INTO stage_stats VALUES (?, ?, ?, ?, ?, ?)",
            (
                cycle_id,
                result.stage.value,
                result.n_in,
                result.n_out,
                result.duration_ms,
                self._schema_version,
            ),
        )
        for reason, count in result.drops.items():
            ids = result.dropped_ids.get(reason) if keep_dropped_ids else None
            if ids:
                self._conn.executemany(
                    "INSERT INTO drops (cycle_id, stage, reason, entity_or_pair_id, "
                    " count, detail_json, ts, schema_version) "
                    "VALUES (?, ?, ?, ?, 1, NULL, ?, ?)",
                    [
                        (cycle_id, result.stage.value, reason.value, item, ts,
                         self._schema_version)
                        for item in ids
                    ],
                )
            else:
                self._conn.execute(
                    "INSERT INTO drops (cycle_id, stage, reason, entity_or_pair_id, "
                    " count, detail_json, ts, schema_version) "
                    "VALUES (?, ?, ?, NULL, ?, NULL, ?, ?)",
                    (cycle_id, result.stage.value, reason.value, count, ts,
                     self._schema_version),
                )
        self._conn.commit()

    def record_opportunity(
        self,
        cycle_id: int,
        opportunity: ArbOpportunity,
        *,
        opp_id: str,
        pair_id: str,
        book_snapshot_json: str | None = None,
    ) -> None:
        """§8: every flagged opportunity is persisted with its book snapshot —
        the single most valuable artifact for validating the detector."""
        o = opportunity
        self._conn.execute(
            "INSERT OR REPLACE INTO opportunities VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                opp_id,
                pair_id,
                cycle_id,
                o.direction.value,
                str(o.size),
                str(o.fill_yes),
                str(o.fill_no),
                str(o.fee_yes),
                str(o.fee_no),
                str(o.net_per_pair),
                str(o.roi_pct),
                o.detected_ts,
                book_snapshot_json,
                self._schema_version,
            ),
        )
        self._conn.commit()

    # -- alert de-dup ledger (§8, M9) -------------------------------------------

    def record_alert(
        self,
        cycle_id: int,
        opportunity: ArbOpportunity,
        *,
        opp_id: str,
        pair_id: str,
        channels: Sequence[str],
        alerted_ts: str,
    ) -> None:
        o = opportunity
        self._conn.execute(
            "INSERT INTO alerts (pair_id, direction, opp_id, net_per_pair, roi_pct, "
            " size, cycle_id, alerted_ts, channels, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                pair_id,
                o.direction.value,
                opp_id,
                str(o.net_per_pair),
                str(o.roi_pct),
                str(o.size),
                cycle_id,
                alerted_ts,
                ",".join(channels),
                self._schema_version,
            ),
        )
        self._conn.commit()

    def last_alert(self, pair_id: str, direction: str) -> dict | None:
        """Most recent delivered alert for one (pair_id, direction) — the
        de-dup lookup. None when this arbitrage has never been alerted."""
        row = self._conn.execute(
            "SELECT net_per_pair, roi_pct, size, cycle_id, alerted_ts "
            "FROM alerts WHERE pair_id = ? AND direction = ? "
            "ORDER BY id DESC LIMIT 1",
            (pair_id, direction),
        ).fetchone()
        if row is None:
            return None
        return {
            "net_per_pair": row[0],
            "roi_pct": row[1],
            "size": row[2],
            "cycle_id": row[3],
            "alerted_ts": row[4],
        }

    def trim_dropped_ids(self, *, keep_cycles: int) -> int:
        """Bound db growth (config ``drop_id_retention_cycles``): delete
        per-item drop rows older than the last ``keep_cycles`` cycles.
        Aggregate rows (NULL id) are kept — the breakdown views stay whole."""
        cur = self._conn.execute(
            "DELETE FROM drops WHERE entity_or_pair_id IS NOT NULL AND cycle_id < "
            "(SELECT COALESCE(MAX(cycle_id), 0) FROM cycles) - ?",
            (keep_cycles,),
        )
        self._conn.commit()
        return cur.rowcount

    # -- introspection -----------------------------------------------------------

    def view(self, name: str, where: str = "", params: Sequence = ()) -> list[dict]:
        """Read a v_* view as dicts (the board and tests go through this)."""
        if not name.startswith("v_"):
            raise ValueError(f"not a view: {name!r}")
        cur = self._conn.execute(f"SELECT * FROM {name} {where}", params)
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def stats(self) -> dict:
        """Row counts + file size for RunState.store_stats (§9.5)."""
        tables = ("markets", "pairs", "verdicts", "opportunities", "drops",
                  "cycles", "stage_stats", "alerts")
        counts = {
            table: self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
        self._conn.commit()
        return {"db_bytes": self._path.stat().st_size, **counts}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False
