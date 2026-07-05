# Cross-Platform Prediction Market Arbitrage Detector — Project Plan

> **Purpose of this document.** This is a complete specification and build plan for an
> arbitrage *detector* (not an executor) spanning **Kalshi** and **Polymarket**. It is written
> to be handed to Claude Code (or any capable coding agent) as the authoritative reference for
> what to build, why each piece exists, and in what order. It is deliberately verbose on
> *intent* so the agent understands the domain, not just the file list.

---

## 0. TL;DR for the implementing agent

Build a Python service that:

1. Pulls **public** market data (metadata + full order books) from Kalshi and Polymarket.
2. **Matches** the "same real-world event" across the two platforms using a two-stage pipeline:
   a cheap recall filter, then an **LLM adjudication** step that compares resolution rules and
   caches a structured verdict.
3. For every LLM-blessed pair, computes the **net-of-fee** arbitrage margin using each
   platform's *actual per-leg fee curve* evaluated at the real fill prices, walking order-book
   depth for a target size.
4. **Alerts** (Telegram/console/log) when net margin exceeds a configurable threshold.
5. **Tracks every item through the pipeline** with a reason-coded funnel, so you can always read
   off — at a glance and per-item — where something is and, if it dropped out, exactly why. See §9;
   this is a first-class goal, not an afterthought.

**It never places trades.** No wallet, no USDC, no Polygon RPC, no Kalshi private keys for
order placement. Read-only everywhere.

**v1 scope restriction:** Polymarket **geopolitics** category ↔ Kalshi. Geopolitics is the one
Polymarket category that is still fee-free (as of 2026), so the arb signal is cleanest there
(fees only on the Kalshi leg) and the market universe is small enough to make LLM matching
tractable.

---

## 1. Domain background (read this before writing code)

### 1.1 What a prediction-market contract is

A binary contract pays **$1 if an event happens, $0 if it doesn't**. The price of a "YES" share
is therefore the market's implied probability, expressed in $0.00–$1.00. For a single
well-defined event, `P(YES) + P(NO) = 1`, because exactly one side pays out.

### 1.2 The arbitrage we're detecting (Type 2: cross-platform)

For the *same* event correctly matched across two platforms A and B:

- Buy YES on whichever platform prices YES lower.
- Buy NO on whichever platform prices YES higher (equivalently, NO cheaper).

If you buy YES on A and NO on B, cost is `p_A(YES) + p_B(NO)`. One of the two legs pays $1 at
resolution regardless of outcome, so:

```
gross_profit_per_pair = 1 - [ p_A(YES) + p_B(NO) ]
                      = 1 - p_A(YES) - (1 - p_B(YES))
                      = p_B(YES) - p_A(YES)
```

So the **gross** margin is just the difference in implied YES probabilities across platforms.
The arbitrage exists whenever the two platforms disagree on the YES price. **This is trivial;
the entire difficulty of the project is (a) confirming it's really the same event and (b)
confirming the margin survives fees, depth, and slippage.**

### 1.3 Why the opportunity exists at all

The two platforms have separate liquidity pools, different user bases (Polymarket skews
crypto-native/global; Kalshi is US-regulated/CFTC), different rules, and independent price
discovery. The *friction of moving capital between them* is precisely what lets a spread persist
— if it were frictionless, arbitrageurs would instantly equalize prices.

### 1.4 Reality check on profitability (set expectations honestly)

- Realistic net margins after all costs are roughly **$0.01–$0.02 per share pair**.
- The space is **crowded**: open-source bots, Chrome extensions (e.g. PolyArbitrage), and hosted
  scanners (ArbBets, Eventarb) already exist.
- Real capture is an execution-latency arms race dominated by professional firms.

**Implication for this project:** its value is as a **detector / research / analytics tool** and
as a genuinely interesting engineering + applied-LLM problem — *not* as a money printer. The
detector-only scope is deliberate and correct. Do not scope-creep into execution.

---

## 2. Data sources — exact APIs and quirks

Everything below is **public / read-only**. No authentication is needed for the core data
except the Kalshi WebSocket handshake.

### 2.1 Kalshi

- **Base URL:** `https://external-api.kalshi.com/trade-api/v2`
- **No auth** required for public market-data REST endpoints.
- Key endpoints:
  - `GET /markets` — list/discover. Filter by `series_ticker`, `event_ticker`, `status=open`,
    and close-time ranges (`min_close_ts` / `max_close_ts`). Cursor-paginated.
  - `GET /events/{event_ticker}` — returns the `category` field (your category filter lever).
  - `GET /markets/{ticker}/orderbook` — full depth.
- **CRITICAL ORDER-BOOK QUIRK:** Kalshi returns **only bids** for both YES and NO sides (no
  asks). In a binary market, a YES bid at price X is equivalent to a NO ask at (1 − X). So to
  get the price you'd *pay* to buy YES (the YES ask), you must derive it from the best NO bid:
  ```
  best_yes_ask = 1.00 - best_no_bid
  best_no_ask  = 1.00 - best_yes_bid
  ```
  Getting this wrong makes every computed spread meaningless. Write a dedicated, unit-tested
  function for this and never inline the arithmetic.
- Response format: prices are dollar strings (e.g. `"0.4200"`), quantities fixed-point strings.
  Use Python `Decimal` throughout — never float — for prices and sizes.
