"""LLM verdict cache (Milestone 6, plan §6, §9.8).

STUB. Will implement the SQLite-backed verdict cache — the ``verdicts`` table
keyed by ``(pair_id, rules_hash)`` — so restarts never re-spend tokens and
re-adjudication happens only when either market's rules text changes.
The price loop reads this cache; it never calls the LLM itself (plan §4).
"""
