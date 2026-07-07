# STATE_SCHEMA.md — registry of everything the detector tracks

First draft per plan §9.10. This is the human-facing map of every entity, enum,
stage, drop reason, table, and view. **Current `schema_version`: 2.**

Version history: **v2** (2026-07-06) — `verdicts` gained a `model` column
recording which LLM produced each verdict (config switched fable-5 → sonnet-5
mid-cache; pre-v2 rows read back as `''` and were all `claude-fable-5`).

Change protocol (plan §9.10): additions are additive — add the enum value /
column / table, bump `schema_version`, append here. Old rows keep their version.
Nothing is ever repurposed or silently reshaped.

Status legend: **[M1]** implemented now · **[Mn]** planned for milestone *n* (plan §11).

---

## 1. Deterministic identifiers — `tracking/ids.py` [M1]

Same inputs → same ID, across restarts. These appear on the status board, in
every structured log line, and as keys/foreign keys in every table.

| ID | Formula | Length | Identifies |
|---|---|---|---|
| `entity_id` | `sha1("{platform}:{market_id}")[:8]` | 8 hex | one market on one platform |
| `pair_id` | `sha1("{kalshi_entity_id}:{poly_entity_id}")[:8]` (kalshi first, fixed order) | 8 hex | a candidate cross-platform pair |
| `opp_id` | `sha1("{pair_id}:{direction}:{detected_ts}")[:12]` | 12 hex | one detected opportunity |

## 2. Enums (categorical vocabularies — never freeform strings)

### `Platform` — `schema.py` [M1]
| Value | Meaning |
|---|---|
| `kalshi` | Kalshi (CFTC-regulated, US) |
| `polymarket` | Polymarket (international CLOB) |

### `Direction` — `schema.py` [M1]
| Value | Meaning |
|---|---|
| `YES@kalshi+NO@poly` | buy YES on Kalshi, NO on Polymarket |
| `NO@kalshi+YES@poly` | buy NO on Kalshi, YES on Polymarket |

### `Stage` — `tracking/stages.py` [M1]
Funnel order. Adding a stage = add a value + emit a `StageResult`; board/store
pick it up with zero display changes.

| Value | What the stage does |
|---|---|
| `ingest` | pull + normalize markets from both platforms |
| `recall` | cheap candidate-pair generation (matching stage 1) |
| `adjudicate` | cached LLM same-event verdicts (matching stage 2) |
| `price` | walk live books for the target size |
| `threshold` | net-of-fee margin vs. alert threshold |
| `alert` | emit + de-duplicate alerts |

### `DropReason` — `tracking/stages.py` [M1]
The complete drop vocabulary (plan §9.4). Every dropped item uses exactly one.

| Stage | Value | Meaning |
|---|---|---|
| recall | `category_mismatch` | not in a configured category on both sides |
| recall | `no_time_overlap` | close-time windows don't overlap |
| recall | `low_similarity` | below the recall similarity floor |
| adjudicate | `llm_not_same_event` | LLM judged the events not equivalent |
| adjudicate | `low_confidence` | same-event but below `min_confidence` |
| adjudicate | `manual_reject` | force-rejected via `manual_overrides.yaml` |
| price | `empty_book` | one side had no orders |
| price | `stale_book` | book older than freshness bound |
| price | `insufficient_depth` | can't fill even minimum size |
| threshold | `negative_margin` | net margin ≤ 0 after fees |
| threshold | `below_threshold` | positive but under alert threshold |
| alert | `duplicate` | already alerted, no material change |
| (any) | `api_error` | upstream fetch failed this cycle |

## 3. Domain entities — `schema.py` [M1]

All money/sizes are `Decimal`; all timestamps ISO 8601 strings.

### `OrderBookLevel` (frozen)
| Field | Meaning |
|---|---|
| `price` | $/share, 0..1 — an ASK price to BUY that side |
| `size` | shares/contracts available at this level |

### `NormalizedMarket`
| Field | Meaning |
|---|---|
| `platform` | `Platform` enum |
| `market_id` | kalshi ticker OR polymarket condition_id |
| `yes_token_id` / `no_token_id` | polymarket outcome token ids (`None` on kalshi) |
| `title` | human question text |
| `category` | normalized category label |
| `resolution_criteria` | FULL rules text — the LLM adjudicator's input |
| `resolution_source` | who/what adjudicates (nullable) |
| `close_time` | ISO 8601 |
| `yes_ask` / `no_ask` | `OrderBookLevel` lists, best first (derived from bids on Kalshi) |
| `raw` | original payload, for debugging |

