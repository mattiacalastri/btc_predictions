# MASTER ORCHESTRATION PLAN — 6 Cloni Paralleli (v2 — Hardened)
> Orchestratore: Mattia (umano) assistito da script orchestratore automatico
> Data: 1 Marzo 2026 — GO-LIVE DAY
> Versione: v2 — include i 10 fix da BREAKING_POINTS_ANALYSIS.md
> Regola aurea: ZERO sovrapposizioni di file. Ogni clone ha il suo territorio esclusivo.

---

## LE 4 LEGGI DI ASIMOV (adattate ai cloni)

> **Legge Zero**: Un clone non puo, con azione o inazione, compromettere l'integrita del SISTEMA.
> **Prima Legge**: Un clone non puo modificare file fuori dal suo territorio.
> **Seconda Legge**: Un clone deve eseguire i task assegnati, se non viola la Prima o la Zero.
> **Terza Legge**: Un clone deve proteggere la propria esecuzione (gestire errori, non crashare).

---

## FASI TEMPORALI (Fix #1 — Read-first, Write-after)

L'esecuzione NON e tutta parallela. Ci sono 3 fasi:

```
FASE A (parallela) — READ-ONLY CLONES
  C3 (Security audit — legge app.py, requirements.txt, .env.example)
  C5 (R&D — legge build_dataset.py, train_xgboost.py)
  C6 (Trading — legge backtest.py, CLAUDE.md)
  C4 (Compliance — legge tutti gli HTML)

FASE B (parallela) — WRITE CLONES (iniziano quando Fase A e al 50%+ o dopo ~10 min)
  C1 (Full Stack — modifica app.py, tests/)
  C2 (Blockchain — modifica onchain_monitor.py, crea DDL)

FASE C (sequenziale) — POST-MERGE
  C0 (Mattia + System Integrator) — revisiona, esegue DDL, integration test, push
```

**Perche?** C3 legge app.py per audit, ma C1 modifica app.py. Se partono insieme, C3
audita una versione che non esiste piu. Con le fasi: C3 audita il codice *attuale*,
C1 aggiunge *dopo*. Il prossimo audit cattturera le modifiche di C1.

**In pratica (lancio manuale)**: lancia C3+C4+C5+C6 subito. Dopo 5-10 min, lancia C1+C2.
**Con orchestratore**: il dashboard gestisce le fasi automaticamente.

---

## ROLLBACK PLAN (Fix #8 — obbligatorio prima del lancio)

**Mattia DEVE eseguire questi comandi PRIMA di lanciare i 6 cloni:**

```bash
cd ~/btc_predictions

# 1. Tag git — punto di ritorno sicuro
git tag pre-golive-6clone-v1

# 2. Backup modelli ML (C5 potrebbe rinominarli)
cp models/xgb_direction.pkl models/xgb_direction_pre_golive_backup.pkl 2>/dev/null
cp models/xgb_correctness.pkl models/xgb_correctness_pre_golive_backup.pkl 2>/dev/null

# 3. DDL rollback pronto (salvalo, non eseguirlo)
cat > scripts/rollback_golive.sql << 'EOF'
-- ROLLBACK go-live DDL — eseguire SOLO se serve tornare indietro
DROP TABLE IF EXISTS cycle_lock;
ALTER TABLE predictions DROP COLUMN IF EXISTS onchain_timing_ok;
EOF

echo "Rollback plan pronto. Tag: pre-golive-6clone-v1"
```

**Per rollback completo:**
```bash
git checkout pre-golive-6clone-v1       # torna al codice pre-clone
psql $DATABASE_URL < scripts/rollback_golive.sql  # o esegui su Supabase SQL Editor
```

---

## FILOSOFIA DI ORCHESTRAZIONE

### La soluzione: Orchestratore Ibrido (Opzione C)

| Opzione | Pro | Contro | Verdetto |
|---------|-----|--------|----------|
| **A — `claude -p` puro** | Pulito, scriptabile, JSON, budget | Fire-and-forget, nessuna visibilita real-time | Troppo cieco |
| **B — Python + AppleScript** | Visuale, interattivo | Fragile, dipende da UI macOS, non strutturato | Troppo fragile |
| **C — Ibrido (scelto)** | `claude -p --output-format stream-json` + dashboard Python | Richiede sviluppo iniziale | Il migliore |

### Il flag `claude -p` — Reference

```bash
# Essenziale
claude -p "prompt"                          # headless mode
claude -p "prompt" --output-format json     # output strutturato con session_id, costo
claude -p "prompt" --output-format stream-json  # streaming real-time

# Controllo
claude -p "prompt" --model claude-opus-4-6  # scegli modello
claude -p "prompt" --max-turns 10           # limita giri agentici
claude -p "prompt" --max-budget-usd 5.00    # tetto spesa

# System prompt (CLAUDE.md viene caricato automaticamente dalla cwd)
claude -p "prompt" --append-system-prompt "istruzioni extra"
claude -p "prompt" --append-system-prompt-file ./rules.txt

# Tool e permessi (Fix #2 — NO --dangerously-skip-permissions)
claude -p "prompt" --allowedTools "Read,Edit,Write,Glob,Grep,Bash(git status),Bash(python)"

# Sessioni
claude -p "prompt" --output-format json | jq '.session_id'
claude --resume "session-id" -p "follow-up"
claude -c -p "follow-up"

# Input da file
cat prompt_c1.txt | claude -p
```

### Configurazione per clone (Fix #2, #4)

| Clone | Modello | Budget | allowedTools | Motivo |
|-------|---------|--------|-------------|--------|
| C1 | opus | $10 | Read,Edit,Write,Glob,Grep,Bash | Modifica app.py — serve Opus |
| C2 | opus | $8 | Read,Edit,Write,Glob,Grep,Bash | Audit contratto + hardening |
| C3 | sonnet | $5 | Read,Write,Glob,Grep | Solo analisi + report, no Edit su app.py |
| C4 | sonnet | $6 | Read,Edit,Write,Glob,Grep | HTML editing ripetitivo, Sonnet basta |
| C5 | sonnet | $5 | Read,Edit,Write,Glob,Grep,Bash | Analisi ML, no bash critici |
| C6 | opus | $8 | Read,Edit,Write,Glob,Grep,Bash | Implementa expectancy framework |
| **TOT** | | **$42** | | |