- **Rate limit:** ~30 requests/sec for public market data.
- **WebSocket:** `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, channels
  `orderbook_snapshot` + `orderbook_delta`. The handshake requires auth **even for public data
  channels** (RSA-PSS request signing). For v1 you can poll REST; add WS later for latency.

### 2.2 Polymarket

Three separate services — do not confuse them:

- **Gamma API** — `https://gamma-api.polymarket.com` — fully public, no auth. Discovery layer:
  events, markets, tags, series, **search**. Use `?active=true&closed=false`. Filter category
  via `tag_id`. Fetch a specific event by slug: `/events?slug=...`.
- **Data API** — `https://data-api.polymarket.com` — fully public. Positions, trades, volume,
  open interest. Optional for v1.
- **CLOB API** — `https://clob.polymarket.com` — **order books live HERE, not on Gamma.** Public
  read endpoints (no auth): `/book?token_id=...`, plus midpoint/spread/price helpers. The Python
  SDK `py-clob-client` exposes:
  - `get_order_book(token_id)`
  - `get_midpoint(token_id)`, `get_spread(token_id)`
  - `calculate_market_price(token_id, side, size, order_type)` — **walks the book to estimate the
    fill price for a given size**. This is your slippage/depth tool; use it rather than
    hand-rolling depth-walking if the SDK is available.
- **IDENTIFIERS (classic first bug):** a **condition ID** (hex) identifies a *market*; a
  **token ID** identifies a specific *outcome* (YES or NO). **Order-book queries take token IDs,
  not condition IDs.** Gamma gives you the mapping.
- **Staleness gotcha:** Gamma `outcomePrices` can lag the live book by a few seconds. Use Gamma
  for *discovery/matching*, but read the **CLOB order book** for the actual price comparison.
- **Rate limits:** be polite; implement exponential backoff on HTTP 429. WebSocket available at
  `wss://ws-subscriptions-clob.polymarket.com/ws/market` (subscribe by token ID) for later.

### 2.3 What you do NOT need

No wallet, no USDC, no Polygon RPC, no Kalshi trading keys. Those are execution-only. This is a
detector.

---

## 3. Fees — the core of practical usefulness

A before-fees detector is useless. Both platforms adopted **Bernoulli-variance fee curves** that
peak at 50¢ and vanish toward the price extremes. Model each leg's fee **at its actual fill
price**, per category. **Never use a flat percentage** — it hides real edges at the tails and
invents fake ones near 50/50.

### 3.1 Kalshi taker fee

```
fee_per_order = ceil(0.07 * P * (1 - P) * n_contracts * 100) / 100      # dollars
```
- `P` = contract price in dollars (0.01–0.99), `n_contracts` = number of contracts.
- Peaks at P = 0.50 → **$0.0175/contract** (rounded up per order to the cent). Symmetric: fee at
  P equals fee at (1 − P).
- **Maker** fee ≈ 25% of taker, frequently rounds to $0 on small orders.
- No settlement fee, no membership fee, no ACH deposit fee.
- Some special-event markets carry different multipliers/maker fees — treat the 0.07 multiplier
  as a per-category configurable, not a constant.

### 3.2 Polymarket taker fee (post-2026 fee rollout — NOT zero anymore)

```
fee = n_shares * feeRate * P * (1 - P)                                   # USDC
```
- **Makers are never charged.** Only takers pay.
- `feeRate` is **category-specific**:

  | Category                                   | feeRate |
  |--------------------------------------------|---------|
  | Crypto                                     | 0.07    |
  | Economics, Culture, Weather, Other         | 0.05    |
  | Finance, Politics, Tech, Mentions          | 0.04    |
  | Sports                                     | 0.03    |
  | **Geopolitics / world events**             | **0.00**|

- Fees rounded to 5 decimals; min charge 0.00001 USDC (tiny trades near extremes → ~$0).
- The separate **US (QCX) exchange** uses a flat 0.05 taker / −0.0125 maker rebate model. For v1
  target the **international** Polymarket schedule above; keep the fee model pluggable so the US
  schedule can be swapped in.

### 3.3 Why v1 = geopolitics

Because Polymarket geopolitics `feeRate = 0`, only the **Kalshi leg** incurs a fee. Cleanest
possible signal, and a naturally small, high-overlap market universe (elections, wars, policy,
international events) that both platforms list.

### 3.4 The net-margin formula the engine must implement

Per matched share-pair, buying YES on A and NO on B:

```
net_profit_per_pair =
      1
    - fill_price_A_YES              # actual size-weighted fill from walking A's book
    - fill_price_B_NO               # actual size-weighted fill from walking B's book
    - fee_A(fill_price_A_YES, size, category_A)
    - fee_B(fill_price_B_NO, size, category_B)
```

Evaluate **both directions** (YES@A + NO@B, and NO@A + YES@B); report the better one. Flag only
when `net_profit_per_pair > threshold`, where the threshold also buffers expected slippage and a
safety margin. Express results both per-pair (dollars) and as return-on-capital-deployed (%).

---

## 4. Architecture

