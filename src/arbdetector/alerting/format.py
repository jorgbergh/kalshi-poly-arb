"""Alert message formatting (plan §8, milestone 9).

One renderer used by both sinks, so the console and Telegram messages never
drift. Input is the flat ``opportunity_summary`` dict from
:func:`arbdetector.tracking.runstate.opportunity_summary` — it already carries
every §8 field (titles, walked fills, per-leg fees, net, roi, size,
confidence, and the LLM caveats a human must see before acting).
"""

from __future__ import annotations

from decimal import Decimal

# ANSI — console only; Telegram gets plain text.
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _money(value: str) -> str:
    return f"${Decimal(value):+.4f}"


def format_opportunity(summary: dict, *, is_update: bool, plain: bool = False) -> str:
    """Render one opportunity as an alert message.

    ``is_update`` marks a re-alert (material net-margin change since the last
    one). ``plain=True`` strips ANSI for Telegram; the default is colored for
    the console.
    """
    if plain:
        green = yellow = bold = dim = reset = ""
    else:
        green, yellow, bold, dim, reset = _GREEN, _YELLOW, _BOLD, _DIM, _RESET

    header = "ARB UPDATE" if is_update else "ARB OPPORTUNITY"
    net = Decimal(summary["net_per_pair"])
    accent = green if net > 0 else yellow

    lines = [
        f"{bold}{accent}⚡ {header}{reset}  [{summary['pair_id']}]",
        f"  {bold}net/pair {_money(summary['net_per_pair'])}{reset}"
        f"   roi {Decimal(summary['roi_pct']):+.2f}%"
        f"   size {Decimal(summary['size']):.0f}",
        f"  {summary['direction']}"
        f"{'  [inverted]' if not summary['same_direction'] else ''}"
        f"   confidence {summary['confidence']:.2f}",
        f"  {dim}K:{reset} {summary['kalshi_title']}",
        f"  {dim}P:{reset} {summary['poly_title']}",
        f"  fills: YES @ {Decimal(summary['fill_yes']):.4f}  +  "
        f"NO @ {Decimal(summary['fill_no']):.4f}"
        f"   fees: {summary['fee_yes']} / {summary['fee_no']}",
    ]
    caveats = (summary.get("resolution_caveats") or "").strip()
    if caveats:
        lines.append(f"  {yellow}⚠ caveats:{reset} {caveats}")
    lines.append(f"  {dim}detected {summary['detected_ts']}{reset}")
    return "\n".join(lines)
