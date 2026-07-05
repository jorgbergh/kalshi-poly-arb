"""The matching layer (plan §6) — the hard part of the project.

Stage 1 (``recall``): cheap, high-recall candidate generation every cycle.
Stage 2 (``adjudicator``): cached LLM verdicts on full resolution-rules text,
out of the hot path. False "same event" verdicts are the expensive failure
mode; the design biases toward caution.
"""
