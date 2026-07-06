"""Matching stage 2 — LLM adjudication (plan §6, milestone 6).

The recall filter (stage 1) optimizes recall; this stage buys precision with
a strong reasoning model reading BOTH markets' full resolution rules. A false
"same event" verdict is the expensive failure mode — both legs held to
resolution can diverge — so the prompt is biased toward rejection, and every
difference it finds lands in ``resolution_caveats``, which alerts surface to
the human before any action (plan §8).

Out of the hot path (plan §4): verdicts are cached by ``(pair_id,
rules_hash)`` in SQLite (matching/cache.py); the API is called at most once
per pair per rules version, across restarts. ``manual_overrides.yaml`` lets a
human force-approve (``approve`` / ``approve_inverted``) or force-``reject``
specific pair_ids without spending tokens; overrides are config, so they are
never written into the cache.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbdetector.matching.cache import Verdict, VerdictCache
from arbdetector.matching.recall import CandidatePair
from arbdetector.schema import MatchedPair
from arbdetector.tracking import DropReason, Stage, StageResult
from arbdetector.tracking.ids import rules_hash

_MAX_RULES_CHARS = 6000
# Generous: on models with thinking on by default (Sonnet 5, Fable 5) the
# thinking tokens count against max_tokens — too tight a cap truncates the
# verdict JSON mid-object.
_MAX_VERDICT_TOKENS = 4096

SYSTEM_PROMPT = """\
You adjudicate whether two prediction-market contracts on different platforms \
resolve on the SAME real-world event with equivalent resolution rules.

Bias toward caution: a false "same event" verdict costs real money (the two \
legs can resolve differently); a false "different" verdict only costs a missed \
opportunity. Flag ANY difference in: resolution window or deadline, resolution \
source or authority, quantitative thresholds, tie/edge-case handling, subject \
entity (person, country, organization), or scope. Subset/superset events \
(e.g. "they meet in Turkey" vs "they meet anywhere") are NOT the same event.

Respond with ONLY a JSON object, no prose before or after it:
{
  "is_same_event": <boolean: true only if the contracts must resolve identically, up to direction>,
  "confidence": <number 0.0-1.0: your confidence in is_same_event>,
  "resolution_caveats": <string: every difference you found, however small; "" if none>,
  "same_direction": <boolean: false if YES on market A corresponds to NO on market B (inverted phrasing)>
}

If is_same_event is false, set same_direction to true (it is meaningless then).\
"""


class AdjudicationError(RuntimeError):
    """The model's reply was not a valid verdict — never guess one."""


class _VerdictModel(BaseModel):
    # strict: a bool must be a JSON bool — "yes" must not coerce to True
    model_config = ConfigDict(extra="forbid", strict=True)

    is_same_event: bool
    confidence: float = Field(ge=0.0, le=1.0)
    resolution_caveats: str
    same_direction: bool


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_prompt(pair: CandidatePair) -> str:
    """The user message: both markets' full facts, nothing else."""

    def block(label: str, market: Any) -> str:
        rules = (market.resolution_criteria or "(no rules text provided)")[:_MAX_RULES_CHARS]
        return (
            f"MARKET {label} — platform: {market.platform.value}\n"
            f"Title: {market.title}\n"
            f"Close time: {market.close_time}\n"
            f"Resolution source: {market.resolution_source or '(not stated)'}\n"
            f"Resolution criteria:\n{rules}\n"
        )

    return block("A", pair.kalshi) + "\n" + block("B", pair.polymarket)