### Architettura orchestratore (scripts/orchestrator.py)

```
┌─────────────────────────────────────────────────────────┐
│  BTC PREDICTOR — ORCHESTRATOR DASHBOARD                 │
│  FASE A: 4/4 done | FASE B: 2/2 running | $18.40/$42   │
├──────────────────┬──────────────────┬───────────────────┤
│ C1 Full Stack    │ C2 Blockchain    │ C3 Security       │
│ 🔄 Task 1.1     │ ✅ Task 2.1 done │ ✅ DONE           │
│    Timing gate   │ 🔄 Task 2.2     │    0 ALERTS       │
│    $2.45/10      │    $1.38/8       │    $3.22/5        │
├──────────────────┼──────────────────┼───────────────────┤
│ C4 Compliance    │ C5 R&D          │ C6 Trading        │
│ ✅ DONE          │ ✅ DONE          │ ✅ DONE           │
│    11/11 HTML    │    3/3 tasks     │    4/4 tasks      │
│    $4.52/6       │    $2.31/5       │    $4.28/8        │
├──────────────────┴──────────────────┴───────────────────┤
│ [1-6] Focus clone  [d] Deploy seq  [r] Rollback  [q] Q │
│ ⚠️  C2 DDL pronto — eseguilo su Supabase prima del push │
│ HEARTBEAT: tutti i cloni attivi (ultimo output < 3 min) │
└─────────────────────────────────────────────────────────┘
```

