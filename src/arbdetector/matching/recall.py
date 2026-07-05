"""Matching stage 1 — recall filter (Milestone 5, plan §6, §11).

STUB. Will implement cheap candidate-pair generation: restrict to configured
categories and overlapping close-time windows, score title/description
similarity, keep top-K above the floor. Emits a
:class:`~arbdetector.tracking.stages.StageResult` with CATEGORY_MISMATCH /
NO_TIME_OVERLAP / LOW_SIMILARITY drops recorded.
"""
