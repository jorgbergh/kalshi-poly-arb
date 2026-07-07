-- Predefined views (plan §9.8) — the "read off at a glance" layer.
-- One definition per thing-to-look-at; the status board and any future
-- dashboard read THESE, never ad-hoc queries. Loaded by store/sqlite.py.

-- What's actionable right now: the latest cycle's opportunities, joined to
-- titles and the LLM verdict (caveats surface in every alert, plan §8).
CREATE VIEW IF NOT EXISTS v_active_opportunities AS
SELECT o.opp_id, o.pair_id, o.cycle_id, o.direction, o.size,
       o.fill_yes, o.fill_no, o.fee_yes, o.fee_no,
       o.net_per_pair, o.roi_pct, o.detected_ts,
       km.title AS kalshi_title, pm.title AS poly_title,
       v.confidence, v.caveats
FROM opportunities o
JOIN pairs p    ON p.pair_id = o.pair_id
JOIN markets km ON km.entity_id = p.kalshi_entity_id
JOIN markets pm ON pm.entity_id = p.poly_entity_id
LEFT JOIN verdicts v ON v.pair_id = o.pair_id AND v.rules_hash = p.rules_hash
WHERE o.cycle_id = (SELECT MAX(cycle_id) FROM cycles);

-- The funnel for the most recent cycle (backs the status board).
CREATE VIEW IF NOT EXISTS v_funnel_latest AS
SELECT stage, n_in, n_out, n_in - n_out AS dropped, duration_ms
FROM stage_stats
WHERE cycle_id = (SELECT MAX(cycle_id) FROM stage_stats);

-- "Why so few opportunities?" — stage, reason, count over the last 24h.
-- SUM(count) not COUNT(*): drops rows may be per-item (count=1) or
-- aggregated (keep_dropped_ids=false), see STATE_SCHEMA.md.
CREATE VIEW IF NOT EXISTS v_drop_breakdown_24h AS
SELECT stage, reason, SUM(count) AS n
FROM drops
WHERE ts >= datetime('now', '-1 day')
GROUP BY stage, reason
ORDER BY n DESC;

-- Everything about one pair_id: verdicts, opportunities, drops — as typed
-- rows. Query: SELECT * FROM v_pair_trace WHERE pair_id=? ORDER BY ts.
CREATE VIEW IF NOT EXISTS v_pair_trace AS
SELECT v.pair_id, 'verdict' AS kind, v.verdict_ts AS ts,
       'same_event=' || v.is_same_event || ' conf=' || v.confidence ||
       ' model=' || v.model || ' ' || v.caveats AS detail
FROM verdicts v
UNION ALL
SELECT o.pair_id, 'opportunity', o.detected_ts,
       o.direction || ' net/pair=' || o.net_per_pair || ' size=' || o.size
FROM opportunities o
UNION ALL
SELECT d.entity_or_pair_id, 'drop', d.ts,
       d.stage || ': ' || d.reason || ' (cycle ' || d.cycle_id || ')'
FROM drops d
WHERE d.entity_or_pair_id IS NOT NULL;

-- Opportunities over time (spread distribution / §12 shadow validation).
CREATE VIEW IF NOT EXISTS v_opportunity_history AS
SELECT o.detected_ts, o.opp_id, o.pair_id, o.cycle_id, o.direction,
       o.net_per_pair, o.roi_pct, o.size, km.title AS kalshi_title
FROM opportunities o
LEFT JOIN pairs p    ON p.pair_id = o.pair_id
LEFT JOIN markets km ON km.entity_id = p.kalshi_entity_id
ORDER BY o.detected_ts;

-- Per-cycle durations + error counts (regression watch, §9.9).
CREATE VIEW IF NOT EXISTS v_cycle_health AS
SELECT c.cycle_id, c.started_ts, c.ended_ts, c.duration_ms, c.error_count,
       (SELECT COUNT(*) FROM opportunities o WHERE o.cycle_id = c.cycle_id)
           AS n_opportunities
FROM cycles c
ORDER BY c.cycle_id DESC;