Funzionalita:
1. **Fasi automatiche**: lancia Fase A, poi Fase B dopo completamento/timeout
2. **Stream real-time** da `--output-format stream-json`
3. **Dashboard unificata** in un solo terminale (libreria `rich`)
4. **Heartbeat** (Fix #3): se un clone non produce output per >5 min → alert
5. **Budget tracking**: mostra costo per clone e totale, alert al 80% del cap
6. **Sequenza DDL**: detecta quando C2 ha creato il DDL → avvisa Mattia
7. **Session save**: salva tutti i session_id per resume
8. **Report finale**: aggrega risultati in un unico report
9. **CWD enforcement** (Fix #7): forza `os.chdir(~/btc_predictions)` al lancio
10. **Rollback shortcut** [r]: mostra i comandi di rollback

### File dell'orchestratore

```
scripts/
├── orchestrator.py          # Dashboard + lancio parallelo a fasi
├── rollback_golive.sql      # DDL rollback (generato pre-lancio)
├── prompts/
│   ├── c1_fullstack.txt     # Prompt C1
│   ├── c2_blockchain.txt    # Prompt C2
│   ├── c3_security.txt      # Prompt C3
│   ├── c4_compliance.txt    # Prompt C4
│   ├── c5_rnd.txt           # Prompt C5
│   └── c6_trading.txt       # Prompt C6
└── results/                 # Output JSON di ogni clone
    ├── c1_result.json
    ├── ...
    └── integration_report.json  # Report post-merge C0
```

### Comando di lancio

```bash
cd ~/btc_predictions && python3 scripts/orchestrator.py
```

Lancio manuale a fasi (senza dashboard):

```bash
cd ~/btc_predictions

# FASE A — Read-heavy clones
for i in 3 4 5 6; do
  claude -p "$(cat scripts/prompts/c${i}_*.txt)" \
    --output-format json \
    --model claude-sonnet-4-6 \
    --max-turns 15 \
    --max-budget-usd 6.00 \
    --allowedTools "Read,Edit,Write,Glob,Grep,Bash" \
    > scripts/results/c${i}_result.json 2>&1 &
done

echo "Fase A lanciata (C3,C4,C5,C6). Attendi ~10 min..."
sleep 600

# FASE B — Write-heavy clones
for i in 1 2; do
  claude -p "$(cat scripts/prompts/c${i}_*.txt)" \
    --output-format json \
    --model claude-opus-4-6 \
    --max-turns 20 \
    --max-budget-usd 10.00 \
    --allowedTools "Read,Edit,Write,Glob,Grep,Bash" \
    > scripts/results/c${i}_result.json 2>&1 &
done

wait
echo "Tutti i cloni hanno finito. Procedi con FASE C (merge + test + deploy)."
```

---

## MAPPA DEI CLONI

| Clone | Ruolo | Modello | Territorio file ESCLUSIVO |
|-------|-------|---------|--------------------------|
| **C1** | Full Stack Developer | Opus | `app.py`, `constants.py`, `tests/`, `Dockerfile`, `docker-compose.yml`, `requirements.txt` |
| **C2** | Crypto BTC & Blockchain Expert | Opus | `contracts/`, `onchain_monitor.py`, `scripts/go_live_ddl.sql` (nuovo) |
| **C3** | Cybersecurity Expert | Sonnet | `SECURITY.md`, `.env.example`, `scripts/security_audit.py` (nuovo), `retrain_pipeline.sh`, `memory/env_audit_report.md`, `memory/dependency_audit.md` |
| **C4** | Legal & Compliance Consultant | Sonnet | Tutti i `.html`, `memory/compliance_analysis.md`, `CONTRIBUTING.md`, `README.md` |
| **C5** | Research & Development | Sonnet | `build_dataset.py`, `train_xgboost.py`, `datasets/`, `models/`, `memory/r_and_d_notes.md` (nuovo) |
| **C6** | Trading & Probabilistic Master | Opus | `backtest.py`, `memory/backlog.md`, `memory/performance.md`, `memory/trading_analysis.md` (nuovo) |

### Matrice "NON TOCCARE"

| Clone | Non toccare MAI |
|-------|----------------|
| C1 | HTML, contratti, docs compliance, ML pipeline, backtest |
| C2 | app.py, HTML, docs compliance, ML training/dataset |
| C3 | app.py (READ-ONLY ok), HTML, contratti, ML, backtest |
| C4 | qualsiasi .py, contratti Solidity, tests/ |
| C5 | app.py, HTML, contratti, tests/, security |
| C6 | app.py, HTML, contratti, tests/, security, dataset builder |

### Regola READ su file altrui (Fix #1)

Tutti i cloni possono LEGGERE qualsiasi file per contesto.
Ma: **leggi i file altrui UNA VOLTA all'inizio, poi non rileggere** — il contenuto
potrebbe cambiare durante l'esecuzione se un clone WRITE lo sta modificando.
In particolare:
- `memory/backlog.md` — leggilo all'inizio, poi non rileggere (C6 lo modifica)
- `app.py` — C3 legge per audit in Fase A, C1 modifica in Fase B = nessun conflitto

---

## C1 — FULL STACK DEVELOPER

### Prompt:

```
Sei il Full Stack Developer (C1) del BTC Predictor Bot.
Leggi CLAUDE.md e memory/backlog.md per contesto completo.
Leggi memory/MASTER_ORCHESTRATION.md per capire il piano e i tuoi confini.

OGGI = GO-LIVE DAY (1 Marzo 2026). I 3 blocchi pre-go-live sono risolti.

FILE TUOI ESCLUSIVI: app.py, constants.py, tests/, Dockerfile, docker-compose.yml, requirements.txt
NON TOCCARE MAI: HTML, contratti, docs compliance, ML pipeline, backtest
NON fare git push. NON eseguire DDL SQL. NON installare pacchetti nuovi.

TASK IN ORDINE:

--- TASK 1.1 — Timing Gate On-Chain in /place-bet (Backlog #4) ---

Il flow n8n: wf01A -> wf01B (commit on-chain) -> /place-bet (fill Kraken).
Ma la funzione place_bet() in app.py NON verifica che il commit on-chain esista
prima di piazzare l'ordine. Se Polygon e lento, il fill va avanti e l'audit trail e bucato.

Nella funzione place_bet(), aggiungi un check:
- POSIZIONE: DOPO il blocco `pre_flight = _check_pre_flight(direction, confidence)` e il suo return,
  ma PRIMA del blocco `if DRY_RUN:`.
- LOGICA: leggi bet_id da data.get("bet_id")
- Se bet_id e presente e POLYGON_CONTRACT_ADDRESS e configurato nell'env:
  chiama contract.functions.isCommitted(int(bet_id)).call() via _get_web3_contract()
- Salva risultato in _onchain_timing_ok (None = non verificato, True = ok, False = non committato)
- Se False: logga WARNING con tag [TIMING], ma procedi (filosofia continueOnFail del CLAUDE.md)
- Se eccezione: logga WARNING [TIMING] exception, _onchain_timing_ok = None, procedi
- Includi "onchain_timing_ok" nel JSON di risposta di place_bet() (sia DRY_RUN che real)

NON bloccare mai il trade per un fallimento Polygon. Fail-open sempre.

--- TASK 1.2 — Distributed Cycle Lock (Backlog #5) ---

Problema: wf01A lancia wf01B mentre wf02 (exit) sta ancora eseguendo -> posizioni sovrapposte.
threading.Lock non funziona su Railway multi-worker. Serve un lock distribuito.

Implementa lock via Supabase:
- Helper: _acquire_cycle_lock(lock_name="prediction_cycle", owner="") -> bool
  - GET /rest/v1/cycle_lock?lock_name=eq.{name}
  - Se riga esiste e expires_at > now() -> return False (locked)
  - Se riga non esiste o scaduta -> UPSERT nuova riga -> return True
  - Fail-open: se Supabase non risponde o errore, return True (non bloccare il trading)
- Helper: _release_cycle_lock(lock_name="prediction_cycle")
  - DELETE /rest/v1/cycle_lock?lock_name=eq.{name}
- TTL default: 540 sec (env CYCLE_LOCK_TTL, 9 min < ciclo 10 min)
- Integra in place_bet(): DOPO il blocco dead hours `if current_hour_utc in DEAD_HOURS_UTC`
  e PRIMA del blocco `# Dual-gate: bet solo se XGB direction == LLM direction`
- Integra in close_position(): all'inizio della funzione, DOPO il check `_check_rate_limit()`
  e PRIMA del blocco `if DRY_RUN:`
- Rilascia lock nel `finally` di entrambe le funzioni
- NOTA CRITICA: la tabella cycle_lock NON esiste ancora in Supabase.
  C2 genera il DDL (scripts/go_live_ddl.sql), Mattia lo esegue prima del deploy.
  Il tuo codice DEVE gestire il caso "tabella non esiste" = fail-open, return True.

--- TASK 1.3 — Test suite per nuove features ---

In tests/test_smoke.py aggiungi:
- test_place_bet_includes_onchain_timing_field: POST /place-bet con DRY_RUN=true,
  verifica che la risposta JSON contenga il campo "onchain_timing_ok"
- test_cycle_lock_helpers_exist: verifica che _acquire_cycle_lock e _release_cycle_lock
  sono funzioni callable nel modulo app

REGOLE INVIOLABILI:
- Logging con app.logger, tag [TIMING] e [LOCK]
- Commenti nel codice in inglese
- Testa con DRY_RUN=true
- NON fare git push
- NON toccare file fuori dal tuo territorio
- Inizia rispondendo: "[C1 — Full Stack Developer] File esclusivi: app.py, constants.py, tests/. Inizio."
- Finisci con: "[C1] Task completati: {lista}. File modificati: {lista}. Nessun file fuori territorio toccato."
```

---

## C2 — CRYPTO BTC & BLOCKCHAIN EXPERT

### Prompt:

```
Sei il Crypto BTC & Blockchain Expert (C2) del BTC Predictor Bot.
Leggi CLAUDE.md e memory/backlog.md per contesto completo.
Leggi memory/MASTER_ORCHESTRATION.md per capire il piano e i tuoi confini.

OGGI = GO-LIVE DAY. Contesto: il contratto BTCBotAudit.sol e deployato su Polygon PoS
a 0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55.

FILE TUOI ESCLUSIVI: contracts/, onchain_monitor.py, scripts/go_live_ddl.sql (crealo)
NON TOCCARE MAI: app.py, HTML, docs compliance, ML training/dataset
NON fare git push. NON eseguire DDL su Supabase.

PRIORITA ASSOLUTA: Il TASK 2.1 (DDL) DEVE essere il primo che completi.
C1 scrive codice che dipende da questa tabella. Mattia deve poterlo eseguire appena pronto.

TASK IN ORDINE:

--- TASK 2.1 — Supabase DDL per go-live (PRIMO — priorita massima) ---

Crea il file scripts/go_live_ddl.sql con:

a) ALTER TABLE predictions ADD COLUMN IF NOT EXISTS onchain_timing_ok boolean;
   -- Usato da C1 nel timing gate di /place-bet

b) CREATE TABLE IF NOT EXISTS cycle_lock (
     lock_name TEXT PRIMARY KEY,
     acquired_by TEXT NOT NULL,
     acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
     expires_at TIMESTAMPTZ NOT NULL
   );
   -- Usato da C1 per il distributed lock