def parse_verdict(text: str, *, verdict_ts: str | None = None) -> Verdict:
    """Strict verdict parse. Tolerates fences/prose AROUND the JSON object
    (outermost braces are extracted) but nothing wrong INSIDE it: unknown
    keys, missing keys, or out-of-range confidence raise AdjudicationError."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise AdjudicationError(f"no JSON object in verdict: {text[:200]!r}")
    try:
        model = _VerdictModel.model_validate(json.loads(text[start : end + 1]))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise AdjudicationError(f"invalid verdict JSON: {exc}") from exc
    return Verdict(
        is_same_event=model.is_same_event,
        confidence=model.confidence,
        same_direction=model.same_direction,
        resolution_caveats=model.resolution_caveats,
        verdict_ts=verdict_ts or _now_iso(),
    )


VALID_OVERRIDES = frozenset({"approve", "approve_inverted", "reject"})


def load_overrides(path: str | Path = "manual_overrides.yaml") -> dict[str, str]:
    """``overrides: {pair_id: approve|approve_inverted|reject}``; missing
    file means no overrides. Unknown values fail loudly — a typo must not
    silently become 'no override'."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    overrides = data.get("overrides") or {}
    for pair_id_, value in overrides.items():
        if value not in VALID_OVERRIDES:
            raise ValueError(
                f"manual override for {pair_id_!r} is {value!r}; "
                f"must be one of {sorted(VALID_OVERRIDES)}"
            )
    return dict(overrides)


class Adjudicator:
    """Cache-first verdict source: SQLite hit, else one API call, then cached."""

    def __init__(
        self,
        *,
        model: str,
        cache: VerdictCache,
        client: Any | None = None,
    ) -> None:
        if client is None:
            import anthropic  # deferred: tests inject a fake and never need the SDK

            client = anthropic.Anthropic()
        self._client = client
        self._model = model
        self._cache = cache
        self.cache_hits = 0
        self.api_calls = 0

    def adjudicate(self, pair: CandidatePair) -> tuple[Verdict, bool]:
        """Verdict for one candidate pair; second element is True on cache hit."""
        pair_rules_hash = rules_hash(
            pair.kalshi.resolution_criteria, pair.polymarket.resolution_criteria
        )
        cached = self._cache.get(pair.pair_id, pair_rules_hash)
        if cached is not None:
            self.cache_hits += 1
            return cached, True

        response = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_VERDICT_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_prompt(pair)}],
        )
        self.api_calls += 1
        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        verdict = parse_verdict(text)
        self._cache.put(pair.pair_id, pair_rules_hash, verdict, model=self._model)
        return verdict, False


