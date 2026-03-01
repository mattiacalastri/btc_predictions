-- COCKPIT DDL — Tabella per il Cockpit web dashboard
-- Data: 1 Marzo 2026
-- Eseguire su Supabase SQL Editor PRIMA di usare il cockpit

-- Tabella cockpit_events: stato real-time degli agent AI
-- L'orchestrator.py pusha aggiornamenti qui, cockpit.html li legge via app.py
CREATE TABLE IF NOT EXISTS cockpit_events (
    clone_id       TEXT PRIMARY KEY,           -- c1, c2, c3, c4, c5, c6
    name           TEXT NOT NULL DEFAULT '',    -- C1 Full Stack, C2 Blockchain, etc.
    role           TEXT NOT NULL DEFAULT '',    -- Full Stack Developer, etc.
    status         TEXT NOT NULL DEFAULT 'pending',  -- pending, running, done, error
    model          TEXT DEFAULT '',             -- claude-opus-4-6, claude-sonnet-4-6
    phase          TEXT DEFAULT '',             -- A or B
    current_task   TEXT DEFAULT '',             -- What the agent is currently doing
    last_message   TEXT DEFAULT '',             -- Last output message
    thought        TEXT DEFAULT '',             -- Agent's reasoning (if available)
    cost_usd       NUMERIC(10,4) DEFAULT 0,    -- Current cost in USD
    max_budget     NUMERIC(10,4) DEFAULT 0,    -- Budget cap
    elapsed_sec    NUMERIC(10,1) DEFAULT 0,    -- Seconds since start
    tasks_json     TEXT DEFAULT '[]',           -- JSON array of tasks with status
    next_action    TEXT DEFAULT '',             -- What the agent plans to do next
    next_action_time TEXT DEFAULT '',           -- When the next action is planned
    result_summary TEXT DEFAULT '',             -- Summary of results when done
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index per query dashboard (order by updated_at)
CREATE INDEX IF NOT EXISTS idx_cockpit_events_updated
    ON cockpit_events (updated_at DESC);

-- RLS: disabilitata per cockpit (accesso solo via service key da app.py)
-- Se si vuole abilitare RLS in futuro:
-- ALTER TABLE cockpit_events ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY cockpit_service ON cockpit_events FOR ALL USING (true);

-- v3: notes and priority columns for agent action buttons
ALTER TABLE cockpit_events ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT '';
ALTER TABLE cockpit_events ADD COLUMN IF NOT EXISTS priority BOOLEAN DEFAULT false;

-- Nota: il cockpit usa UPSERT (Prefer: resolution=merge-duplicates)
-- quindi clone_id come PK garantisce una sola riga per agent.

-- v4: cockpit_log — Centralized event log hub
-- Aggregates: Sentry errors, circuit breaker, trading errors, anomalies, n8n failures
CREATE TABLE IF NOT EXISTS cockpit_log (
    id             BIGSERIAL PRIMARY KEY,
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    source         TEXT NOT NULL DEFAULT 'system',     -- app, n8n, sentry, orchestrator, anomaly
    level          TEXT NOT NULL DEFAULT 'info',        -- info, success, warning, error, critical
    title          TEXT NOT NULL DEFAULT '',            -- Short summary (max 120 chars)
    message        TEXT NOT NULL DEFAULT '',            -- Full message body
    metadata       JSONB DEFAULT '{}'                   -- Extra data (bet_id, confidence, stack trace, etc.)
);

-- Index for dashboard queries (newest first, limited to ~500)
CREATE INDEX IF NOT EXISTS idx_cockpit_log_ts
    ON cockpit_log (ts DESC);

-- Index for filtering by level
CREATE INDEX IF NOT EXISTS idx_cockpit_log_level
    ON cockpit_log (level, ts DESC);

-- Auto-cleanup: keep only last 7 days of logs (run via pg_cron or manual)
-- DELETE FROM cockpit_log WHERE ts < now() - interval '7 days';

-- v5: slippage guard — track price drift between signal and execution
ALTER TABLE btc_predictions ADD COLUMN IF NOT EXISTS price_drift_pct FLOAT DEFAULT NULL;

-- v6: funding rate — track funding fee paid/received during position lifetime
ALTER TABLE btc_predictions ADD COLUMN IF NOT EXISTS funding_fee FLOAT DEFAULT NULL;

-- v7: n8n workflow traceability — track which workflow/node/execution created or modified each row
-- Populated by n8n nodes on INSERT (wf01A Save to Supabase) and UPDATE (wf02, wf08)
ALTER TABLE btc_predictions ADD COLUMN IF NOT EXISTS source_workflow TEXT DEFAULT NULL;
ALTER TABLE btc_predictions ADD COLUMN IF NOT EXISTS source_node TEXT DEFAULT NULL;
ALTER TABLE btc_predictions ADD COLUMN IF NOT EXISTS source_execution_id TEXT DEFAULT NULL;
ALTER TABLE btc_predictions ADD COLUMN IF NOT EXISTS source_updated_by TEXT DEFAULT NULL;

-- Index for filtering by workflow source (dashboard chart, debugging, audit)
CREATE INDEX IF NOT EXISTS idx_btc_predictions_source_wf
    ON btc_predictions (source_workflow)
    WHERE source_workflow IS NOT NULL;

COMMENT ON COLUMN btc_predictions.source_workflow IS 'n8n workflow that created this row (e.g. wf01A, wf01B, wf02, wf08)';
COMMENT ON COLUMN btc_predictions.source_node IS 'n8n node name that wrote this row (e.g. Save to Supabase, XGBoost Gate)';
COMMENT ON COLUMN btc_predictions.source_execution_id IS 'n8n execution ID that created this row (for audit trail)';
COMMENT ON COLUMN btc_predictions.source_updated_by IS 'Last n8n workflow that modified this row (e.g. wf02 on close, wf08 on ghost eval)';
