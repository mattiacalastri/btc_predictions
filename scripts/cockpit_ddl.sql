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
