"""Alerting sinks + the alert funnel stage (plan §8, milestone 9).

Reads the threshold stage's survivors, sends each NEW or materially-changed
opportunity to every enabled sink, and de-duplicates the rest via
``DropReason.DUPLICATE``. De-dup identity is ``(pair_id, direction)`` — NOT
``opp_id``, which hashes in ``detected_ts`` and so changes every cycle.
"Material change" is a net-per-pair delta vs. the last delivered alert
(config ``alerting.material_change_per_pair``), read from the ``alerts``
table so de-dup is restart-safe.
"""

from __future__ import annotations

import time
from collections import defaultdict
from decimal import Decimal
from typing import Protocol, Sequence

from arbdetector.schema import ArbOpportunity
from arbdetector.store.sqlite import Store
from arbdetector.tracking import DropReason, Stage, StageResult
from arbdetector.tracking.ids import matched_pair_id, opp_id
from arbdetector.tracking.runstate import opportunity_summary


class Alerter(Protocol):
    """A sink. ``enabled`` gates it; ``send`` delivers or raises."""

    name: str
    enabled: bool

    def send(self, summary: dict, *, is_update: bool) -> None: ...


def _should_alert(
    opportunity: ArbOpportunity, last: dict | None, material_delta: Decimal
) -> tuple[bool, bool]:
    """Return ``(should_alert, is_update)``.

    New identity -> alert (not an update). Seen before -> alert only if the
    net margin moved at least ``material_delta`` (an update); otherwise it is
    a DUPLICATE.
    """
    if last is None:
        return True, False
    moved = abs(opportunity.net_per_pair - Decimal(last["net_per_pair"]))
    return (moved >= material_delta, True)


def run_alert(
    opportunities: Sequence[ArbOpportunity],
    *,
    store: Store,
    alerters: Sequence[Alerter],
    material_delta: Decimal,
    cycle_id: int,
    ts: str,
) -> tuple[list[ArbOpportunity], StageResult, int]:
    """Alert stage: threshold survivors in, delivered opportunities out.

    Returns ``(emitted, stage_result, send_error_count)``. A per-sink send
    failure is caught and counted — the opportunity is still recorded as
    alerted so it won't spam next cycle, but the failure feeds the cycle's
    ``error_count``, never a silent DUPLICATE.
    """
    started = time.perf_counter()
    active = [a for a in alerters if a.enabled]
    emitted: list[ArbOpportunity] = []
    dropped: dict[DropReason, list[str]] = defaultdict(list)
    errors = 0

    for opportunity in opportunities:
        pid = matched_pair_id(opportunity.pair)
        direction = opportunity.direction.value
        last = store.last_alert(pid, direction)
        should, is_update = _should_alert(opportunity, last, material_delta)
        if not should:
            dropped[DropReason.DUPLICATE].append(pid)
            continue

        summary = opportunity_summary(opportunity)
        delivered: list[str] = []
        for alerter in active:
            try:
                alerter.send(summary, is_update=is_update)
                delivered.append(alerter.name)
            except Exception:
                errors += 1  # never let a sink failure crash the cycle
        store.record_alert(
            cycle_id,
            opportunity,
            opp_id=opp_id(pid, opportunity.direction, opportunity.detected_ts),
            pair_id=pid,
            channels=delivered,
            alerted_ts=ts,
        )
        emitted.append(opportunity)

    result = StageResult(
        stage=Stage.ALERT,
        n_in=len(opportunities),
        n_out=len(emitted),
        drops={reason: len(ids) for reason, ids in dropped.items()},
        dropped_ids=dict(dropped),
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    return emitted, result, errors


def build_alerters(alerting_config) -> list[Alerter]:
    """Construct the enabled sinks from config + environment (§8)."""
    from arbdetector.alerting.console import ConsoleAlerter
    from arbdetector.alerting.telegram import TelegramAlerter

    alerters: list[Alerter] = []
    if alerting_config.console_enabled:
        alerters.append(ConsoleAlerter())
    if alerting_config.telegram_enabled:
        alerters.append(TelegramAlerter.from_env())
    return alerters
