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

-- v8: bot_errors — Error Intelligence Hub (wf00)
-- Centralized error logging, classification, dedup, and recovery tracking
CREATE TABLE IF NOT EXISTS bot_errors (
    id                 BIGSERIAL PRIMARY KEY,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    workflow_id        TEXT NOT NULL,
    workflow_name      TEXT,
    node_name          TEXT,
    execution_id       TEXT,
    severity           TEXT NOT NULL CHECK (severity IN ('P0','P1','P2','P3')),
    error_type         TEXT NOT NULL,
    error_message      TEXT,
    error_fingerprint  TEXT NOT NULL,
    context            JSONB DEFAULT '{}',
    resolved           BOOLEAN DEFAULT FALSE,
    resolved_at        TIMESTAMPTZ,
    resolved_by        TEXT,
    recovery_attempted BOOLEAN DEFAULT FALSE,
    recovery_result    TEXT,
    notification_sent  BOOLEAN DEFAULT FALSE,
    duplicate_of       BIGINT REFERENCES bot_errors(id)
);

-- Dedup: lookup by fingerprint (recent errors with same fingerprint)
CREATE INDEX IF NOT EXISTS idx_bot_errors_fingerprint
    ON bot_errors (error_fingerprint, created_at DESC);

-- Dashboard: open errors by severity
CREATE INDEX IF NOT EXISTS idx_bot_errors_severity
    ON bot_errors (severity, created_at DESC)
    WHERE resolved = FALSE;

-- Timeline: newest first
CREATE INDEX IF NOT EXISTS idx_bot_errors_created
    ON bot_errors (created_at DESC);

-- Auto-cleanup: keep only last 30 days of resolved errors (run via pg_cron or manual)
-- DELETE FROM bot_errors WHERE resolved = TRUE AND created_at < now() - interval '30 days';

-- v9: Adaptive Calibration Engine (ACE) — bot_adaptive_state + bot_adaptive_log
-- bot_adaptive_state: single-row current state of the adaptive engine
CREATE TABLE IF NOT EXISTS bot_adaptive_state (
    id                      INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- single row
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    signals_since_last_calc INTEGER NOT NULL DEFAULT 0,
    total_signals_used      INTEGER NOT NULL DEFAULT 0,

    -- Rolling WR windows
    wr_50                   FLOAT,     -- WR last 50 signals
    wr_100                  FLOAT,     -- WR last 100 signals
    wr_200                  FLOAT,     -- WR last 200 signals
    wr_up                   FLOAT,     -- WR UP direction only
    wr_down                 FLOAT,     -- WR DOWN direction only

    -- Adaptive threshold
    optimal_threshold       FLOAT NOT NULL DEFAULT 0.56,
    best_band_label         TEXT,      -- e.g. "0.60-0.65"
    best_band_expectancy    FLOAT,     -- E = (WR×avg_win) - ((1-WR)×avg_loss)

    -- Direction bias
    direction_bias_adj      FLOAT NOT NULL DEFAULT 0.0,
    bias_direction          TEXT,      -- 'UP' or 'DOWN' if biased, NULL if balanced
    bias_pct                FLOAT,     -- % of dominant direction in last 30

    -- Market regime
    regime                  TEXT NOT NULL DEFAULT 'UNKNOWN',  -- RANGING, TRENDING, VOLATILE, UNKNOWN
    regime_adj              FLOAT NOT NULL DEFAULT 0.0,
    regime_size_factor      FLOAT NOT NULL DEFAULT 1.0,

    -- Momentum
    momentum_factor         FLOAT NOT NULL DEFAULT 1.0,
    wr_recent_10            FLOAT,     -- WR last 10
    wr_baseline_50          FLOAT,     -- WR last 50 (momentum baseline)

    -- Composite result
    effective_threshold     FLOAT NOT NULL DEFAULT 0.56,
    calibration_wr_factor   FLOAT NOT NULL DEFAULT 1.0
);

-- Seed the single row if empty
INSERT INTO bot_adaptive_state (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- bot_adaptive_log: history of parameter changes (audit trail)
CREATE TABLE IF NOT EXISTS bot_adaptive_log (
    id                  BIGSERIAL PRIMARY KEY,
    ts                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    trigger_reason      TEXT NOT NULL,         -- 'scheduled', 'retrain', 'ghost_batch', 'manual'
    signals_used        INTEGER NOT NULL,
    optimal_threshold   FLOAT NOT NULL,
    effective_threshold FLOAT NOT NULL,
    direction_bias_adj  FLOAT NOT NULL DEFAULT 0.0,
    regime              TEXT NOT NULL DEFAULT 'UNKNOWN',
    regime_adj          FLOAT NOT NULL DEFAULT 0.0,
    momentum_factor     FLOAT NOT NULL DEFAULT 1.0,
    calibration_wr_factor FLOAT NOT NULL DEFAULT 1.0,
    details             JSONB DEFAULT '{}'     -- full breakdown for debugging
);

-- Index: newest first for dashboard/monitoring queries
CREATE INDEX IF NOT EXISTS idx_adaptive_log_ts
    ON bot_adaptive_log (ts DESC);

-- Auto-cleanup: keep only last 90 days (run via pg_cron or manual)
-- DELETE FROM bot_adaptive_log WHERE ts < now() - interval '90 days';

-- v10: Portfolio Engine — decision logging
CREATE TABLE IF NOT EXISTS bot_portfolio_decisions (
    id                      BIGSERIAL PRIMARY KEY,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action                  TEXT NOT NULL,          -- OPEN, PYRAMID, REVERSE, PARTIAL_CLOSE_AND_OPEN, SKIP
    reason                  TEXT,
    confidence              FLOAT,
    risk_score              FLOAT,
    portfolio_exposure_btc  FLOAT,
    unrealized_pnl_pct      FLOAT,
    position_direction      TEXT,                   -- long, short, flat
    signal_direction        TEXT,                   -- UP, DOWN
    size_decided            FLOAT,
    is_fallback             BOOLEAN DEFAULT FALSE   -- true if PE was disabled/errored and legacy logic was used
);

-- Index: newest first for dashboard
CREATE INDEX IF NOT EXISTS idx_portfolio_decisions_ts
    ON bot_portfolio_decisions (created_at DESC);

-- Index: filter by action type
CREATE INDEX IF NOT EXISTS idx_portfolio_decisions_action
    ON bot_portfolio_decisions (action, created_at DESC);

-- Auto-cleanup: keep only last 30 days (run via pg_cron or manual)
-- DELETE FROM bot_portfolio_decisions WHERE created_at < now() - interval '30 days';
