"""Colored console alerter (plan §8, milestone 9)."""

from __future__ import annotations

from arbdetector.alerting.format import format_opportunity


class ConsoleAlerter:
    """Prints colored alerts to stdout. No dependencies, always available."""

    name = "console"
    enabled = True

    def send(self, summary: dict, *, is_update: bool) -> None:
        print(format_opportunity(summary, is_update=is_update))