### `FeeModel`
| Field | Meaning |
|---|---|
| `platform`, `category` | registry key |
| `fee_fn(price, size)` | → dollar fee for buying `size` shares at `price` |

### `MatchedPair`
| Field | Meaning |
|---|---|
| `kalshi`, `polymarket` | the two `NormalizedMarket`s |
| `is_same_event` | LLM verdict |
| `confidence` | 0..1 from the LLM |
| `same_direction` | `false`: YES on kalshi ≡ NO on polymarket (inverted phrasing). Flagged addition to §5 [M6] — §6's verdict has it; the engine needs it |
| `resolution_caveats` | LLM notes on subtle rule differences (surfaced in alerts) |
| `verdict_ts` | when adjudicated |
| `rules_hash` | `sha1(kalshi_rules ␟ poly_rules)[:16]` — changed hash → re-adjudicate |

### `ArbOpportunity`
| Field | Meaning |
|---|---|
| `pair` | the `MatchedPair` |
| `direction` | `Direction` enum |
| `size` | share-pairs achievable at these levels |
| `fill_yes`, `fill_no` | size-weighted fill prices from walking the books |
| `fee_yes`, `fee_no` | per-leg fees at those fills |
| `net_per_pair` | §3.4 formula result |
| `roi_pct` | return on capital deployed |
| `detected_ts` | ISO 8601 |

## 4. `StageResult` — `tracking/stages.py` [M1]

The standardized per-stage funnel report. **Invariant enforced at
construction: `sum(drops.values()) == n_in − n_out`** — nothing vanishes
untracked.

| Field | Meaning |
|---|---|
| `stage` | `Stage` enum |
| `n_in`, `n_out` | items entering / surviving the stage |
| `drops` | `DropReason → count` |
| `dropped_ids` | `DropReason → [entity/pair ids]`, lengths must match `drops`; may be empty when `keep_dropped_ids` is off |
| `duration_ms` | stage wall time |

**Recall-stage unit semantics [M5]:** the recall `StageResult` counts
**markets** (both platforms) in and out, not pairs — the invariant needs one
unit, and the §9.6 sketch mixes them. A market survives if it appears in ≥1
emitted `CandidatePair`; otherwise it drops with exactly one reason
(`CATEGORY_MISMATCH` → `NO_TIME_OVERLAP` → `LOW_SIMILARITY`, first
applicable). The pair count is reported separately (`len(candidates)`).

**Loop cadence & cycle timing [M10]:** the daemon (`main.run_loop`) refreshes
the blessed set on the slow `poll.discovery_interval_sec` cadence and re-prices
it every `poll.price_interval_sec`; discovery stage-results are carried forward
onto price-only ticks so every persisted cycle shows all six funnel stages.
`RunState.started_ts` is the **process** start (drives board uptime);
`cycle_ts` is the per-tick time. Each tick is one `cycles` + 6 `stage_stats`
rows. API failures back off exponentially (`poll.backoff_base_sec` →
`poll.max_backoff_sec`) and never crash the loop. The sweep
(`adjudicator._smoke`) and the loop share one implementation via
`arbdetector.pipeline` (`run_discovery` / `run_price_alert` / `persist_cycle`).

**Alert-stage semantics [M9]:** units are **pairs**. `run_alert` sends each
threshold survivor that is NEW or materially changed (net/pair moved ≥
`alerting.material_change_per_pair` vs. the last delivered alert for the same
`(pair_id, direction)` — read from the `alerts` table). Unchanged repeats drop
as `DUPLICATE`. Per-sink send failures feed the cycle's `error_count`, never a
silent DUPLICATE. This is the final funnel stage — the board now renders all
six.

**Price/threshold stage semantics [M7]:** units are **pairs** throughout.
`price` (in `engine/signal.py::run_price`) fetches/replays books, applies the
`same_direction` swap, walks both directions to `engine.target_size_pairs`;
drops `API_ERROR` → `STALE_BOOK` (older than `engine.max_book_age_sec`) →
`EMPTY_BOOK` (no quotable direction) → `INSUFFICIENT_DEPTH` (no direction
fills `engine.min_size_pairs`). `threshold` (`run_threshold`) applies the
strictly-greater §3.4 rule; drops `NEGATIVE_MARGIN` → `BELOW_THRESHOLD`;
survivors are §5 `ArbOpportunity` objects (their §9.2 id comes from
`opportunity_id()` — the id is derived, not a field, per §5).