```
                    ┌────────────────────────────────────────────────────┐
                    │                   CONFIG (YAML)                     │
                    │  categories, thresholds, poll intervals, fee tables │
                    └────────────────────────────────────────────────────┘
                                          │
   ┌──────────────┐   normalize   ┌───────▼────────┐   normalize   ┌──────────────┐
   │  Kalshi      │──────────────▶│   INGEST /      │◀──────────────│ Polymarket   │
   │  client      │               │   NORMALIZE     │               │  client      │
   │ (REST/WS)    │               │  → common       │               │ (Gamma+CLOB) │
   └──────────────┘               │    schema       │               └──────────────┘
                                  └───────┬─────────┘
                                          │ NormalizedMarket objects
                                          ▼
                            ┌─────────────────────────────┐
                            │      MATCHING LAYER          │
                            │  Stage 1: recall filter      │  (cheap, frequent)
                            │  Stage 2: LLM adjudication   │  (cached, on rules)
                            │  → blessed pair cache        │
                            └─────────────┬───────────────┘
                                          │ blessed pairs
                                          ▼
                            ┌─────────────────────────────┐
                            │      SIGNAL ENGINE           │
                            │  walk books for target size  │
                            │  per-leg fee curves          │
                            │  net margin, both directions │
                            └─────────────┬───────────────┘
                                          │ opportunities
                          ┌───────────────┼────────────────┐
                          ▼               ▼                ▼
                   ┌────────────┐  ┌────────────┐  ┌────────────────┐
                   │  ALERTING  │  │  LOGGING / │  │  DASHBOARD     │
                   │ (Telegram) │  │  BACKTEST  │  │  (optional/    │
                   │            │  │  STORE     │  │   later)       │
                   └────────────┘  └────────────┘  └────────────────┘
```

Design principles:
- **Ports/adapters:** each platform hides behind a `MarketDataClient` interface so the engine
  never sees platform-specific quirks. Adding a third platform later = one new adapter.
- **`Decimal` everywhere** for money. Never float.
- **The LLM is offline / out of the hot path.** It runs once per candidate pair and its verdict
  is cached; the price loop only ever touches already-blessed pairs.
- **Fees are first-class, pluggable objects** keyed by (platform, category), so schedule changes
  are config edits, not code edits.

---

## 5. Common normalized schema

Every platform adapter maps into this. This is the contract the rest of the system depends on.

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Callable

