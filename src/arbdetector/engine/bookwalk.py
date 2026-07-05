"""Order-book depth walking (Milestone 7, plan §7, §11).

STUB. Will implement size-weighted fill-price computation for a target size
in share-pairs: walk :class:`~arbdetector.schema.OrderBookLevel` lists (both
platforms normalize to ask books, best first), report partial fills and max
achievable size when depth is insufficient (INSUFFICIENT_DEPTH). On
Polymarket, prefer the SDK's ``calculate_market_price`` when available.
"""
