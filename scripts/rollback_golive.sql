-- ROLLBACK go-live DDL — eseguire SOLO se serve tornare indietro
-- Data: 1 Marzo 2026
-- Contesto: annulla le modifiche DDL del go-live (cycle_lock + onchain_timing_ok)

DROP TABLE IF EXISTS cycle_lock;
ALTER TABLE predictions DROP COLUMN IF EXISTS onchain_timing_ok;

-- Nota: questo NON ripristina i dati persi. Solo lo schema.
-- Per rollback completo del codice: git checkout pre-golive-6clone-v1