class Platform(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"

@dataclass(frozen=True)
class OrderBookLevel:
    price: Decimal          # $/share, 0..1  (an ASK price to BUY that side)
    size: Decimal           # shares/contracts available at this level

@dataclass
class NormalizedMarket:
    platform: Platform
    market_id: str                    # kalshi ticker OR polymarket condition_id
    yes_token_id: str | None          # polymarket token id for YES (None on kalshi)
    no_token_id: str | None
    title: str                        # human question text
    category: str                     # normalized category label
    resolution_criteria: str          # FULL rules text — critical for LLM matching
    resolution_source: str | None     # who/what adjudicates
    close_time: str                   # ISO 8601
    yes_ask: list[OrderBookLevel]     # levels to BUY YES, best first  (derived on Kalshi!)
    no_ask:  list[OrderBookLevel]     # levels to BUY NO,  best first
    raw: dict                         # original payload, for debugging

@dataclass
class FeeModel:
    platform: Platform
    category: str
    # returns dollar fee for buying `size` shares at fill price `p`
    fee_fn: Callable[[Decimal, Decimal], Decimal]

@dataclass
class MatchedPair:
    kalshi: NormalizedMarket
    polymarket: NormalizedMarket
    is_same_event: bool
    confidence: float                 # 0..1 from the LLM
    resolution_caveats: str           # LLM notes on any subtle differences
    verdict_ts: str
    rules_hash: str                   # hash of both rule texts; re-adjudicate if it changes

@dataclass
class ArbOpportunity:
    pair: MatchedPair
    direction: str                    # "YES@kalshi+NO@poly" or "NO@kalshi+YES@poly"
    size: Decimal                     # share-pairs achievable at these levels
    fill_yes: Decimal
    fill_no: Decimal
    fee_yes: Decimal
    fee_no: Decimal
    net_per_pair: Decimal
    roi_pct: Decimal
    detected_ts: str
```

---

## 6. The matching layer (the hard part) — detailed design

**Problem:** "Fed cuts in March" on one platform may or may not resolve identically to the
other's version — different windows, resolution sources, tie/edge-case handling. A naive
keyword/embedding match produces false positives that would be *losing* trades if executed. The
entire risk of the domain concentrates here.

### Stage 1 — recall filter (cheap, runs frequently)

Goal: propose candidate pairs with high recall, don't worry about precision yet.
- Restrict to the configured category on both platforms and to overlapping `close_time` windows.
- Compute text similarity between titles/descriptions (embeddings or a lightweight model).
- Keep top-K candidates per Kalshi market above a low similarity floor.
- This stage must be cheap enough to run every scan cycle.

### Stage 2 — LLM adjudication (expensive, cached, out of hot path)

For each *new* candidate pair (or when either market's `rules_hash` changes):
- Prompt a high-reasoning model with **both full resolution-criteria texts**, close dates, and
  resolution sources.
- Require a **strict structured JSON** verdict (no prose outside JSON):

  ```json
  {
    "is_same_event": true,
    "confidence": 0.0,
    "resolution_caveats": "string describing any window/source/edge-case differences",
    "same_direction": true
  }
  ```
  `same_direction` handles cases where YES on one platform corresponds to NO on the other
  (inverted phrasing).
- **Cache the verdict** keyed by `(kalshi_id, poly_id, rules_hash)`. Re-adjudicate only when the
  hash changes. The price loop reads this cache; it never calls the LLM itself.
- Only pairs with `is_same_event == true` **and** `confidence >= config.min_confidence` proceed
  to the signal engine. Log low-confidence and near-miss pairs for human review.

### Implementation notes

- Anthropic API via the official SDK. Model choice configurable; use a strong reasoning model.
- Make the prompt demand it flag *any* difference in resolution window, source, or tie-handling —
  false "same event" verdicts are the expensive failure mode; bias toward caution.
- Keep a `manual_overrides.yaml` so a human can force-approve or force-reject specific pairs.
- Persist the verdict cache (SQLite) so restarts don't re-spend tokens.

---

## 7. The signal engine

For each blessed `MatchedPair`, every scan cycle:

1. Pull the **live CLOB/REST order book** for both markets (not the possibly-stale Gamma prices).
2. For a configured target `size` (in share-pairs), **walk each book** to get the size-weighted
   fill price:
   - Polymarket: prefer `calculate_market_price(token_id, side, size, FOK)` if available.
   - Kalshi: walk the derived ask levels (remember the NO-bid → YES-ask reconstruction).
   - If depth is insufficient for the full target size, report the max achievable size and the
     fill at that size (partial-fill realism).
3. Evaluate **both directions**; for each compute `net_per_pair` via the §3.4 formula using the
   per-leg, per-category `FeeModel`.
4. If `net_per_pair > config.threshold` (which already buffers slippage + safety), emit an
   `ArbOpportunity`.
5. Hand opportunities to alerting + logging.

Include a `simulate` data mode (replay recorded books) so the engine can be tested and demoed
without hitting live APIs.

---

## 8. Alerting, logging, backtesting

- **Alerting:** Telegram bot (token via env var) + colored console. Message includes both
  markets' titles, the direction, fill prices, per-leg fees, net-per-pair, ROI%, achievable size,
  and the LLM `resolution_caveats` (so a human sees the risk before acting). De-duplicate: don't
  re-alert the same opportunity every cycle; alert on appearance and on material change.
- **Logging / backtest store:** persist **every** flagged opportunity with a timestamp and the
  full book snapshot to SQLite (or Parquet). This is what lets you later answer "were these real?"
  by checking whether the spread would actually have been capturable and how it resolved. This
  record is the single most valuable artifact for validating the detector before trusting it.
- **Metrics:** opportunities/hour by category, spread distribution, match-cache hit rate, API
  error/backoff counts.

---

## 9. Observability, state tracking & legibility

**This is a first-class goal of the project, weighted as heavily as the arb math itself.** The
system is a multi-stage funnel (`ingest → recall → adjudicate → price → threshold → alert`). The
single most useful property for both debugging *and* future extension is being able to answer, at
any moment and for any item: **"where is it in the pipeline, and if it fell out, exactly why?"**
Everything below serves that one question. It is optimized for **legibility, not aesthetics** —
fixed-width text, categorical codes, and queryable tables, not charts.

### 9.1 Design principles

1. **One source of truth.** A single typed `RunState` object holds the current state of the world
   each cycle. Every view (status board, logs, future dashboard) is *rendered from it* — there are
   never two subsystems maintaining their own divergent idea of "what's happening."
2. **Stable IDs threaded end-to-end.** Every market, pair, and opportunity has a deterministic ID
   so a single item can be traced across every stage and every table (correlation).
3. **Append-only ledgers for history.** Cycles, opportunities, and drops are appended, never
   mutated in place. History is free and regressions become visible.
4. **Categorical reason codes, never freeform.** Every time an item is dropped, the reason is a
   value from a fixed enum. "Why are there no opportunities right now?" becomes a `GROUP BY`, not a
   log-reading exercise. **This is the heart of the whole design.**
5. **Machine-readable first, human-readable rendered.** State is persisted as JSON + SQLite; the
   plain-text board and any dashboard are thin renderers over that. Nothing is *only* human-readable.
6. **Everything typed and schema-versioned.** Enums for stages/reasons/platforms; a
   `schema_version` on every persisted record. Additions are additive and tracked.

### 9.2 Stable identifiers (correlation)

```python
# Deterministic, so the same market/pair always gets the same id across restarts.
entity_id  = sha1(f"{platform}:{market_id}")[:8]           # one market on one platform
pair_id    = sha1(f"{kalshi_entity_id}:{poly_entity_id}")[:8]   # a candidate cross-platform pair
opp_id     = sha1(f"{pair_id}:{direction}:{detected_ts}")[:12]  # one detected opportunity
```

These IDs appear in the status board, in every structured log line, and as keys/foreign keys in
every table. Given any `pair_id` you can reconstruct its two markets, its LLM verdict, its latest
opportunity, and every time it was dropped and why (see the `v_pair_trace` view, §9.8).

### 9.3 The pipeline funnel (the at-a-glance artifact)

The pipeline stages are an explicit enum, and **each stage reports a standardized result**:

```python
class Stage(str, Enum):
    INGEST = "ingest"; RECALL = "recall"; ADJUDICATE = "adjudicate"
    PRICE = "price";  THRESHOLD = "threshold"; ALERT = "alert"

@dataclass
class StageResult:
    stage: Stage
    n_in: int
    n_out: int
    drops: dict["DropReason", int]              # reason -> count  (must sum to n_in - n_out)
    dropped_ids: dict["DropReason", list[str]]  # reason -> [entity/pair ids]  (for tracing)
    duration_ms: float
```

Because every stage speaks this same shape, the funnel is uniform and self-describing: for each
stage you always see how many went in, how many survived, and the reason breakdown for the rest.
Adding a new stage later means adding an enum value and emitting a `StageResult` — the funnel,
board, and store pick it up with **zero display changes**.

### 9.4 Drop reason codes (fixed vocabulary)

The complete, versioned vocabulary. Every drop in the system uses one of these; no ad-hoc strings.

| Stage | `DropReason` | Meaning |
|-------|--------------|---------|
| recall | `CATEGORY_MISMATCH` | Not in a configured category on both sides |
| recall | `NO_TIME_OVERLAP` | Close-time windows don't overlap |
| recall | `LOW_SIMILARITY` | Below the recall similarity floor |
| adjudicate | `LLM_NOT_SAME_EVENT` | LLM judged the events not equivalent |
| adjudicate | `LOW_CONFIDENCE` | Same-event but below `min_confidence` |
| adjudicate | `MANUAL_REJECT` | Force-rejected via `manual_overrides.yaml` |
| price | `EMPTY_BOOK` | One side had no orders |
| price | `STALE_BOOK` | Book older than freshness bound |
| price | `INSUFFICIENT_DEPTH` | Can't fill even minimum size |
| threshold | `NEGATIVE_MARGIN` | Net margin ≤ 0 after fees |
| threshold | `BELOW_THRESHOLD` | Positive but under alert threshold |
| alert | `DUPLICATE` | Already alerted, no material change |
| infra | `API_ERROR` | Upstream fetch failed this cycle |

New reasons are added to the enum (never invented inline), which keeps the drop ledger and its
breakdown views complete and trustworthy over time.

### 9.5 Canonical run state (single source of truth)

```python
@dataclass
class RunState:
    schema_version: int
    cycle_id: int
    started_ts: str                      # process start (for uptime)
    cycle_ts: str                        # this cycle's timestamp
    funnel: list[StageResult]            # ordered ingest → alert
    active_opportunities: list[ArbOpportunity]
    health: dict                         # per-source: status, latency_ms, error_count
    cache_stats: dict                    # verdict-cache size + hit rate
    store_stats: dict                    # db size, row counts, last-discovery age
```

Serialized every cycle to `state/latest.json` (atomic write: temp file + rename, so a reader never
sees a half-written file). This single object is what the status board renders and what a future
dashboard would poll. **If it's worth showing, it lives in `RunState`.**

### 9.6 The plain-text status board

Regenerated each cycle to `state/STATUS.txt` and optionally stdout. Fixed-width, aligned, boring on
purpose — the point is that you can read the entire state of the system in three seconds.

```
================================================================================
 ARB DETECTOR   cycle #01423   2026-07-05T14:32:07Z   uptime 3h12m   schema v1
================================================================================
 PIPELINE FUNNEL                      in    out   dropped   top drop reason
 ------------------------------------------------------------------------------
 ingest.kalshi                         —    412        —
 ingest.polymarket                     —     88        —
 recall (candidate pairs)            500    137      363    LOW_SIMILARITY (351)
 adjudicate (same-event, cached)     137     24      113    LLM_NOT_SAME_EVENT (98)
 price (book-walked)                  24     24        0
 threshold (net > 0.02)               24      3       21    BELOW_THRESHOLD (19)
 alert                                 3      2        1    DUPLICATE (1)
 ------------------------------------------------------------------------------
 ACTIVE OPPORTUNITIES                                          net/pair   roi   size
   [3f9a] "Will X happen by Aug?"     YES@kalshi + NO@poly       $0.031   6.2%   500
   [7c12] "Country Y joins Z?"        NO@kalshi + YES@poly       $0.024   4.9%   220
 ------------------------------------------------------------------------------
 HEALTH   kalshi OK 31ms   poly OK 88ms   llm_cache hit 0.94   errors 0
 STORE    arb.db 1.2MB   verdicts 218   opps(24h) 47   last full discovery 4m ago
================================================================================
```

The columns map 1:1 to `StageResult` fields, so this renderer is generic: it loops the funnel and
prints — it does not hard-code stage names. Add a stage → it appears automatically.

### 9.7 Structured logging (JSON lines)

Every log record is a typed event, one JSON object per line (`state/events.jsonl`) — greppable,
`jq`-able, and loadable into the store. **No freeform string logs for anything that's part of the
pipeline.**

```json
{"ts":"2026-07-05T14:32:07.114Z","lvl":"info","stage":"adjudicate","pair_id":"3f9a","event":"verdict","is_same_event":false,"confidence":0.41,"reason":"llm_not_same_event"}
{"ts":"2026-07-05T14:32:07.220Z","lvl":"info","stage":"threshold","pair_id":"7c12","event":"drop","reason":"below_threshold","net_per_pair":"0.004"}
{"ts":"2026-07-05T14:32:07.310Z","lvl":"info","stage":"alert","pair_id":"3f9a","event":"emit","direction":"YES@kalshi+NO@poly","net_per_pair":"0.031","roi_pct":"6.2"}
```

Mandatory keys on every line: `ts`, `lvl`, `stage`, `event`, and (where applicable) `entity_id` /
`pair_id` and `reason`. This uniformity is what lets you trace one `pair_id` through its entire life
with a single `grep`.

### 9.8 Persistent store: SQLite tables + predefined views

SQLite is the structured backbone (one file, zero-ops, trivially queryable). Well-named tables plus
**predefined VIEWS** so that "reading off" any common question is a single `SELECT * FROM v_...`.

**Tables** (each carries `schema_version`):

```
markets(entity_id PK, platform, market_id, title, category, close_time,
        first_seen_ts, last_seen_ts, schema_version)
pairs(pair_id PK, kalshi_entity_id FK, poly_entity_id FK, rules_hash, first_seen_ts)
verdicts(pair_id, rules_hash, is_same_event, confidence, same_direction,
         caveats, verdict_ts,  PRIMARY KEY(pair_id, rules_hash))     -- also the LLM cache
opportunities(opp_id PK, pair_id FK, cycle_id FK, direction, size,
              fill_yes, fill_no, fee_yes, fee_no, net_per_pair, roi_pct, detected_ts)
drops(id PK, cycle_id FK, stage, reason, entity_or_pair_id, detail_json, ts)
cycles(cycle_id PK, started_ts, ended_ts, duration_ms, error_count, schema_version)
stage_stats(cycle_id FK, stage, n_in, n_out, duration_ms,  PRIMARY KEY(cycle_id, stage))
```

**Views** (the "read off at a glance" layer; keep definitions in `store/views.sql`):

```
v_active_opportunities  -- latest opp per pair currently above threshold, joined to titles + caveats
v_funnel_latest         -- the funnel for the most recent cycle (backs the status board)
v_drop_breakdown_24h    -- stage, reason, count over last 24h  (answers "why so few opps?")
v_pair_trace            -- ALL rows for one pair_id: its 2 markets, verdict, latest opp, recent drops
v_opportunity_history   -- opps over time for spread-distribution / shadow-validation analysis
v_cycle_health          -- per-cycle durations + error counts (regression watch)
```

The same views back both the status board today and any dashboard later, so there's exactly one
definition of each "thing to look at."

### 9.9 The cycle ledger (history & time series)

`cycles` + `stage_stats` are an append-only ledger: one row set per scan cycle with per-stage
counts and durations. This gives you, for free: a time series of how many opportunities appeared,
where items fell out over time, and whether any stage is slowing down or erroring more — all by
querying `v_cycle_health` and `v_opportunity_history`. It is also the substrate for §11's shadow
validation.

### 9.10 Schema versioning & the state registry doc

- Every persisted record and `RunState` carries `schema_version` (start at `1`).
- A committed **`STATE_SCHEMA.md`** is the human-facing registry: it enumerates every entity, field,
  `Stage`, `DropReason`, table, and view, with a one-line description each. It is the map a future
  contributor (or future-you) reads first.
- Changing the shape of tracked state = bump `schema_version`, add the enum value / column, append to
  `STATE_SCHEMA.md`. Migrations are additive; old rows keep their version.

### 9.11 Why this makes future extension easy (the payoff)

Each of these future changes touches *only* its own additive surface, never the display or storage
plumbing, because everything renders from `RunState` + views and every stage speaks `StageResult`:

- **Add a third platform** → new adapter + new `Platform` enum value. Funnel, board, and store are
  generic over platform; no renderer changes.
- **Add a pipeline stage** (e.g. a liquidity pre-filter) → new `Stage` value + emit a `StageResult`.
  It appears in the funnel, board, `stage_stats`, and health automatically.
- **Add a drop reason** → new `DropReason` value. The drop ledger and `v_drop_breakdown_24h` include
  it with no code changes elsewhere.
- **Add a dashboard** → a new renderer over the existing `RunState` + views. Zero changes to the
  pipeline.
- **Add execution someday** (out of scope now) → an `executions` table + `EXECUTE` stage slot; the
  tracking model already anticipates it.

This is the concrete sense in which "structured tracking" and "easy to extend later" are the same
property: the tracking layer is the stable spine, and features hang off it additively.

---

## 10. Suggested repository layout

```
prediction-arb-detector/
├── README.md
├── PREDICTION_MARKET_ARB_DETECTOR_PLAN.md   # this document
├── STATE_SCHEMA.md             # §9.10 registry: every entity, stage, reason, table, view
├── pyproject.toml
├── config.yaml                              # categories, thresholds, intervals, fee tables
├── .env.example                             # ANTHROPIC_API_KEY, TELEGRAM_TOKEN, ...
├── state/                       # RUNTIME OUTPUT (gitignored) — the tracking surface
│   ├── latest.json             #   current RunState snapshot (atomic-written each cycle)
│   ├── STATUS.txt              #   plain-text status board (rendered each cycle)
│   ├── events.jsonl            #   structured JSON-lines log
│   └── arb.db                  #   SQLite store (tables + views)
├── src/arbdetector/
│   ├── __init__.py
│   ├── schema.py               # §5 dataclasses / enums (domain objects)
│   ├── clients/
│   │   ├── base.py             # MarketDataClient interface (port)
│   │   ├── kalshi.py           # REST + (later) WS; NO-bid→YES-ask reconstruction lives here
│   │   └── polymarket.py       # Gamma discovery + CLOB books; condition_id↔token_id mapping
│   ├── fees/
│   │   ├── base.py             # FeeModel + registry keyed by (platform, category)
│   │   ├── kalshi_fees.py      # ceil(0.07*P*(1-P)*n*100)/100
│   │   └── polymarket_fees.py  # category feeRate table, n*rate*P*(1-P)
│   ├── matching/
│   │   ├── recall.py           # stage 1 similarity/candidate generation
│   │   ├── adjudicator.py      # stage 2 LLM verdict + prompt
│   │   └── cache.py            # SQLite verdict cache keyed by rules_hash
│   ├── engine/
│   │   ├── bookwalk.py         # depth-walking + slippage, both platforms
│   │   └── signal.py           # net-margin, both directions, thresholding
│   ├── tracking/               # §9 observability — the stable spine
│   │   ├── ids.py              #   deterministic entity_id / pair_id / opp_id
│   │   ├── stages.py           #   Stage + DropReason enums, StageResult
│   │   ├── runstate.py         #   RunState model + atomic JSON serialization
│   │   ├── statusboard.py      #   generic plain-text board renderer (reads RunState)
│   │   └── structlog.py        #   JSON-lines structured logger
│   ├── alerting/
│   │   ├── telegram.py
│   │   └── console.py
│   ├── store/
│   │   ├── sqlite.py           # tables: markets, pairs, verdicts, opportunities,
│   │   │                       #         drops, cycles, stage_stats
│   │   └── views.sql           # the predefined v_* VIEW definitions (§9.8)
│   ├── config.py               # typed config loader (pydantic)
│   └── main.py                 # orchestration loop; --simulate flag
└── tests/
    ├── test_kalshi_orderbook_reconstruction.py   # the NO-bid→YES-ask invariant
    ├── test_fees_kalshi.py                        # peak at 0.50, symmetry, tails→~0
    ├── test_fees_polymarket.py                    # per-category rates, geopolitics=0
    ├── test_bookwalk_slippage.py                  # partial fills, insufficient depth
    ├── test_signal_both_directions.py
    ├── test_adjudicator_schema.py                 # verdict JSON parses & validates
    ├── test_ids_deterministic.py                  # same inputs → same ids across restarts
    ├── test_stage_funnel.py                       # drops sum to n_in - n_out for every stage
    ├── test_runstate_serialization.py             # RunState round-trips; atomic write
    └── test_store_views.py                        # v_* views return expected shapes
```

---

## 11. Build order (milestones for the coding agent)

Implement in this sequence; each milestone should be independently runnable/testable. Note that
the tracking primitives (§9) are foundational and appear early — every later stage reports through
them, so they are not bolted on at the end.

1. **Schema + config + fee models + tracking primitives.** Pure functions, fully unit-tested.
   Verify Kalshi fee peaks at $0.0175 @ 0.50 and is symmetric; verify Polymarket geopolitics = 0
   and the category table. In the same milestone, define the `Stage` / `DropReason` enums, the
   `StageResult` shape, and the deterministic ID helpers (§9.2–9.4) — they're the spine everything
   else reports through, so they exist before any stage does.
2. **Kalshi client (REST).** Discovery + order book, with the **NO-bid→YES-ask reconstruction**
   and its dedicated test. Print best YES/NO asks for a live market.
3. **Polymarket client (Gamma + CLOB).** Discovery via Gamma, book via CLOB, `condition_id ↔
   token_id` mapping, staleness note respected. Print best asks for a live market.
4. **Normalize** both into `NormalizedMarket`. Confirm one geopolitics market from each platform
   round-trips into the common schema.
5. **Recall filter (stage 1).** Given the two normalized sets, emit candidate pairs *and a
   `StageResult`* (with `LOW_SIMILARITY` / `NO_TIME_OVERLAP` drops recorded). Eyeball
   precision/recall on a handful of known overlapping events.
6. **LLM adjudicator (stage 2) + cache.** Structured JSON verdict, SQLite cache keyed by
   `rules_hash`, confidence threshold, manual overrides; emit a `StageResult` with
   `LLM_NOT_SAME_EVENT` / `LOW_CONFIDENCE` drops. Verify it correctly *rejects* a deliberately
   mismatched pair (e.g. different resolution windows).
7. **Book-walk + signal engine.** Net margin, both directions, partial-fill handling; emit a
   `StageResult` with `INSUFFICIENT_DEPTH` / `BELOW_THRESHOLD` / `NEGATIVE_MARGIN` drops. Test with
   recorded books in `--simulate` mode first.
8. **Tracking & state layer (§9).** `RunState` assembly from the per-stage `StageResult`s, atomic
   `latest.json`, the plain-text status board renderer, the JSON-lines logger, and the SQLite
   tables + `v_*` views. Write `STATE_SCHEMA.md`. This is where the funnel becomes visible; get it
   working before wiring the live loop so you can *see* every subsequent run.
9. **Alerting.** Telegram + console, reading from `RunState.active_opportunities`; de-dup via the
   `DUPLICATE` reason. (The persistence half already exists from milestone 8.)
10. **Orchestration loop.** Wire it all together with poll intervals and backoff, appending a
    `cycles` + `stage_stats` row each cycle and rendering the board. Run live in detect-only mode
    over geopolitics and watch the funnel.
11. **(Optional, later)** WebSocket feeds for lower latency; a dashboard rendering over the same
    `RunState` + views; add a third platform; expand categories (turning on the Polymarket fee
    curve for non-geopolitics). Each of these is additive per §9.11.

---

## 12. Testing & validation strategy

- **Unit:** fee curves (peak/symmetry/tails), order-book reconstruction, book-walking with
  partial fills, verdict-JSON parsing.
- **Tracking invariants:** for every stage, `sum(drops.values()) == n_in - n_out` (nothing
  vanishes untracked); IDs are deterministic across restarts; `RunState` round-trips through JSON;
  every `v_*` view returns the expected columns. These guard the property that the funnel never
  lies.
- **Golden pairs:** a small hand-labeled set of known same/different event pairs to regression-test
  the matching layer's precision.
- **Simulation mode:** replay recorded books so the engine is testable offline and demoable
  without live API load.
- **Shadow validation:** run live in detect-only mode and log everything; after resolution, check
  whether flagged "arbs" were real and capturable. Do this **before** ever trusting the signal for
  real money. Track a false-positive rate driven by (a) bad matches and (b) spreads that vanished
  under depth/slippage.

---

## 13. Key risks & how the design mitigates them

| Risk | Mitigation baked into this plan |
|------|--------------------------------|
| **Opaque pipeline** — "why are there no opportunities?" is unanswerable | Reason-coded funnel + `drops` ledger + `v_drop_breakdown_24h` + `v_pair_trace`; every drop is categorical and traceable to an id (§9). |
| **Subtle event mismatch** (different resolution rules) → fake arb | LLM adjudication on full rules text; cautious bias; `resolution_caveats` surfaced in every alert; manual overrides. |
| **Fees eaten silently** | Per-leg, per-category fee curves evaluated at actual fill price; geopolitics-first for a fee-free Polymarket leg. |
| **Order-book reconstruction bug (Kalshi)** | Dedicated, unit-tested `no_bid → yes_ask` function; never inline. |
| **condition_id vs token_id mix-up (Polymarket)** | Explicit mapping in the adapter; token IDs only for book queries. |
| **Stale Gamma prices** | Read live CLOB book for pricing; Gamma only for discovery/matching. |
| **Depth/slippage illusion** | Walk the book for a target size; report partial fills and max achievable size. |
| **Float rounding on money** | `Decimal` everywhere. |
| **LLM cost/latency** | Out of hot path; cached by `rules_hash`; only re-run on rule changes. |
| **Scope creep into execution** | Explicit non-goal; no wallet/keys for trading anywhere in the codebase. |
| **Rate limits / API flakiness** | Exponential backoff on 429; local caching of metadata; polite poll intervals. |

---

## 14. Explicit non-goals (do not build these in v1)

- No order placement / execution / wallet / USDC / Polygon signing / Kalshi trading keys.
- No auto-trading, no position management, no PnL-from-real-fills.
- No multi-outcome/combinatorial arbitrage yet (Type 3) — binary same-event only for v1.
- No non-geopolitics categories in v1 (keeps the Polymarket leg fee-free and the universe small).
- No dashboard in v1 (console + Telegram is enough to prove the concept).

---

## 15. Config sketch (`config.yaml`)

```yaml
categories:
  polymarket: ["geopolitics"]      # v1: fee-free leg
  kalshi: ["World", "Politics"]    # map to whatever Kalshi categories overlap
matching:
  recall_top_k: 5
  recall_min_similarity: 0.55
  llm_model: "claude-<reasoning-model>"
  min_confidence: 0.80
engine:
  target_size_pairs: 500           # share-pairs to size the book-walk against
  net_threshold_per_pair: 0.02     # only alert above this (buffers slippage)
poll:
  discovery_interval_sec: 300
  price_interval_sec: 5
  backoff_base_sec: 1
fees:
  kalshi_multiplier_default: 0.07
  polymarket_fee_rates:
    geopolitics: 0.00
    politics: 0.04
    crypto: 0.07
    sports: 0.03
    economics: 0.05
alerting:
  telegram_enabled: true
tracking:
  state_dir: "./state"             # latest.json, STATUS.txt, events.jsonl, arb.db live here
  status_board_stdout: true        # also print the board to stdout each cycle
  sqlite_path: "./state/arb.db"
  structured_log_path: "./state/events.jsonl"
  schema_version: 1
  keep_dropped_ids: true           # persist per-reason dropped ids for tracing (costs some space)
  drop_id_retention_cycles: 200    # trim dropped-id detail older than this to bound db growth
```

---

## 16. Pointers / references for the agent

- Kalshi API docs: `https://docs.kalshi.com` (market data quick start, orderbook responses,
  websockets).
- Kalshi fee schedule: `https://kalshi.com/fee-schedule`.
- Polymarket API docs: `https://docs.polymarket.com` (introduction, CLOB, Gamma, trading/fees).
- Polymarket Python SDK: `py-clob-client`; agent skills repo `Polymarket/agent-skills`.
- Prior art to study (not copy): `ImMike/polymarket-arbitrage`,
  `WSOL12/Polymarket-Kalshi-Arbitrage-Trading-Bot-BTC`.

> When in doubt, prefer correctness and clarity of the *fee and matching* logic over speed. The
> whole point of this tool is that its "arbitrage" flags are trustworthy — which means every flag
> must survive real fees, real depth, and a genuine same-event check.
