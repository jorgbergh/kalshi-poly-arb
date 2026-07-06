# Prediction-Market Arbitrage Detector (Kalshi ↔ Polymarket)

A Python service that detects cross-platform arbitrage between Kalshi and Polymarket:
it ingests public market data, matches "the same real-world event" across platforms
(cheap recall filter + cached LLM adjudication of resolution rules), computes
**net-of-fee** margins by walking real order-book depth, and alerts when the net margin
clears a threshold — with a fully reason-coded tracking funnel so you can always answer
*"where is this item in the pipeline, and if it dropped, exactly why?"*

> **This is a detector, not a trader.** No order placement, no wallets, no USDC, no
> Polygon RPC, no Kalshi trading keys — anywhere in the codebase. See plan §14.

## The authoritative spec

**[PREDICTION_MARKET_ARB_DETECTOR_PLAN.md](PREDICTION_MARKET_ARB_DETECTOR_PLAN.md)** is
the full specification: domain background, API quirks, fee formulas, matching design,
tracking design, and the milestone build order (§11). Read it first.

[STATE_SCHEMA.md](STATE_SCHEMA.md) is the registry of every tracked entity, stage,
drop reason, table, and view (plan §9.10).

## Status

| Milestone (plan §11) | Status |
|---|---|
| 1. Schema + config + fee models + tracking primitives | ✅ implemented & tested |
| 2. Kalshi client (REST, NO-bid→YES-ask reconstruction) | ✅ implemented & tested |
| 3. Polymarket client (Gamma + CLOB) | ✅ implemented & tested |
| 4. Normalization into common schema | ✅ confirmed live, both platforms |
| 5. Recall filter (matching stage 1) | ✅ implemented & tested |
| 6. LLM adjudicator + verdict cache (stage 2) | ✅ implemented & tested |
| 7. Book-walk + signal engine | 🟡 partial: §3.4 net margin, both directions, top-of-book only |
| 8. Tracking & state layer (RunState, board, store) | ⬜ stub (primitives done) |
| 9. Alerting (Telegram + console) | ⬜ stub |
| 10. Orchestration loop | ⬜ stub |

## Setup & tests

Requires Python 3.11+.

```bash
python3 -m venv .venv            # or: uv venv --python 3.12
source .venv/bin/activate
pip install -e ".[dev]"          # or: uv pip install -e ".[dev]"
pytest                           # milestone-1 suite; future milestones show as skipped
```

Tests are pure/offline — no network, no API keys needed. Runtime secrets (LLM key,
Telegram token) go in `.env`, copied from [.env.example](.env.example).

Live smoke check (read-only, no auth — prints best derived YES/NO asks):

```bash
python -m arbdetector.clients.kalshi --limit 3            # discover World/Politics
python -m arbdetector.clients.kalshi --ticker KXELONMARS-99
python -m arbdetector.clients.polymarket --limit 3        # discover geopolitics
python -m arbdetector.clients.polymarket --slug putin-out-before-2027

# discover both platforms, generate candidate pairs + the recall funnel:
python -m arbdetector.matching.recall --top 20

# the full detection sweep: discover -> recall -> LLM-adjudicate (cached,
# needs ANTHROPIC_API_KEY in .env) -> price blessed pairs net of fees:
python -m arbdetector.matching.adjudicator --margins

# price one HAND-MATCHED pair live, both directions, net of fees:
python -m arbdetector.engine.signal \
    --kalshi-ticker KXZELENSKYPUTIN-29-27 \
    --poly-slug where-will-zelenskyy-and-putin-meet-next \
    --poly-question "not meet" --poly-inverted
```

## Conventions (enforced, see plan §4/§13)

- **`Decimal` everywhere for money and sizes.** Fee functions raise `TypeError` on floats.
- **Enums, never freeform strings** for platforms, stages, drop reasons, directions.
- **Every stage reports a `StageResult`**; `sum(drops) == n_in − n_out` is enforced at
  construction — the funnel cannot silently lose items.
- Runtime output lives in `state/` (gitignored): `latest.json`, `STATUS.txt`,
  `events.jsonl`, `arb.db`.
