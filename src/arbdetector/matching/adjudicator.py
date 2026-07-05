"""Matching stage 2 — LLM adjudication (Milestone 6, plan §6, §11).

STUB. Will implement: prompt a strong reasoning model (Anthropic SDK, model
from config ``matching.llm_model``) with both FULL resolution-criteria texts,
close dates, and sources; demand a strict structured JSON verdict
(is_same_event, confidence, resolution_caveats, same_direction); bias toward
caution — flag ANY window/source/tie-handling difference. Verdicts are cached
by (kalshi_id, poly_id, rules_hash) via cache.py; supports
manual_overrides.yaml force-approve/reject. Emits a StageResult with
LLM_NOT_SAME_EVENT / LOW_CONFIDENCE / MANUAL_REJECT drops.
"""
