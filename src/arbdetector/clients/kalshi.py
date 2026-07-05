"""Kalshi REST client (Milestone 2, plan §2.1, §11).

STUB. Will implement:
- Discovery via ``GET /markets`` (cursor-paginated, ``status=open``) and
  ``GET /events/{event_ticker}`` for the category filter.
- Order books via ``GET /markets/{ticker}/orderbook``.
- **THE critical quirk:** Kalshi returns only BIDS for both sides. The YES ask
  book must be derived from NO bids (``yes_ask = 1 - no_bid``) in a dedicated,
  unit-tested function (tests/test_kalshi_orderbook_reconstruction.py) — never
  inline arithmetic (plan §2.1, §13).
- Dollar-string prices/quantities parsed straight into ``Decimal``.
- Rate limit ~30 req/s; polite polling. WebSocket (auth-required handshake)
  deferred to a later milestone.
"""
