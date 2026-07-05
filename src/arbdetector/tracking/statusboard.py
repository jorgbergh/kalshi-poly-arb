"""Plain-text status board renderer (Milestone 8, plan §9.6).

STUB. Will render ``state/STATUS.txt`` (and optionally stdout) from RunState:
fixed-width funnel table (in / out / dropped / top drop reason per stage),
active opportunities, health, store stats. The renderer is generic over the
funnel — it loops StageResults and never hard-codes stage names, so new
stages appear automatically (plan §9.11).
"""
