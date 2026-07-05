"""Orchestration loop (Milestone 10, plan §11).

STUB. Will wire the full cycle — discovery -> recall -> adjudicate -> price ->
threshold -> alert — with poll intervals and backoff from config, emitting a
StageResult per stage, appending cycles/stage_stats rows, atomically writing
state/latest.json, and rendering the status board. Supports ``--simulate``
(replay recorded books, plan §7) for offline demo/testing.
"""

from __future__ import annotations


def main() -> None:
    raise NotImplementedError(
        "Milestone 10: orchestration loop not yet implemented (plan §11). "
        "Milestone 1 (schema/config/fees/tracking primitives) is testable via pytest."
    )


if __name__ == "__main__":
    main()