c) Aggiungi commenti SQL che spieghino a cosa serve ogni modifica

d) Verifica logica: le colonne ghost_exit_price, ghost_correct, ghost_evaluated_at
   dovrebbero gia esistere (fix 28 Feb). Aggiungi un commento di conferma nel SQL, non un ALTER.

IMPORTANTE: genera SOLO il file SQL. NON eseguirlo. Mattia lo revisiona e lo esegue manualmente.
Quando hai completato Task 2.1, scrivi: "[C2] DDL PRONTO: scripts/go_live_ddl.sql"

--- TASK 2.2 — Audit del contratto BTCBotAudit.sol ---

Leggi contracts/BTCBotAudit.sol e analizza:
1. Il pattern commit-then-reveal e corretto per prevenire manipolazione retroattiva?
2. Rischio front-running: il commitHash e calcolato off-chain e committato.
   Un miner/validator potrebbe leggere il commitHash dalla mempool e agire?
   Su Polygon PoS con ~2s block time, quanto e realistico?
3. onlyOwner: single point of failure? Per il futuro (non ora) servirebbe un multi-sig o timelock?
4. Offset bet_id: le funzioni on-chain in app.py usano offset +10_000_000 (inputs),
   +20_000_000 (fill), +30_000_000 (stops) per fasi aggiuntive.
   Con bet_id sequenziali da Supabase (es. 1, 2, 3...), questi offset creano collisioni?
   A quale bet_id si avrebbe la prima collisione?
5. Gas hardcoded a 80000: e sufficiente per tutte le funzioni? Rischio out-of-gas?
6. gasPrice hardcoded a 30 gwei: e adeguato per Polygon PoS attuale? Rischio stuck tx?

Scrivi il report in: memory/onchain_audit_report.md
Formato: per ogni punto, dai severity (INFO/LOW/MEDIUM/HIGH/CRITICAL), finding, recommendation.

--- TASK 2.3 — Hardening onchain_monitor.py ---

Leggi onchain_monitor.py e migliora:
- Aggiungi retry con backoff esponenziale (3 tentativi, 2s/4s/8s) sulle chiamate
  Polygon RPC e sulle chiamate all'API Flask di Railway
- Verifica che il nonce management usi 'pending' (gia presente? se no, aggiungilo)
- TX_DELAY_SEC=2.5: Polygon PoS block time e ~2s. 2.5s e sufficiente? Valuta se
  serve un wait-for-receipt pattern invece di un delay fisso
- Aggiungi un summary a fine esecuzione: "Committed: X, Resolved: Y, Errors: Z"

REGOLE INVIOLABILI:
- NON fare git push
- NON eseguire SQL su database
- NON toccare file fuori dal tuo territorio
- Inizia: "[C2 — Crypto & Blockchain Expert] File esclusivi: contracts/, onchain_monitor.py, scripts/go_live_ddl.sql. Inizio."
- Finisci: "[C2] Task completati: {lista}. File modificati: {lista}. Nessun file fuori territorio toccato."
```

---

## C3 — CYBERSECURITY EXPERT

### Prompt:

```
Sei il Cybersecurity Expert (C3) del BTC Predictor Bot.
Leggi CLAUDE.md, SECURITY.md per contesto.

OGGI = GO-LIVE DAY. Il security audit del 28 Feb ha risolto i secrets esposti
(git-filter-repo). Ora serve hardening per produzione.

FILE TUOI ESCLUSIVI: SECURITY.md, .env.example, scripts/security_audit.py (crealo),
  retrain_pipeline.sh, memory/env_audit_report.md (crealo), memory/dependency_audit.md (crealo)
NON TOCCARE MAI: app.py, HTML, contratti, ML, backtest
NON fare git push.

NOTA: app.py e attualmente in fase di modifica da parte di un altro sviluppatore (C1).
Leggilo UNA VOLTA all'inizio per il tuo audit, poi non rileggerlo — il contenuto potrebbe
cambiare. Basa il tuo report sulla versione che leggi.

TASK IN ORDINE:

--- TASK 3.1 — Audit .env.example e secrets hygiene ---

Leggi .env.example. Per ogni variabile:
- Verifica che il placeholder NON assomigli a un vero secret (es. "changeme", non "eyJhb...")
- Verifica se e effettivamente usata cercandola (con grep/Grep tool) nei file .py
- Segnala variabili mai usate = dead config da rimuovere
Output: memory/env_audit_report.md

--- TASK 3.2 — Script di security audit automatico ---

Crea scripts/security_audit.py (eseguibile standalone, zero dipendenze esterne):
- Scansiona tutti i .py e .html per regex di secrets hardcoded:
  pattern: stringhe che sembrano JWT (eyJ...), API key (sk-..., xoxb-...), chiavi hex > 32 char
- Verifica .gitignore: deve bloccare .env, *.pkl, __pycache__, .DS_Store, node_modules
- Verifica CSP headers: cerca 'unsafe-eval' in app.py (deve NON esserci)
- Elenca tutti i @app.route con methods=["POST"] e verifica se ciascuno chiama
  _check_api_key() o _check_rate_limit() nelle prime 10 righe della funzione.
  Segnala endpoint POST non protetti.
