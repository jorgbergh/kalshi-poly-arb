"""Net-margin signal engine (Milestone 7, plan §3.4, §7, §11).

STUB. Will implement, per blessed MatchedPair per cycle: pull live books,
walk depth for the target size, evaluate BOTH directions
(YES@kalshi+NO@poly and NO@kalshi+YES@poly) with per-leg per-category
FeeModels, emit :class:`~arbdetector.schema.ArbOpportunity` when
``net_per_pair > config.engine.net_threshold_per_pair``, and a StageResult
with EMPTY_BOOK / STALE_BOOK / INSUFFICIENT_DEPTH / NEGATIVE_MARGIN /
BELOW_THRESHOLD drops. Testable offline via recorded books (--simulate).
"""
