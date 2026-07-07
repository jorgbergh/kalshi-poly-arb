"""Deterministic correlation IDs (plan §9.2).

Same inputs -> same ID, across restarts, forever. These IDs appear in the
status board, every structured log line, and every store table, so any single
market/pair/opportunity can be traced end-to-end (e.g. via ``v_pair_trace``).

    entity_id = sha1("{platform}:{market_id}")[:8]      one market on one platform
    pair_id   = sha1("{kalshi_eid}:{poly_eid}")[:8]     a candidate cross-platform pair
    opp_id    = sha1("{pair_id}:{direction}:{ts}")[:12] one detected opportunity
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from arbdetector.schema import Direction, Platform

if TYPE_CHECKING:
    from arbdetector.schema import MatchedPair

ENTITY_ID_LEN = 8
PAIR_ID_LEN = 8
OPP_ID_LEN = 12


def _sha1_hex(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def entity_id(platform: Platform | str, market_id: str) -> str:
    """ID for one market on one platform. ``platform`` must be a valid
    :class:`Platform` value — an unknown string raises ``ValueError``."""
    platform_value = Platform(platform).value
    return _sha1_hex(f"{platform_value}:{market_id}")[:ENTITY_ID_LEN]


def pair_id(kalshi_entity_id: str, poly_entity_id: str) -> str:
    """ID for a candidate cross-platform pair. Argument order is fixed
    (kalshi first) so the ID is stable."""
    return _sha1_hex(f"{kalshi_entity_id}:{poly_entity_id}")[:PAIR_ID_LEN]


def opp_id(pair_id_: str, direction: Direction | str, detected_ts: str) -> str:
    """ID for one detected opportunity at one point in time."""
    direction_value = Direction(direction).value
    return _sha1_hex(f"{pair_id_}:{direction_value}:{detected_ts}")[:OPP_ID_LEN]


def matched_pair_id(pair: "MatchedPair") -> str:
    """The §9.2 pair id for an adjudicated pair (convenience over entity_id
    + pair_id; lives here so the tracking spine never imports the engine)."""
    return pair_id(
        entity_id(Platform.KALSHI, pair.kalshi.market_id),
        entity_id(Platform.POLYMARKET, pair.polymarket.market_id),
    )


def rules_hash(kalshi_rules: str, poly_rules: str) -> str:
    """Hash of both markets' full rules texts (plan §5/§6).

    Half of the verdict-cache key: a pair is re-adjudicated only when this
    changes. The unit separator keeps ("ab","") distinct from ("a","b")."""
    return _sha1_hex(f"{kalshi_rules}\x1f{poly_rules}")[:16]