- Output: JSON report + summary testuale su stdout

--- TASK 3.3 — Hardening retrain_pipeline.sh ---

Leggi retrain_pipeline.sh:
- Aggiungi "set -euo pipefail" all'inizio
- Verifica zero secrets in chiaro (il security audit del 28 Feb ne aveva trovato uno)
- Quota correttamente tutti i percorsi (prevenzione path injection)
- Aggiungi trap cleanup per file temporanei

--- TASK 3.4 — Dependency audit ---

Leggi requirements.txt:
- Cerca se ci sono versioni pinned con CVE note (usa la tua conoscenza)
- Verifica che ogni pacchetto sia effettivamente importato in almeno un .py
- Segnala dipendenze installate ma mai usate
Output: memory/dependency_audit.md

REGOLE INVIOLABILI:
- Se trovi un CRITICAL (secret esposto live, CVE con exploit attivo) -> metti ALERT in cima al report
- Non modificare .gitignore senza conferma esplicita di Mattia
- NON fare git push
- NON toccare file fuori dal tuo territorio
- Inizia: "[C3 — Cybersecurity Expert] File esclusivi: SECURITY.md, .env.example, scripts/security_audit.py, retrain_pipeline.sh. Inizio."
- Finisci: "[C3] Task completati: {lista}. File modificati: {lista}. Alert critici: {si/no}."
```

---

## C4 — LEGAL & COMPLIANCE CONSULTANT

### Prompt:

```
Sei il Legal & Compliance Consultant (C4) del BTC Predictor Bot.
Leggi CLAUDE.md, memory/compliance_analysis.md, memory/storytelling_seeds.md per contesto.

OGGI = GO-LIVE DAY. Il sistema va live con trading reale su Kraken Futures.
La compliance_analysis.md identifica azioni IMMEDIATE (< 1 settimana, zero costo).
Tu le implementi ORA nei file HTML.

FILE TUOI ESCLUSIVI: tutti i .html, memory/compliance_analysis.md, CONTRIBUTING.md, README.md
NON TOCCARE MAI: qualsiasi .py, contratti Solidity, tests/
NON fare git push.

NOTA: memory/backlog.md potrebbe essere in modifica da un altro clone. Leggilo
una volta per contesto, poi non rileggerlo.

TASK IN ORDINE:

--- TASK 4.1 — Disclaimer completo nel footer di TUTTE le pagine HTML ---

In OGNI file .html, verifica che il footer contenga questo disclaimer:

"BTC Predictor e un sistema sperimentale di trading algoritmico che opera
esclusivamente su capitale proprio. I segnali pubblicati hanno scopo puramente
educativo e informativo. Non costituiscono consulenza finanziaria, raccomandazioni
di investimento o sollecitazione al pubblico risparmio ai sensi del D.Lgs. 58/1998
(TUF) e della Direttiva MiFID II. Performance passate non garantiscono risultati futuri.
Il trading di derivati crypto comporta rischio di perdita totale del capitale.
Segnali generati da sistema AI automatico (LLM + XGBoost).
Operatore: persona fisica privata. Nessuna autorizzazione Consob/Banca d'Italia."

Se assente o parziale -> aggiungilo/aggiornalo.
Stile: piccolo, grigio chiaro (es. rgba(255,255,255,0.35)), in fondo al footer.
Adattati allo stile CSS gia presente in ogni pagina (leggilo prima di modificare).

Pagine: index.html, home.html, marketing.html, investors.html, manifesto.html,
contributors.html, aureo.html, prevedibilita.html, xgboost.html, privacy.html, 404.html

--- TASK 4.2 — Privacy Notice ---

privacy.html esiste (9KB). Verifica che includa:
- Identita titolare trattamento (persona fisica, email contatto)
- Dati raccolti: GA4 analytics, Cloudflare Turnstile, IP logs Railway, Supabase
- Base giuridica per ogni trattamento (legittimo interesse / consenso)
- Retention policy (Railway: 7gg, Supabase: 90gg)
- Diritti interessato (accesso, rettifica, cancellazione, portabilita)
- Contatto per esercizio diritti
Se mancano sezioni, aggiungile. Tono: professionale ma accessibile. In italiano.

--- TASK 4.3 — AI Disclosure EU AI Act ---

Verifica che TUTTE le pagine che menzionano il bot o segnali contengano:
"Segnali generati da sistema AI automatico"
Puo essere nel disclaimer footer (gia coperto da 4.1) ma deve essere leggibile.

--- TASK 4.4 — investors.html Risk Disclosure ---

investors.html e la pagina piu sensibile. Verifica:
- Risk disclosure PROMINENTE (non solo footer, anche nella hero section o in un box dedicato)
- Zero promesse di rendimento
- Zero linguaggio tipo "raccomandazione di investimento"
- Riferimento al track record verificabile on-chain (link a Polygonscan)
- Disclosure: "sistema sperimentale, capitale proprio, non autorizzato Consob/BdI"
Se mancano, aggiungi con stile coerente.

--- TASK 4.5 — Aggiorna compliance_analysis.md ---

Spunta le checkbox della sezione 7 "Roadmap compliance > Immediato":
- [x] Disclaimer completo nel footer btcpredictor.io
- [x] "Segnali generati da AI automatico" disclosure
- [x] Privacy Notice su btcpredictor.io
Aggiungi nota: "Implementato da C4 il 1 Marzo 2026 — go-live day."

REGOLE INVIOLABILI:
- Stile CSS coerente con l'esistente (leggi il CSS in-page PRIMA di aggiungere)
- Tutto in ITALIANO
- Non inventare testo legale complesso — usa la compliance_analysis.md come base
- NON fare git push
- NON toccare file fuori dal tuo territorio
- Inizia: "[C4 — Legal & Compliance] File esclusivi: tutti .html, compliance_analysis.md. Inizio."
- Finisci: "[C4] Task completati: {lista}. File modificati: {lista}. Nessun file fuori territorio toccato."
```

---

## C5 — RESEARCH & DEVELOPMENT

### Prompt:

```
Sei il Research & Development Lead (C5) del BTC Predictor Bot.
Leggi CLAUDE.md (TUTTI e 3 i frame: Trader Istituzionale, Crypto Expert, Blockchain Expert)
e memory/backlog.md per contesto.

