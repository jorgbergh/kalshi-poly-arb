"""Matching stage 1 — recall filter (plan §6, milestone 5).

Cheap, deterministic candidate-pair generation. The job is RECALL, not
precision: surface everything that might be the same real-world event and let
the LLM adjudicator (stage 2, M6) do the precise — and expensive — judging.
Two consequences worth remembering:

- Polarity must not block matching: "meet before 2027" and "NOT meet before
  2027" are the same event inverted (`same_direction=false`, plan §6), so
  similarity looks at shared subject tokens, never at phrasing direction.
- The filter runs on every discovery cycle, so it is pure Python with an
  inverted index — only token-sharing pairs are ever scored.

Flagged decisions (2026-07-05):
- Similarity is TF-IDF cosine over normalized title tokens (no embeddings —
  a torch-sized dependency for v1 recall; the scoring is isolated behind
  small functions so embeddings can replace it later without touching the
  stage). Tokens are prefix-truncated to blunt spelling variants
  (Zelensky/Zelenskyy -> "zelens").
- Close-time blocking: |close_A − close_B| <= config
  ``close_time_tolerance_days``. Measured live: same-event skew is hours
  (04:59Z vs 00:00Z); different-window events differ by months.
- StageResult counts MARKETS in/out, not pairs (the §9.6 sketch mixes units;
  the drops invariant needs one unit). A market survives if it appears in at
  least one emitted pair; otherwise it drops with exactly one reason. The
  pair count is ``len(candidates)``, reported separately. Documented in
  STATE_SCHEMA.md.
"""

from __future__ import annotations

import math
import re
import time
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from arbdetector.config import CategoriesConfig, MatchingConfig
from arbdetector.schema import NormalizedMarket, Platform
from arbdetector.tracking import DropReason, Stage, StageResult, entity_id, pair_id

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_PREFIX_LEN = 6
# Deliberately tiny and polarity-free: "not" must survive tokenization so it
# can't be the reason an inverted pair is missed (it just adds one
# non-matching token). Discriminating words stay.
_STOPWORDS = frozenset(
    {"will", "the", "a", "an", "be", "by", "in", "of", "to", "on", "at", "and", "or", "for"}
)


@dataclass(frozen=True)
class CandidatePair:
    """A recalled (not yet adjudicated) cross-platform pair.

    In-memory shape only for now; persisted into the ``pairs`` table in M8.
    ``similarity`` is a recall score, NOT a same-event probability — the
    adjudicator's ``confidence`` (plan §5 MatchedPair) is the real verdict.
    """

    pair_id: str
    kalshi: NormalizedMarket
    polymarket: NormalizedMarket
    similarity: float


# ---------------------------------------------------------------------------
# Text similarity (pure, deterministic)
# ---------------------------------------------------------------------------


def normalize_tokens(text: str) -> list[str]:
    """Lowercase word/number tokens, stopwords removed, prefix-truncated."""
    return [
        token[:_PREFIX_LEN]
        for token in _TOKEN_RE.findall(text.lower())
        if token not in _STOPWORDS
    ]


def _idf(token_sets: Sequence[set[str]]) -> dict[str, float]:
    n = len(token_sets)
    doc_freq = Counter(token for tokens in token_sets for token in tokens)
    return {
        token: math.log((1 + n) / (1 + freq)) + 1.0 for token, freq in doc_freq.items()
    }


