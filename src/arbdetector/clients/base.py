"""MarketDataClient port — the read-only interface every platform adapter
implements (plan §4, ports & adapters).

The engine, matching layer, and orchestration only ever see this interface
plus the common schema; every platform quirk (Kalshi's bid-only books,
Polymarket's condition_id/token_id split) stays inside its adapter. Adding a
third platform = one new adapter behind this port (plan §9.11).

Detector-only: this port exposes reads. No order-placement method will ever
be added here (plan §14).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal, InvalidOperation
from typing import ClassVar, Sequence

from arbdetector.schema import NormalizedMarket, OrderBookLevel, Platform


def parse_fixed_point(value: object, *, what: str) -> Decimal:
    """Parse one fixed-point money/size value into an exact ``Decimal``.

    Part of the port's contract, shared by all adapters: both platforms serve
    fixed-point STRINGS, so a float here means an upstream parsing bug about
    to poison the money math — refuse it outright (plan §4, §13).
    """
    if isinstance(value, float):
        raise TypeError(
            f"refusing float {value!r} for {what} — expected a fixed-point string "
            f"(plan §2.1/§2.2)"
        )
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"unparseable {what}: {value!r}") from exc


class MarketDataClient(ABC):
    """Read-only market-data access for one platform."""

    platform: ClassVar[Platform]

    @abstractmethod
    def discover_markets(self, categories: Sequence[str]) -> list[NormalizedMarket]:
        """All open binary markets whose category matches ``categories``
        (case-insensitive), with books left EMPTY.

        Discovery runs on the slow cadence (config
        ``poll.discovery_interval_sec``); books are refreshed on the fast
        cadence via :meth:`fetch_order_book`.
        """

    @abstractmethod
    def fetch_order_book(
        self, market: NormalizedMarket
    ) -> tuple[list[OrderBookLevel], list[OrderBookLevel]]:
        """Fresh ``(yes_ask, no_ask)`` for ``market``, each sorted best
        (cheapest) first.

        The adapter uses whatever identifiers it needs from ``market``
        (ticker on Kalshi, outcome token ids on Polymarket).
        """

    def close(self) -> None:
        """Release underlying resources (HTTP connections). Default: no-op."""

    def __enter__(self) -> "MarketDataClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False
