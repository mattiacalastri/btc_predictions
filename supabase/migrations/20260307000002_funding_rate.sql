-- Add funding_rate column for ML signal quality
-- Run via: supabase db push --linked
-- Or manually in Supabase SQL editor
ALTER TABLE btc_predictions
  ADD COLUMN IF NOT EXISTS funding_rate numeric(14,8);

COMMENT ON COLUMN btc_predictions.funding_rate IS 'Binance perpetual funding rate at signal time (wf01A Format Derivatives → wf01B Open Position → app.py PATCH)';
