# BREAKING POINTS ANALYSIS — Sistema 6 Cloni
> Data: 1 Marzo 2026 | Autore: C0 (System Integrator)
> Ispirato da: Asimov's Laws — le regole perfette generano paradossi negli edge case

---

## Le 10 Falle Identificate

### FALLA 1 — Read su file in mutazione (Paradosso di Asimov)
**Severity**: MEDIUM
**Problema**: C3 legge app.py per audit security, ma C1 sta modificando app.py in parallelo.
C3 basa il suo report su codice che non esiste più.
Stessa falla: C6 vs C5 su backtest.py.
**Fix**: Fasi temporali — cloni READ-only (C3, C6-analisi) iniziano prima.
Oppure: `git stash` point di partenza per ogni clone.

### FALLA 2 — `--dangerously-skip-permissions` è un'arma nucleare
**Severity**: HIGH
**Problema**: Permette ai cloni di eseguire QUALSIASI bash senza approvazione.
Un clone con allucinazione potrebbe: rm -rf, eseguire DDL, pushare su git.
**Fix**: Usare `--allowedTools "Read,Edit,Write,Glob,Grep"` — tool specifici.
Se serve Bash: `--append-system-prompt "NEVER run: git push, rm -rf, psql, supabase"`

### FALLA 3 — Nessun health check inter-clone
**Severity**: MEDIUM-HIGH
**Problema**: Se C2 fallisce silenziosamente, C1 continua a scrivere codice dipendente dal DDL.
Al merge si scopre che il DDL non esiste.
**Fix**: Heartbeat check in orchestratore (no output > 5 min = alert).
C2 deve produrre DDL come PRIMO task.

### FALLA 4 — Budget explosion: 6 × Opus 4.6
**Severity**: MEDIUM
**Problema**: 6 cloni Opus con file grandi (app.py 220KB, index.html 405KB).
Stima: $30-72 per sessione, fino a $100+ con retry.
**Fix**: C3/C5 → Sonnet. C4 (HTML ripetitivo) → Haiku. `--max-budget-usd 8.00` per clone.

### FALLA 5 — memory/ è territorio conteso (backlog.md)
**Severity**: MEDIUM
**Problema**: C6 modifica backlog.md (Task 6.4), tutti gli altri lo leggono come contesto.
Git merge gestisce file diversi, ma la lettura concorrente crea ambiguità logica.
**Fix**: backlog.md READ-ONLY per tutti tranne C6. Nei prompt: "leggi all'inizio, non rileggere."

### FALLA 6 — Nessun test di integrazione post-merge
**Severity**: MEDIUM
**Problema**: Ogni clone funziona in isolamento. Ma l'interazione post-merge potrebbe
generare conflitti (import diversi, assunzioni su schema JSON, etc.)
**Fix**: Task T+4.5 — dopo merge, prima del deploy:
`python -m pytest tests/ -v && python scripts/security_audit.py`

### FALLA 7 — CWD sbagliato in orchestratore
**Severity**: LOW
**Problema**: `claude -p` carica CLAUDE.md dalla CWD. Se orchestratore non fa chdir, cloni senza contesto.
**Fix**: `os.chdir(~/btc_predictions)` in orchestratore. Ogni prompt inizia con "Leggi CLAUDE.md".

### FALLA 8 — Nessun rollback plan
**Severity**: HIGH
**Problema**: Deploy va male dopo merge 6 cloni → come si torna indietro?
**Fix**: Pre-lancio:
```bash
git tag pre-golive-v1
pg_dump --table=predictions > backup_predictions_pre_golive.sql
cp models/xgb_direction.pkl models/xgb_direction_pre_golive_backup.pkl
```
DDL rollback: `DROP TABLE cycle_lock; ALTER TABLE predictions DROP COLUMN onchain_timing_ok;`

### FALLA 9 — Riferimenti a righe specifiche nei prompt
**Severity**: LOW
**Problema**: "riga ~1151" diventa sbagliato se C1 aggiunge righe prima di quel punto.
**Fix**: Usare ancore semantiche: "dopo il blocco `# --- Pre-flight checks ---`"

### FALLA 10 — Nessun System Integrator (Legge Zero di Asimov)
**Severity**: HIGH
**Problema**: Chi verifica che il SISTEMA COMPLESSIVO funziona dopo le modifiche di 6 cloni?
Ogni clone ottimizza localmente. Nessuno verifica globalmente.
**Fix**: Clone 0 (System Integrator) — non modifica file, legge tutto post-merge,
genera integration report. Oppure: Mattia come review gate umano.

---

## Matrice Priorità Fix

| Priorità | Falla | Azione |
|----------|-------|--------|
| 🔴 Prima del lancio | #2 (permissions), #8 (rollback) | Configurare allowedTools, creare git tag + backup |
| 🟡 Nel prompt update | #1 (fasi), #3 (heartbeat), #5 (backlog r/o), #9 (ancore) | Aggiornare MASTER_ORCHESTRATION.md |
| 🟢 Nel design orchestratore | #4 (budget), #6 (integration test), #7 (cwd), #10 (C0) | Implementare in orchestrator.py |

---

## Le Tre Leggi + Legge Zero (adattate da Asimov)

1. **Legge Zero**: Un clone non può, con la sua azione o inazione, compromettere l'integrità del SISTEMA.
2. **Prima Legge**: Un clone non può modificare file fuori dal suo territorio.
3. **Seconda Legge**: Un clone deve eseguire i task assegnati, a meno che non violi la Prima o la Zero.
4. **Terza Legge**: Un clone deve proteggere la propria esecuzione (non crashare, gestire errori) a meno che non violi le leggi superiori.
