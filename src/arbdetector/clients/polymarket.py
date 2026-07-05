"""Polymarket client: Gamma discovery + CLOB order books (Milestone 3, plan §2.2, §11).

STUB. Will implement:
- Discovery via Gamma (``gamma-api.polymarket.com``, ``?active=true&closed=false``,
  category via ``tag_id``) — used for matching only, its prices can be stale.
- Order books via CLOB (``clob.polymarket.com`` ``/book?token_id=...``),
  preferring ``py-clob-client``'s ``calculate_market_price`` for depth-walking.
- **THE classic bug to avoid:** condition IDs identify a *market*; token IDs
  identify an *outcome*. Book queries take TOKEN ids; Gamma provides the
  condition_id <-> token_id mapping, kept explicit in this adapter (plan §13).
- Exponential backoff on HTTP 429.
"""
