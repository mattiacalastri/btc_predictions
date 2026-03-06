-- council_votes table — AI Council deliberation log
-- Run this in Supabase Dashboard > SQL Editor

CREATE TABLE IF NOT EXISTS council_votes (
    id             BIGSERIAL PRIMARY KEY,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prediction_id  BIGINT REFERENCES btc_predictions(id) ON DELETE SET NULL,
    signal_hash    TEXT,
    round          SMALLINT DEFAULT 1,
    member         TEXT NOT NULL,         -- TECNICO | SENTIMENT | QUANT
    model_used     TEXT,
    direction      TEXT,                  -- UP | DOWN | ABSTAIN
    confidence     DOUBLE PRECISION,
    weight         DOUBLE PRECISION,
    reasoning      TEXT,
    raw_response   JSONB
);

CREATE INDEX IF NOT EXISTS idx_council_votes_prediction_id ON council_votes(prediction_id);
CREATE INDEX IF NOT EXISTS idx_council_votes_signal_hash   ON council_votes(signal_hash);
CREATE INDEX IF NOT EXISTS idx_council_votes_created_at    ON council_votes(created_at DESC);

-- RLS: only service_role can read/write
ALTER TABLE council_votes ENABLE ROW LEVEL SECURITY;
CREATE POLICY "service_role_only" ON council_votes
    USING (auth.role() = 'service_role')
    WITH CHECK (auth.role() = 'service_role');