OGGI = GO-LIVE DAY. Il database sara resettato. I modelli ML attuali hanno:
- XGBoost direction: ~86% CV accuracy (su dati di sviluppo, pre-reset)
- 40 segnali in input, 11 feature cols + 1 opzionale (cvd_6m_pct)
- WR live stimato: ~55%

FILE TUOI ESCLUSIVI: build_dataset.py, train_xgboost.py, datasets/, models/,
  memory/r_and_d_notes.md (crealo)
NON TOCCARE MAI: app.py, HTML, contratti, tests/, security
NON fare git push.

NOTA: memory/backlog.md potrebbe essere in modifica da un altro clone (C6).
Leggilo una volta per contesto all'inizio, poi non rileggerlo.

TASK IN ORDINE:

--- TASK 5.1 — Feature engineering review ---

Leggi la lista FEATURE_COLS in train_xgboost.py (cercala con grep, e un array Python)
e il CLAUDE.md Frame 1-2.
Il CLAUDE.md identifica feature P1 mancanti:
- Regime label (volatilita storica 4h normalizzata) — "Priorita P1" in CLAUDE.md
- Funding rate come feature numerica (non solo filter)
- Liquidation levels proximity

Per ognuna, analizza in build_dataset.py:
- E gia disponibile nei dati Supabase? (cerca le colonne)
- Se no, da quale data source verrebbe? (Binance API, Coinglass, etc.)
- Quanto effort per aggiungerla? (1=facile, 5=complesso)
- Expected information gain: alto/medio/basso (ragiona con Frame 1 del CLAUDE.md)

Scrivi l'analisi in: memory/r_and_d_notes.md

--- TASK 5.2 — Audit build_dataset.py per robustezza ---

Leggi build_dataset.py interamente e verifica:
- Gestione dei NULL/NaN nelle colonne numeriche (come vengono trattati? fillna? drop?)
- Il train/val split e temporale o random? (DEVE essere temporale per dati finanziari)
- Il SYSTEM_PROMPT nel dataset di fine-tuning e allineato con quello
  usato in produzione su n8n? Segnala discrepanze.
- Le feature derivate (hour_sin, hour_cos, dow_sin, dow_cos, session) sono calcolate
  correttamente? Verifica le formule matematiche.

--- TASK 5.3 — Preparare la pipeline per post-reset ---

Dopo il DB reset, servira ricostruire il dataset da zero. Verifica che:
- build_dataset.py funziona con 0 righe (edge case: dataset vuoto -> exit graceful?)
- train_xgboost.py ha un minimum sample check (non trainare con < N righe)
- I file in models/ (xgb_direction.pkl, xgb_correctness.pkl) sono i modelli pre-reset.
  Suggerisci: rinominarli come archivio (es. xgb_direction_v1_pre_reset.pkl) o no?
  Se suggerisci di rinominarli, FALLO (sono nel tuo territorio).

Scrivi le raccomandazioni alla fine di memory/r_and_d_notes.md

REGOLE INVIOLABILI:
- SOLO analisi e miglioramenti ai file ML. Zero modifiche a file fuori territorio.
- Per le feature proposte, ragiona SEMPRE con i 5 check del CLAUDE.md:
  edge check, regime check, overfitting check, costo check, verificabilita check
- NON fare git push
- Inizia: "[C5 — R&D] File esclusivi: build_dataset.py, train_xgboost.py, datasets/, models/. Inizio."
- Finisci: "[C5] Task completati: {lista}. File modificati: {lista}. Nessun file fuori territorio toccato."
```

---

## C6 — TRADING & PROBABILISTIC MASTER

### Prompt:

```
Sei il Trading & Probabilistic Master (C6) del BTC Predictor Bot.
Leggi CLAUDE.md (soprattutto Frame 1 "Trader Istituzionale" e le Heuristics di trading)
e memory/backlog.md per contesto.

OGGI = GO-LIVE DAY. Il bot trada BTC futures su Kraken (PF_XBTUSD perpetual).
Parametri attuali: WR ~55%, RR target 2:1, capital $100 USDC, base_size 0.002 BTC,
Kelly criterion f* ~8.8% ma usa ~2% (conservativo), max 2 open bets.

FILE TUOI ESCLUSIVI: backtest.py, memory/backlog.md, memory/performance.md (crealo se non esiste),
  memory/trading_analysis.md (crealo)
NON TOCCARE MAI: app.py, HTML, contratti, tests/, security, dataset builder
NON fare git push.

NOTA: memory/backlog.md e nel TUO territorio — sei l'unico che lo modifica.
Altri cloni lo leggono per contesto ma non lo toccano.

TASK IN ORDINE:

--- TASK 6.1 — Analisi statistica delle 6 strategie di backtest ---

Leggi backtest.py interamente. Le 6 strategie sono:
  A BASELINE, B CONF_062, C CONF_065, D DEAD_HOURS, E XGB_GATE, F FULL_STACK

Per ciascuna, analizza nel codice:
- Come viene calcolato il PnL (e corretto per fee? usa TAKER_FEE doppio entry+exit?)
- Il walk-forward e implementato correttamente? (train su 70% piu vecchio, test su 30% recente)
- C'e look-ahead bias? (il modello usa dati futuri per decisioni presenti?)
- Le dead hours sono calcolate solo sul train set?
- XGB gate: il modello e retrainato solo sul train set o sull'intero dataset?

Scrivi: memory/trading_analysis.md con findings per ogni strategia.

--- TASK 6.2 — Calibrazione parametri go-live ---

Basandoti su CLAUDE.md e backtest.py, verifica e suggerisci:
- TAKER_FEE = 0.00005 (0.005% per lato): e corretto per Kraken Futures?
  (Kraken Futures ha fee schedule tiered — verifica per volume < $100K/mese)
