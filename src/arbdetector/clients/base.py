"""MarketDataClient port — the interface every platform adapter implements.

STUB (Milestone 2, plan §11). Will define the read-only contract the engine
depends on: discover markets in the configured categories, fetch a full order
book, and map both into :class:`~arbdetector.schema.NormalizedMarket`.
Adding a third platform later = one new adapter behind this port (plan §9.11).
"""
