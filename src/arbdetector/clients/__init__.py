"""Platform market-data clients (ports & adapters, plan §4).

Each platform hides behind the ``MarketDataClient`` port in ``base.py`` so the
engine never sees platform-specific quirks. Read-only everywhere — these
clients must never gain order-placement capabilities (plan §14).
"""