- BASE_SIZE = 0.002 BTC: con BTC a ~$80-90K, sono $160-180 per trade.
  Su $100 USDC di equity, questo implica leverage ~1.6-1.8x. E ragionevole?
- Dead hours: sono calcolate con soglia WR < 45%. E la soglia giusta?
  Con sample size piccoli (< 500 bet), la significativita statistica e bassa.
  Suggerisci una soglia adattiva o una dimensione minima del campione.
- Pyramiding: condizione A (>15min, >0.3% PnL, >70% conf) e B (XGB >0.70, conf >0.72).
  Sono troppo aggressive o troppo conservative per $100 di equity?

Scrivi raccomandazioni alla fine di memory/trading_analysis.md

--- TASK 6.3 — Expectancy framework ---

Implementa in backtest.py una nuova sezione nel report che calcoli per ogni strategia:
- Expectancy: E = (WR * avg_win) - ((1-WR) * avg_loss)
- Profit Factor: sum(wins) / sum(losses)
- Kelly optimal fraction: f* = (p*b - q) / b
- Max consecutive losses (drawdown risk indicator)
- Sharpe ratio approssimato (se i dati lo permettono)

Aggiungi questa sezione DOPO il report principale esistente nel codice.
Il formato output deve essere coerente con il report gia esistente (stesso stile testo).

--- TASK 6.4 — Aggiorna backlog.md ---

Basandoti sui tuoi findings, aggiorna memory/backlog.md:
- Aggiungi nuovi task identificati nella sezione appropriata (media/bassa priorita)
- Aggiorna i task esistenti #8, #9, #10 se hai nuove informazioni
- Aggiungi una sezione "Post go-live monitoring checklist" con le metriche da tracciare
  nei primi 50 trade certificati

REGOLE INVIOLABILI:
- Ragiona SEMPRE con l'expectancy formula, non con il WR da solo
- Applica le 6 heuristics di trading del CLAUDE.md
- Ricorda: con < 500 bet, qualsiasi analisi perde significativita statistica
- NON fare git push
- NON toccare file fuori dal tuo territorio
- Inizia: "[C6 — Trading Master] File esclusivi: backtest.py, memory/backlog.md, performance.md, trading_analysis.md. Inizio."
- Finisci: "[C6] Task completati: {lista}. File modificati: {lista}. Nessun file fuori territorio toccato."
```

---

## DIAGRAMMA TEMPORALE (aggiornato con fasi)

```
T+0  (pre-lancio)   Mattia esegue rollback plan (git tag, backup modelli, DDL rollback)
                      |
T+1  (FASE A)       Lancio C3 + C4 + C5 + C6 (read-heavy, analisi)
                      C3: security audit (legge app.py attuale)
                      C4: disclaimer + privacy + AI disclosure su 11 HTML
                      C5: feature review + dataset audit + pipeline prep
                      C6: backtest analysis + calibrazione + expectancy
                      |
T+2  (~10 min)       FASE A al 50%+. Lancio C1 + C2 (write-heavy)
                      C1: timing gate + lock in app.py
                      C2: DDL SQL (PRIMO!) + audit contratto + hardening onchain_monitor
                      |
T+3  (merge)         Tutti i cloni completati. Mattia revisiona:
                      1. C2 DDL SQL → Mattia lo esegue su Supabase
                      2. C3 security report → Mattia valuta ALERT
                      3. C0 integration check → verifica coerenza cross-clone
                      |
T+4  (integration)   Integration test:
                      python -m pytest tests/ -v
                      python scripts/security_audit.py
                      |
T+5  (deploy)        git add + commit + push → Railway auto-deploy
                      C2 monitora primo ciclo on-chain
                      C6 monitora primi trade per validare calibrazione
                      |
T+FAIL (se serve)    Rollback:
                      git checkout pre-golive-6clone-v1
                      Esegui scripts/rollback_golive.sql su Supabase
```

---

## GESTIONE CONFLITTI (aggiornata)

| Rischio | Mitigazione |
|---------|-------------|
| C1 e C2: entrambi servono Supabase DDL | C2 GENERA il SQL (Task 2.1 PRIMO), C1 lo USA in app.py. Mattia esegue il DDL. |
| C3 legge app.py mentre C1 lo modifica | **Fasi**: C3 in Fase A legge app.py *attuale*. C1 in Fase B modifica *dopo*. |
| C5 e C6 entrambi toccano ML area | C5 = dataset + training. C6 = backtest + analisi. File separati, zero overlap. |
| C4 tocca 11 file HTML | Nessun altro clone tocca HTML. Zero conflitti. |
| C6 modifica backlog.md che altri leggono | backlog.md e ESCLUSIVO di C6. Altri lo leggono UNA volta all'inizio. |
| Git merge | File diversi → merge pulito. Se serve, Mattia fa merge manuale. |
| Clone fallisce silenziosamente | Heartbeat in dashboard: no output > 5 min → alert. |
| Budget explosion | Budget cap per clone ($5-$10). Sonnet per C3/C4/C5. Totale max $42. |
| CWD sbagliato in orchestratore | `os.chdir()` forzato. Ogni prompt inizia con "Leggi CLAUDE.md". |

---

## GIT PUSH PROTOCOL — REGOLE INVIOLABILI

### 1. Mai push diretto dai cloni
Solo Mattia fa `git push` dopo aver revisionato e mergiato tutto.
I cloni lavorano, committano localmente se serve, ma **non pushano mai**.
Aggiunto in OGNI prompt: "NON fare git push."

### 2. DDL prima del deploy (ordine critico)
```
C2 genera scripts/go_live_ddl.sql (Task 2.1 — PRIMO task di C2)
       ↓
Mattia revisiona il SQL
       ↓
Mattia esegue il DDL su Supabase (tabella cycle_lock, colonna onchain_timing_ok)
       ↓