### `CandidatePair` — `matching/recall.py` [M5]

A recalled, not-yet-adjudicated pair (in-memory; persisted into `pairs` [M8]).

| Field | Meaning |
|---|---|
| `pair_id` | deterministic id (§1) |
| `kalshi`, `polymarket` | the two `NormalizedMarket`s |
| `similarity` | recall score (tf-idf cosine of titles) — NOT a same-event probability; the adjudicator's `confidence` is the verdict |

## 5. `RunState` — `tracking/runstate.py` [M8 ✓]

Single source of truth per cycle; every view renders from it. Fields per plan
§9.5: `schema_version`, `cycle_id`, `started_ts`, `cycle_ts`, `funnel`
(ordered `StageResult`s), `active_opportunities`, `health`, `cache_stats`,
`store_stats`. Serialized atomically (temp file + `os.replace`) to
`state/latest.json`. **Serialization note:** opportunities serialize as
board-ready summaries (ids, titles, exact money strings) — the full book
snapshot lives in `opportunities.book_snapshot_json`, per §8.

## 6. State files — `state/` (gitignored) [M8 ✓]

| File | Contents |
|---|---|
| `latest.json` | current `RunState` snapshot, atomic-written each cycle |
| `STATUS.txt` | plain-text status board rendered from `RunState` (generic over the funnel) |
| `events.jsonl` | structured log: one JSON object per line; mandatory keys `ts`, `lvl`, `stage`, `event`, plus `entity_id`/`pair_id`/`reason` where applicable |
| `arb.db` | SQLite store (tables + views below) |

## 7. SQLite tables — `store/sqlite.py` [M8 ✓] (verdicts DDL canonical here, shared with `matching/cache.py`)

All tables carry `schema_version`. Append-only ledgers; no in-place mutation
of history. Money/sizes are stored as TEXT (Decimal strings), never REAL.

| Table | Key | Contents |
|---|---|---|
| `markets` | `entity_id` PK | platform, market_id, title, category, close_time, first/last_seen_ts |
| `pairs` | `pair_id` PK | kalshi/poly entity FKs, rules_hash, first_seen_ts |
| `verdicts` [M6 ✓] | (`pair_id`, `rules_hash`) PK | is_same_event, confidence, same_direction, caveats, verdict_ts, model (v2), schema_version — doubles as the LLM cache |
| `opportunities` | `opp_id` PK | pair FK, cycle FK, direction, size, fills, fees, net, roi, detected_ts, **book_snapshot_json** (additive: §8 requires the full book with every flagged opportunity) |
| `drops` | `id` PK | cycle FK, stage, reason, entity_or_pair_id, **count** (additive: per-item rows carry count=1 + an id; with `keep_dropped_ids` off, one aggregate row per reason carries the count with NULL id — views SUM(count) so both shapes read identically), detail_json, ts |
| `cycles` | `cycle_id` PK | started/ended_ts, duration_ms, error_count |
| `stage_stats` | (`cycle_id`, `stage`) PK | n_in, n_out, duration_ms per stage per cycle |
| `alerts` [M9] | `id` PK | pair_id, direction, opp_id, net_per_pair, roi_pct, size, cycle_id, alerted_ts, channels — the de-dup memory: `last_alert(pair_id, direction)` is the DUPLICATE lookup, restart-safe |

## 8. Views — `store/views.sql` [M8 ✓]

One definition per "thing to look at"; board and any future dashboard read
these same views.

| View | Answers |
|---|---|
| `v_active_opportunities` | what's actionable right now (joined to titles + caveats) |
| `v_funnel_latest` | the funnel for the most recent cycle (backs the board) |
| `v_drop_breakdown_24h` | "why so few opportunities?" — stage, reason, count over 24h |
| `v_pair_trace` | everything about one `pair_id`: markets, verdict, latest opp, recent drops |
| `v_opportunity_history` | spread distribution / shadow-validation series |
| `v_cycle_health` | per-cycle durations + error counts (regression watch) |