def _weight_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    """L2-normalized tf-idf weights for one title."""
    weights = {
        token: count * idf[token] for token, count in Counter(tokens).items()
    }
    norm = math.sqrt(sum(w * w for w in weights.values()))
    if norm == 0:
        return {}
    return {token: w / norm for token, w in weights.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(b) < len(a):
        a, b = b, a
    return sum(weight * b[token] for token, weight in a.items() if token in b)


# ---------------------------------------------------------------------------
# Close-time blocking
# ---------------------------------------------------------------------------


def _parse_close(close_time: str) -> datetime | None:
    """ISO 8601 -> aware datetime; None when missing/unparseable (a market
    without a readable close time can't be time-matched and drops as
    NO_TIME_OVERLAP)."""
    if not close_time:
        return None
    try:
        parsed = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _within(
    a: datetime | None, b: datetime | None, tolerance: timedelta
) -> bool:
    return a is not None and b is not None and abs(a - b) <= tolerance


def _any_close_within(
    close: datetime | None, sorted_closes: list[datetime], tolerance: timedelta
) -> bool:
    """True if any counterpart close time is within tolerance (bisect on the
    sorted list — this runs once per market, not per pair)."""
    if close is None or not sorted_closes:
        return False
    i = bisect_left(sorted_closes, close)
    for j in (i - 1, i):
        if 0 <= j < len(sorted_closes) and abs(sorted_closes[j] - close) <= tolerance:
            return True
    return False


# ---------------------------------------------------------------------------
# The stage
# ---------------------------------------------------------------------------


def run_recall(
    kalshi_markets: Sequence[NormalizedMarket],
    poly_markets: Sequence[NormalizedMarket],
    *,
    matching: MatchingConfig,
    categories: CategoriesConfig,
) -> tuple[list[CandidatePair], StageResult]:
    """Generate top-K candidate pairs per Kalshi market and the stage's
    funnel report.

    Blocking order per plan §6: configured category (defensive — discovery
    already filters), close-time overlap, then similarity floor + top-K.
    Candidates come back sorted best-first, ties broken by pair_id so runs
    are fully deterministic.
    """
    started = time.perf_counter()
    tolerance = timedelta(days=matching.close_time_tolerance_days)
    kalshi_wanted = {c.strip().lower() for c in categories.kalshi}
    poly_wanted = {c.strip().lower() for c in categories.polymarket}

    dropped: dict[DropReason, list[str]] = defaultdict(list)

    # -- category blocking (defensive) --------------------------------------
    kalshi_valid: list[NormalizedMarket] = []
    for market in kalshi_markets:
        if market.category.strip().lower() in kalshi_wanted:
            kalshi_valid.append(market)
        else:
            dropped[DropReason.CATEGORY_MISMATCH].append(
                entity_id(market.platform, market.market_id)
            )
    poly_valid: list[NormalizedMarket] = []
    for market in poly_markets:
        if market.category.strip().lower() in poly_wanted:
            poly_valid.append(market)
        else:
            dropped[DropReason.CATEGORY_MISMATCH].append(
                entity_id(market.platform, market.market_id)
            )

    # -- shared tf-idf vocabulary over both sides ---------------------------
    kalshi_tokens = [normalize_tokens(m.title) for m in kalshi_valid]
    poly_tokens = [normalize_tokens(m.title) for m in poly_valid]
    idf = _idf([set(t) for t in kalshi_tokens + poly_tokens])
    kalshi_weights = [_weight_vector(t, idf) for t in kalshi_tokens]
    poly_weights = [_weight_vector(t, idf) for t in poly_tokens]

    poly_closes = [_parse_close(m.close_time) for m in poly_valid]
    kalshi_closes = [_parse_close(m.close_time) for m in kalshi_valid]
    sorted_poly_closes = sorted(c for c in poly_closes if c is not None)
    sorted_kalshi_closes = sorted(c for c in kalshi_closes if c is not None)

    token_index: dict[str, list[int]] = defaultdict(list)
    for idx, weights in enumerate(poly_weights):
        for token in weights:
            token_index[token].append(idx)

    # -- per-Kalshi-market candidate generation -----------------------------
    candidates: list[CandidatePair] = []
    surviving_poly_indices: set[int] = set()

    for k_idx, kalshi_market in enumerate(kalshi_valid):
        kalshi_eid = entity_id(Platform.KALSHI, kalshi_market.market_id)
        close = kalshi_closes[k_idx]
        if not _any_close_within(close, sorted_poly_closes, tolerance):
            dropped[DropReason.NO_TIME_OVERLAP].append(kalshi_eid)
            continue

        weights = kalshi_weights[k_idx]
        candidate_indices = {
            p_idx for token in weights for p_idx in token_index.get(token, ())
        }
        scored = [
            (score, p_idx)
            for p_idx in candidate_indices
            if _within(close, poly_closes[p_idx], tolerance)
            and (score := _cosine(weights, poly_weights[p_idx]))
            >= matching.recall_min_similarity
        ]
        if not scored:
            dropped[DropReason.LOW_SIMILARITY].append(kalshi_eid)
            continue

        scored.sort(key=lambda item: (-item[0], poly_valid[item[1]].market_id))
        for score, p_idx in scored[: matching.recall_top_k]:
            poly_market = poly_valid[p_idx]
            candidates.append(
                CandidatePair(
                    pair_id=pair_id(
                        kalshi_eid, entity_id(Platform.POLYMARKET, poly_market.market_id)
                    ),
                    kalshi=kalshi_market,
                    polymarket=poly_market,
                    similarity=score,
                )
            )
            surviving_poly_indices.add(p_idx)

    # -- poly-side accounting (every input market gets survive-or-one-reason)
    for p_idx, poly_market in enumerate(poly_valid):
        if p_idx in surviving_poly_indices:
            continue
        poly_eid = entity_id(Platform.POLYMARKET, poly_market.market_id)
        if not _any_close_within(poly_closes[p_idx], sorted_kalshi_closes, tolerance):
            dropped[DropReason.NO_TIME_OVERLAP].append(poly_eid)
        else:
            dropped[DropReason.LOW_SIMILARITY].append(poly_eid)

    surviving_kalshi = {c.kalshi.market_id for c in candidates}
    n_in = len(kalshi_markets) + len(poly_markets)
    n_out = len(surviving_kalshi) + len(surviving_poly_indices)

    candidates.sort(key=lambda c: (-c.similarity, c.pair_id))
    result = StageResult(
        stage=Stage.RECALL,
        n_in=n_in,
        n_out=n_out,
        drops={reason: len(ids) for reason, ids in dropped.items()},
        dropped_ids=dict(dropped),
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return candidates, result


# ---------------------------------------------------------------------------
# Milestone-5 acceptance smoke (plan §11): eyeball precision/recall live.
#   .venv/bin/python -m arbdetector.matching.recall --top 20
# ---------------------------------------------------------------------------


def _smoke(argv: Sequence[str] | None = None) -> None:
    import argparse

    from arbdetector.clients.kalshi import KalshiClient
    from arbdetector.clients.polymarket import PolymarketClient
    from arbdetector.config import load_config

    parser = argparse.ArgumentParser(
        description="Run live discovery on both platforms, then the recall filter."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--top", type=int, default=20, help="candidate pairs to print")
    parser.add_argument(
        "--min-similarity", type=float, help="override config recall_min_similarity"
    )
    args = parser.parse_args(argv)

    config = load_config(args.config)
    matching = config.matching
    if args.min_similarity is not None:
        matching = matching.model_copy(update={"recall_min_similarity": args.min_similarity})

    with KalshiClient() as kalshi_client:
        kalshi_markets = kalshi_client.discover_markets(config.categories.kalshi)
    print(f"kalshi:     {len(kalshi_markets):6d} open binary markets in {config.categories.kalshi}")
    with PolymarketClient() as poly_client:
        poly_markets = poly_client.discover_markets(config.categories.polymarket)
    print(f"polymarket: {len(poly_markets):6d} open binary markets in {config.categories.polymarket}")

    candidates, result = run_recall(
        kalshi_markets, poly_markets, matching=matching, categories=config.categories
    )

    drops = ", ".join(f"{r.value}={n}" for r, n in sorted(result.drops.items()))
    print(
        f"\nrecall: in={result.n_in} out={result.n_out} (markets)  "
        f"pairs={len(candidates)}  floor={matching.recall_min_similarity}  "
        f"[{result.duration_ms:.0f}ms]\n  drops: {drops}\n"
    )
    for c in candidates[: args.top]:
        print(f"  [{c.pair_id}] sim={c.similarity:.3f}")
        print(f"    K: {c.kalshi.title[:88]}  (close {c.kalshi.close_time[:10]})")
        print(f"    P: {c.polymarket.title[:88]}  (close {c.polymarket.close_time[:10]})")


if __name__ == "__main__":
    _smoke()