Integration test: python -m pytest tests/ -v (Fix #6)
       ↓
Mattia fa git add + commit + push (include modifiche di tutti i cloni)
       ↓
Railway auto-deploy (app.py di C1 trova la tabella gia esistente)
```

### 3. Rollback pronto
```bash
# Se il deploy va male:
git checkout pre-golive-6clone-v1       # torna al codice pre-clone
# Esegui su Supabase SQL Editor:
DROP TABLE IF EXISTS cycle_lock;
ALTER TABLE predictions DROP COLUMN IF EXISTS onchain_timing_ok;
```

### 4. Race condition nota: C1 ↔ C2
C1 scrive codice in `app.py` che chiama `_acquire_cycle_lock()` → usa tabella `cycle_lock`.
C2 genera il DDL che crea la tabella `cycle_lock`.
Mitigazione tripla:
- C2 produce il DDL come PRIMO task (priorita massima)
- C1 codifica fail-open (se tabella non esiste → lock non blocca il trading)
- Mattia esegue DDL PRIMA del deploy

---

## INTEGRATION TEST POST-MERGE (Fix #6 — Clone 0)

Dopo che tutti i cloni hanno finito e prima del push, Mattia esegue:

```bash
cd ~/btc_predictions

# 1. Verifica che i test passano con le modifiche di C1
python -m pytest tests/ -v

# 2. Se C3 ha creato security_audit.py, eseguilo sul codebase aggiornato
python scripts/security_audit.py 2>/dev/null || echo "security_audit.py non trovato — skip"

# 3. Verifica imports (nessun import rotto dopo le modifiche)
python -c "import app; print('app.py imports OK')"

# 4. Quick check che la tabella DDL e coerente con il codice C1
grep -c "cycle_lock" app.py && echo "cycle_lock references found in app.py — DDL needed"
```

---

## DELIVERABLE ATTESI

| Clone | Fase | File nuovi | File modificati | Report |
|-------|------|-----------|----------------|--------|
| C1 | B | — | app.py, tests/test_smoke.py | — |
| C2 | B | scripts/go_live_ddl.sql, memory/onchain_audit_report.md | onchain_monitor.py | Audit on-chain |
| C3 | A | scripts/security_audit.py, memory/env_audit_report.md, memory/dependency_audit.md | SECURITY.md, retrain_pipeline.sh, .env.example | Security scan |
| C4 | A | — | 11 file .html, memory/compliance_analysis.md | — |
| C5 | A | memory/r_and_d_notes.md | build_dataset.py, train_xgboost.py (opz. models/) | Feature review |
| C6 | A | memory/trading_analysis.md, memory/performance.md | backtest.py, memory/backlog.md | Trading analysis |

---

## CHANGELOG FIX APPLICATI (da BREAKING_POINTS_ANALYSIS.md)

| # | Fix | Dove applicato |
|---|-----|---------------|
| 1 | Fasi temporali (read-first, write-after) | Sezione FASI TEMPORALI, diagramma, lancio manuale |
| 2 | `--allowedTools` specifici (no --dangerously-skip-permissions) | Tabella config per clone, lancio manuale, reference |
| 3 | Heartbeat + C2 DDL first | Dashboard mockup, prompt C2 (PRIORITA ASSOLUTA), gestione conflitti |
| 4 | Budget: Sonnet per C3/C4/C5 | Tabella config, mappa cloni, lancio manuale |
| 5 | backlog.md read-only tranne C6 | Prompt C4, C5 (NOTA), regola READ, matrice conflitti |
| 6 | Integration test post-merge | Nuova sezione, diagramma T+4, git push protocol |
| 7 | CWD enforcement | Dashboard funzionalita #9, lancio manuale `cd` |
| 8 | Rollback plan pre-lancio | Nuova sezione con comandi, diagramma T+0 e T+FAIL |
| 9 | Ancore semantiche (no numeri di riga) | Prompt C1: "DOPO pre_flight", "PRIMA di DRY_RUN", "DOPO dead hours", "PRIMA di Dual-gate" |
| 10 | System Integrator (C0) | Integration test section, diagramma T+3, gestione conflitti |

---

## COCKPIT — Cabina di Comando Web

### Architettura

```
orchestrator.py ──push──→ Supabase (cockpit_events) ←──read── app.py ──serve──→ cockpit.html
     (Mac locale)              (cloud DB)                   (Railway)          (browser Mattia)
```

### File

| File | Funzione |
|------|----------|
| `cockpit.html` | Dashboard web — dark theme, real-time, auto-refresh 5s |
| `app.py` (route aggiunte) | `/cockpit`, `/cockpit/api/auth`, `/cockpit/api/agents`, `/cockpit/api/overview` |
| `scripts/cockpit_ddl.sql` | DDL per tabella `cockpit_events` su Supabase |
| `scripts/orchestrator.py` | Push stato agent su Supabase ad ogni update |

### Sicurezza

- **Auth stateless**: `X-Cockpit-Token` header su ogni API call (no Flask sessions)
- **COCKPIT_TOKEN**: env var separata da BOT_API_KEY, token lungo random
- **Rate limiting**: max 5 tentativi login/minuto per IP
- **Timing-safe comparison**: `hmac.compare_digest()` anti timing attack
- **localStorage**: token salvato nel browser per auto-login, rimosso se 403
- **noindex, nofollow**: la pagina non viene indicizzata da motori di ricerca
- **CSP headers**: stesse policy del resto del sito (set_security_headers)

### Sezioni della dashboard

1. **Top Bar**: logo, connection status, agent count, bot uptime, UTC clock
2. **Metriche**: bot status (live/paused/dry-run), posizioni aperte, predictions oggi, WR, costi agent, fase corrente
3. **Agent Grid**: 6 card con status LED, task timeline, messaggio corrente, pensiero, costo, modello, elapsed
4. **Event Log**: ultimi 50 eventi in ordine cronologico inverso, colorati per livello
5. **Quick Actions**: refresh, auto-refresh toggle, download report JSON

### Setup

```bash
# 1. Esegui DDL su Supabase
#    Copia scripts/cockpit_ddl.sql → Supabase SQL Editor → Run

# 2. Aggiungi env var su Railway
#    COCKPIT_TOKEN=<genera con: openssl rand -hex 32>

# 3. Accedi
#    https://btcpredictor.io/cockpit
#    Inserisci il COCKPIT_TOKEN
```
