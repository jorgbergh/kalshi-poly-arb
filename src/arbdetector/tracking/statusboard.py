"""Plain-text status board renderer (plan §9.6, milestone 8).

Fixed-width, aligned, boring on purpose — the whole system state readable in
three seconds. GENERIC over the funnel: it loops ``RunState.funnel`` and
prints; no stage name is hard-coded, so adding a Stage value makes it appear
here automatically (§9.11).
"""

from __future__ import annotations

from pathlib import Path

from arbdetector.tracking.runstate import RunState, opportunity_summary

_WIDTH = 80


def render_board(state: RunState, *, uptime: str = "—") -> str:
    bar = "=" * _WIDTH
    rule = " " + "-" * (_WIDTH - 2)
    lines = [
        bar,
        f" ARB DETECTOR   cycle #{state.cycle_id:05d}   {state.cycle_ts}   "
        f"uptime {uptime}   schema v{state.schema_version}",
        bar,
        f" {'PIPELINE FUNNEL':36s}{'in':>7}{'out':>7}{'dropped':>9}   top drop reason",
        rule,
    ]
    for result in state.funnel:
        dropped = result.n_in - result.n_out
        top = ""
        if result.drops:
            reason, count = max(result.drops.items(), key=lambda item: item[1])
            top = f"{reason.value.upper()} ({count})"
        lines.append(
            f" {result.stage.value:36s}{result.n_in:>7d}{result.n_out:>7d}"
            f"{(str(dropped) if dropped else '—'):>9}   {top}"
        )
    lines.append(rule)

    lines.append(f" {'ACTIVE OPPORTUNITIES':44s}{'net/pair':>10}{'roi':>9}{'size':>10}")
    if state.active_opportunities:
        for opportunity in state.active_opportunities:
            summary = opportunity_summary(opportunity)
            title = summary["kalshi_title"][:32]
            lines.append(
                f"   [{summary['pair_id']}] \"{title}\""
                f"{'':>{max(1, 40 - len(title))}}"
                f"${float(summary['net_per_pair']):+.4f}"
                f"{float(summary['roi_pct']):>8.2f}%"
                f"{float(summary['size']):>10.2f}"
            )
            lines.append(f"      {summary['direction']}   conf {summary['confidence']:.2f}")
    else:
        lines.append("   (none)")
    lines.append(rule)

    health = "   ".join(f"{key} {value}" for key, value in state.health.items()) or "—"
    lines.append(f" HEALTH   {health}")
    store = "   ".join(f"{key} {value}" for key, value in state.store_stats.items()) or "—"
    lines.append(f" STORE    {store}")
    lines.append(bar)
    return "\n".join(lines) + "\n"


def write_board(
    state: RunState, path: str | Path, *, stdout: bool = False, uptime: str = "—"
) -> str:
    """Render to ``state/STATUS.txt`` (and optionally stdout); returns the text."""
    text = render_board(state, uptime=uptime)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    if stdout:
        print(text, end="")
    return text