def run_adjudicate(
    candidates: Sequence[CandidatePair],
    *,
    adjudicator: Adjudicator,
    min_confidence: float,
    overrides: Mapping[str, str] | None = None,
) -> tuple[list[MatchedPair], StageResult]:
    """Stage 2: candidates in, blessed MatchedPairs out (units: pairs).

    Per-pair API failures drop as API_ERROR and the sweep continues — one
    flaky call must not kill a 245-pair run. Only pairs with
    ``is_same_event and confidence >= min_confidence`` survive (plan §6).
    """
    started = time.perf_counter()
    overrides = overrides or {}
    blessed: list[MatchedPair] = []
    dropped: dict[DropReason, list[str]] = defaultdict(list)

    for pair in candidates:
        override = overrides.get(pair.pair_id)
        if override == "reject":
            dropped[DropReason.MANUAL_REJECT].append(pair.pair_id)
            continue
        if override in ("approve", "approve_inverted"):
            verdict = Verdict(
                is_same_event=True,
                confidence=1.0,
                same_direction=(override == "approve"),
                resolution_caveats=f"manual override: {override}",
                verdict_ts=_now_iso(),
            )
        else:
            try:
                verdict, _ = adjudicator.adjudicate(pair)
            except Exception:
                # AdjudicationError, API/transport failures: reason-coded,
                # never fatal to the sweep
                dropped[DropReason.API_ERROR].append(pair.pair_id)
                continue

        if not verdict.is_same_event:
            dropped[DropReason.LLM_NOT_SAME_EVENT].append(pair.pair_id)
            continue
        if verdict.confidence < min_confidence:
            dropped[DropReason.LOW_CONFIDENCE].append(pair.pair_id)
            continue
        blessed.append(
            MatchedPair(
                kalshi=pair.kalshi,
                polymarket=pair.polymarket,
                is_same_event=True,
                confidence=verdict.confidence,
                same_direction=verdict.same_direction,
                resolution_caveats=verdict.resolution_caveats,
                verdict_ts=verdict.verdict_ts,
                rules_hash=rules_hash(
                    pair.kalshi.resolution_criteria, pair.polymarket.resolution_criteria
                ),
            )
        )

    result = StageResult(
        stage=Stage.ADJUDICATE,
        n_in=len(candidates),
        n_out=len(blessed),
        drops={reason: len(ids) for reason, ids in dropped.items()},
        dropped_ids=dict(dropped),
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return blessed, result


# ---------------------------------------------------------------------------
# Milestone-6 acceptance sweep: discover -> recall -> adjudicate [-> price].
#   .venv/bin/python -m arbdetector.matching.adjudicator --filter zelensky
#   .venv/bin/python -m arbdetector.matching.adjudicator --margins
# ---------------------------------------------------------------------------


def _smoke(argv: Sequence[str] | None = None) -> None:
    import argparse

    from dotenv import load_dotenv

    from arbdetector.clients.kalshi import KalshiClient
    from arbdetector.clients.polymarket import PolymarketClient
    from arbdetector.config import load_config
    from arbdetector.engine.signal import (
        dump_recordings,
        live_book_fetcher,
        load_recordings,
        opportunity_id,
        recording_fetcher,
        replay_fetcher,
        run_price,
        run_threshold,
    )
    from arbdetector.fees import build_fee_registry
    from arbdetector.matching.recall import run_recall

    parser = argparse.ArgumentParser(
        description="Live detection sweep: discover, recall, LLM-adjudicate (cached), "
        "optionally price the blessed pairs by walking full book depth."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--filter", help="only pairs whose titles contain this substring")
    parser.add_argument("--limit", type=int, help="max pairs to adjudicate (best-first)")
    parser.add_argument(
        "--margins", action="store_true", help="fetch live books and price blessed pairs"
    )
    parser.add_argument("--record", metavar="FILE", help="save fetched books for replay")
    parser.add_argument("--replay", metavar="FILE", help="price from recorded books (offline)")
    args = parser.parse_args(argv)

    load_dotenv()
    config = load_config(args.config)

    with KalshiClient() as kalshi_client:
        kalshi_markets = kalshi_client.discover_markets(config.categories.kalshi)
    with PolymarketClient() as poly_client:
        poly_markets = poly_client.discover_markets(config.categories.polymarket)
    candidates, recall_result = run_recall(
        kalshi_markets, poly_markets, matching=config.matching, categories=config.categories
    )
    print(
        f"recall: {recall_result.n_in} markets in -> {len(candidates)} candidate pairs "
        f"[{recall_result.duration_ms:.0f}ms]"
    )

    if args.filter:
        needle = args.filter.lower()
        candidates = [
            c
            for c in candidates
            if needle in c.kalshi.title.lower() or needle in c.polymarket.title.lower()
        ]
        print(f"filter {args.filter!r}: {len(candidates)} pairs")
    if args.limit is not None:
        candidates = candidates[: args.limit]

    overrides = load_overrides()
    with VerdictCache(
        config.tracking.sqlite_path, schema_version=config.tracking.schema_version
    ) as cache:
        adjudicator = Adjudicator(model=config.matching.llm_model, cache=cache)
        blessed, result = run_adjudicate(
            candidates,
            adjudicator=adjudicator,
            min_confidence=config.matching.min_confidence,
            overrides=overrides,
        )
        drops = ", ".join(f"{r.value}={n}" for r, n in sorted(result.drops.items()))
        print(
            f"adjudicate: in={result.n_in} blessed={result.n_out} "
            f"[api={adjudicator.api_calls} cached={adjudicator.cache_hits} "
            f"verdicts_in_db={cache.count()}] [{result.duration_ms:.0f}ms]\n"
            f"  drops: {drops or '(none)'}\n"
        )

    for mp in blessed:
        direction_note = "" if mp.same_direction else "  [INVERTED]"
        print(f"[{mp.rules_hash[:8]}] conf={mp.confidence:.2f}{direction_note}")
        print(f"  K: {mp.kalshi.title[:86]}")
        print(f"  P: {mp.polymarket.title[:86]}")
        if mp.resolution_caveats:
            print(f"  caveats: {mp.resolution_caveats[:160]}")

    if not ((args.margins or args.record or args.replay) and blessed):
        return

    source = f"replay of {args.replay}" if args.replay else "live books"
    print(f"\npricing blessed pairs, walking depth for {config.engine.target_size_pairs} "
          f"pairs ({source}):")
    registry = build_fee_registry(config.fees)

    kalshi_client = poly_client = None
    price_now = None  # wall clock for live books
    if args.replay:
        recordings = load_recordings(args.replay)
        fetcher = replay_fetcher(recordings)
        # staleness relative to the recording's own clock: wall-clock age is
        # meaningless offline, but intra-recording skew still counts
        if recordings:
            price_now = max(books.fetched_at for books in recordings.values())
    else:
        kalshi_client, poly_client = KalshiClient(), PolymarketClient()
        fetcher = live_book_fetcher(kalshi_client, poly_client)
    sink: dict = {}
    if args.record:
        fetcher = recording_fetcher(fetcher, sink)

    try:
        priced, price_result = run_price(
            blessed,
            fetch_books=fetcher,
            target_size=config.engine.target_size_pairs,
            min_size=config.engine.min_size_pairs,
            max_book_age_sec=config.engine.max_book_age_sec,
            fee_registry=registry,
            now=price_now,
        )
    finally:
        for client in (kalshi_client, poly_client):
            if client is not None:
                client.close()

    drops = ", ".join(f"{r.value}={n}" for r, n in sorted(price_result.drops.items()))
    print(f"price: in={price_result.n_in} out={price_result.n_out} "
          f"[{price_result.duration_ms:.0f}ms]  drops: {drops or '(none)'}")

    opportunities, threshold_result = run_threshold(
        priced, threshold=config.engine.net_threshold_per_pair
    )
    drops = ", ".join(f"{r.value}={n}" for r, n in sorted(threshold_result.drops.items()))
    print(f"threshold (net > {config.engine.net_threshold_per_pair}): "
          f"in={threshold_result.n_in} out={threshold_result.n_out}  drops: {drops or '(none)'}\n")

    for mp, q in sorted(priced, key=lambda item: item[1].net_per_pair, reverse=True):
        marker = " <-- OPPORTUNITY" if q.net_per_pair > config.engine.net_threshold_per_pair else ""
        inv = "" if mp.same_direction else " [inv]"
        partial = "" if q.size >= config.engine.target_size_pairs else " (partial)"
        print(
            f"  net/pair ${q.net_per_pair:+.4f}  roi {q.roi_pct:+6.2f}%  "
            f"size {q.size:>9.2f}{partial}  {q.direction.value}{inv}  "
            f"conf {mp.confidence:.2f}  {mp.kalshi.title[:52]}{marker}"
        )

    if opportunities:
        print("\nOPPORTUNITIES:")
        for opp in opportunities:
            print(
                f"  [{opportunity_id(opp)}] {opp.direction.value}  size {opp.size:.2f}  "
                f"fills YES@{opp.fill_yes:.4f}+NO@{opp.fill_no:.4f}  "
                f"net/pair ${opp.net_per_pair:+.4f}  roi {opp.roi_pct:+.2f}%  "
                f"detected {opp.detected_ts}"
            )
            if opp.pair.resolution_caveats:
                print(f"    caveats: {opp.pair.resolution_caveats[:200]}")

    if args.record:
        dump_recordings(sink, args.record)
        print(f"\nrecorded {len(sink)} pair books -> {args.record}")


if __name__ == "__main__":
    _smoke()
