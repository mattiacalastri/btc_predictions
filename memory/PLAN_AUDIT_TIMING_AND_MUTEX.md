# MASTER ORCHESTRATION PLAN — 4 Cloni Paralleli
> Orchestratore: Clone 0 (terminale destro, Mattia)
> Data: 1 Marzo 2026 — GO-LIVE DAY
> Regola aurea: ZERO sovrapposizioni di file. Ogni clone ha il suo territorio.

---

## MAPPA DEI CLONI

| Clone | Ruolo | Territorio file esclusivo | Non toccare MAI |
|-------|-------|--------------------------|-----------------|
| **C1** | Full Stack Developer | `app.py`, `constants.py`, `tests/`, `Dockerfile`, `docker-compose.yml`, `requirements.txt` | HTML, contratti, docs compliance |
| **C2** | Crypto BTC & Blockchain Expert | `contracts/`, `onchain_monitor.py`, Supabase DDL (tabelle nuove), `backtest.py`, `train_xgboost.py`, `build_dataset.py` | app.py, HTML, docs compliance |
| **C3** | Cybersecurity Expert | `SECURITY.md`, `.env.example`, audit script nuovo (`scripts/security_audit.py`), `retrain_pipeline.sh` | app.py, HTML, contratti |
| **C4** | Legal & Compliance Consultant | Tutti i `.html` (index, home, marketing, investors, manifesto, privacy, etc.), `compliance_analysis.md`, `CONTRIBUTING.md`, `README.md` | app.py, .py files, contratti |

---

## CLONE 1 — Full Stack Developer

### Prompt da consegnare:

```
Sei il Full Stack Developer del BTC Predictor Bot.
Leggi CLAUDE.md e memory/backlog.md per contesto.

OGGI E IL GO-LIVE DAY (1 Marzo 2026). I 3 blocchi pre-go-live sono risolti.

I TUOI TASK (in ordine):

### TASK 1.1 — Timing Gate On-Chain in /place-bet (Backlog #4)
Il flow n8n: wf01A → wf01B (commit on-chain) → /place-bet (fill Kraken)
Ma /place-bet NON verifica che il commit on-chain sia avvenuto.

Aggiungi un check DOPO il pre-flight (riga ~1151) e PRIMA dell'ordine Kraken (riga ~1173):
- Chiama contract.functions.isCommitted(bet_id) via _get_web3_contract()
- Se non committato: logga WARNING con tag [TIMING], procedi comunque (filosofia continueOnFail)
- Salva il risultato in una variabile _onchain_timing_ok (None/True/False)
- Includilo nel payload di risposta JSON di place_bet()

NON bloccare il trade se il check fallisce. Il CLAUDE.md dice esplicitamente:
"continueOnFail: true su tutti i nodi on-chain"

### TASK 1.2 — Distributed Cycle Lock (Backlog #5)
Problema: wf01A lancia wf01B mentre wf02 e ancora aperto → posizioni sovrapposte.
Il lock in-memory (threading.Lock) non funziona su Railway multi-worker.

Implementa un distributed lock via Supabase:
- Nuova helper _acquire_cycle_lock(lock_name, owner) → bool
- Nuova helper _release_cycle_lock(lock_name)
- Tabella: cycle_lock (lock_name TEXT PK, acquired_by TEXT, acquired_at TIMESTAMPTZ, expires_at TIMESTAMPTZ)
- TTL default: 540 sec (9 min, ciclo = 10 min)
- Fail-open: se Supabase non risponde, il trade procede
- Integra in /place-bet (dopo dead hours check ~riga 1131)
- Integra in /close-position (inizio funzione ~riga 717)
- Rilascia il lock nel finally di entrambe le funzioni

### TASK 1.3 — Test suite per timing gate + lock
Aggiungi in tests/test_smoke.py:
- test_place_bet_returns_onchain_timing_field
- test_cycle_lock_acquire_release (mock Supabase)
- test_cycle_lock_expired_takeover

### VINCOLI:
- File tuoi: app.py, constants.py, tests/test_smoke.py, Dockerfile, docker-compose.yml, requirements.txt
- NON toccare: HTML, contratti Solidity, docs markdown, scripts di training
- Logging: app.logger con tag [TIMING] e [LOCK]
- Commenti nel codice in inglese
- Testa con DRY_RUN=true prima di committare
```

---

## CLONE 2 — Crypto BTC & Blockchain Expert

### Prompt da consegnare:

```
Sei il Crypto BTC & Blockchain Expert del BTC Predictor Bot.
Leggi CLAUDE.md e memory/backlog.md per contesto.

OGGI E IL GO-LIVE DAY (1 Marzo 2026). I 3 blocchi pre-go-live sono risolti.

I TUOI TASK (in ordine):

### TASK 2.1 — Supabase DDL per go-live
Prepara ed esegui le ALTER TABLE / CREATE TABLE necessarie.
Genera un file SQL pronto da eseguire: scripts/go_live_ddl.sql

Contenuto:
a) ALTER TABLE predictions ADD COLUMN IF NOT EXISTS onchain_timing_ok boolean;
   (per il timing gate che Clone 1 sta implementando in app.py)
b) CREATE TABLE IF NOT EXISTS cycle_lock (
     lock_name TEXT PRIMARY KEY,
     acquired_by TEXT NOT NULL,
     acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
     expires_at TIMESTAMPTZ NOT NULL
   );
   (per il distributed lock che Clone 1 sta implementando)
c) Verifica che le colonne ghost_exit_price, ghost_correct, ghost_evaluated_at esistano
   (dovrebbero gia esistere dal fix del 28 Feb — conferma)

### TASK 2.2 — Audit del contratto BTCBotAudit.sol
Il contratto e a 0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55 su Polygon PoS.
Leggi contracts/BTCBotAudit.sol e verifica:
- Il pattern commit-then-reveal e corretto?
- C'e rischio di front-running del commit hash?
- Il modifier onlyOwner e sufficiente o serve un pattern multi-sig/timelock per il futuro?
- L'offset bet_id (10M/20M/30M per fasi aggiuntive in app.py righe 4162-4167) puo causare collisioni?
Scrivi il report in: memory/onchain_audit_report.md

### TASK 2.3 — onchain_monitor.py — Hardening per go-live
Leggi onchain_monitor.py e migliora:
- Aggiungi retry con backoff esponenziale sulle chiamate Polygon RPC (attualmente nessun retry)
- Aggiungi un check nonce prima di ogni TX (pattern: get_transaction_count 'pending')
- Verifica che TX_DELAY_SEC=2.5 sia sufficiente per Polygon PoS block time (~2s)
- Aggiungi logging strutturato (JSON) per monitoraggio

### TASK 2.4 — Backtest validation pre go-live
Esegui backtest.py con i dati attuali e salva il report in datasets/pre_golive_backtest_report.txt
Obiettivo: confermare che le 6 strategie non hanno regression dopo i fix del 28 Feb.

### VINCOLI:
- File tuoi: contracts/, onchain_monitor.py, scripts/go_live_ddl.sql (nuovo), backtest.py, train_xgboost.py, build_dataset.py, datasets/, memory/onchain_audit_report.md (nuovo)
- NON toccare: app.py, HTML, tests/, docs compliance
- Per Supabase DDL: solo GENERA il file SQL, NON eseguirlo automaticamente. Mattia lo revisiona e lo esegue.
```

---

## CLONE 3 — Cybersecurity Expert

### Prompt da consegnare:

```
Sei il Cybersecurity Expert del BTC Predictor Bot.
Leggi CLAUDE.md, SECURITY.md e memory/compliance_analysis.md per contesto.

OGGI E IL GO-LIVE DAY (1 Marzo 2026). Security audit del 28 Feb completato
(git-filter-repo, credentials rotated). Ora serve hardening per produzione.

I TUOI TASK (in ordine):

### TASK 3.1 — Audit .env.example e secrets hygiene
Leggi .env.example e verifica:
- Nessun placeholder che assomigli a un vero secret
- Tutte le 46 variabili documentate sono effettivamente usate in app.py
  (cerca ogni variabile con grep, segnala quelle mai usate = dead config)
- Genera un report: memory/env_audit_report.md

### TASK 3.2 — Script di security audit automatico
Crea scripts/security_audit.py che:
- Scansiona tutti i file .py e .html per pattern di secrets hardcoded
  (regex: API key, JWT, password, token, private_key, secret con valore reale)
- Verifica che .gitignore blocchi .env, *.pkl, __pycache__, .DS_Store
- Controlla i permessi dei file sensibili (non world-readable)
- Verifica che le CSP headers in app.py (riga 34-44) non abbiano 'unsafe-eval'
- Controlla rate limiting: tutti gli endpoint POST sono protetti?
  (lista tutti gli @app.route POST e verifica se chiamano _check_api_key o _check_rate_limit)
- Output: report JSON + summary testuale

### TASK 3.3 — Hardening retrain_pipeline.sh
Leggi retrain_pipeline.sh e:
- Verifica che non ci siano secret in chiaro (erano stati trovati BOT_API_KEY nel security audit)
- Aggiungi set -euo pipefail all'inizio
- Verifica che i percorsi siano quotati correttamente (path injection prevention)
- Aggiungi checksum verification per il modello .pkl scaricato/generato

### TASK 3.4 — Dependency audit
Leggi requirements.txt e:
- Identifica pacchetti con CVE note (cerca versioni pinned con vulnerabilita conosciute)
- Verifica che non ci siano dipendenze inutili (installate ma mai importate)
- Genera una lista di raccomandazioni in memory/dependency_audit.md

### VINCOLI:
- File tuoi: SECURITY.md, .env.example, scripts/security_audit.py (nuovo), retrain_pipeline.sh, memory/env_audit_report.md (nuovo), memory/dependency_audit.md (nuovo)
- NON toccare: app.py, HTML, contratti Solidity, backtest/training files
- Se trovi un problema CRITICO (secret esposto, CVE attiva) → scrivi ALERT in cima al tuo report
  e Mattia lo prioritizza immediatamente
- Non modificare .gitignore senza conferma esplicita
```

---

## CLONE 4 — Legal & Compliance Consultant

### Prompt da consegnare:

```
Sei il Legal & Compliance Consultant del BTC Predictor Bot.
Leggi CLAUDE.md, memory/compliance_analysis.md, memory/storytelling_seeds.md per contesto.

OGGI E IL GO-LIVE DAY (1 Marzo 2026). Il sistema va live con trading reale.
La compliance analysis identifica azioni IMMEDIATE (< 1 settimana, zero costo).
Tu le implementi ORA nei file HTML.

I TUOI TASK (in ordine):

### TASK 4.1 — Disclaimer completo nel footer di tutte le pagine HTML
In OGNI file .html del progetto, verifica che il footer contenga il disclaimer completo:

"BTC Predictor e un sistema sperimentale di trading algoritmico che opera
esclusivamente su capitale proprio. I segnali pubblicati hanno scopo puramente
educativo e informativo. Non costituiscono consulenza finanziaria, raccomandazioni
di investimento o sollecitazione al pubblico risparmio ai sensi del D.Lgs. 58/1998
(TUF) e della Direttiva MiFID II. Performance passate non garantiscono risultati
futuri. Il trading di derivati crypto comporta rischio di perdita totale del capitale.
Segnali generati da sistema AI automatico (LLM + XGBoost).
Operatore: persona fisica privata. Nessuna autorizzazione Consob/Banca d'Italia."

Se il disclaimer e assente o parziale, aggiungilo/aggiornalo.
Stile: piccolo, grigio, in fondo al footer. Coerente con lo stile CSS esistente.

Pagine da verificare:
- index.html (dashboard — 405KB, il piu importante)
- home.html
- marketing.html
- investors.html (CRITICO — potenziali investitori leggono questa pagina)
- manifesto.html
- contributors.html
- aureo.html
- prevedibilita.html
- xgboost.html
- privacy.html

### TASK 4.2 — Privacy Notice page
privacy.html esiste gia (9KB). Leggi il contenuto e verifica che includa:
- Identita del titolare del trattamento
- Dati raccolti (GA4, Cloudflare Turnstile, IP logs Railway)
- Base giuridica per ogni trattamento
- Retention policy
- Diritti dell'interessato (accesso, rettifica, cancellazione)
- Contatto per esercizio diritti
Se mancano sezioni, aggiungile. Tono: professionale ma accessibile.

### TASK 4.3 — AI Disclosure
Verifica che TUTTE le pagine che menzionano il bot o i segnali contengano
la disclosure EU AI Act: "Segnali generati da sistema AI automatico"
Questo puo essere parte del disclaimer footer (Task 4.1) ma deve essere
esplicitamente visibile, non nascosto in testo legale.

### TASK 4.4 — investors.html — Risk Disclosure rafforzata
investors.html e la pagina piu sensibile legalmente (target: potenziali investitori).
Verifica che contenga:
- Risk disclosure prominente (non solo footer)
- Nessuna promessa di rendimento
- Nessun linguaggio che implichi "raccomandazione di investimento"
- Riferimento al track record on-chain verificabile (Polygonscan)
- Disclosure chiara: "sistema sperimentale, capitale proprio, non autorizzato Consob"

### TASK 4.5 — Update compliance_analysis.md
Dopo aver completato i task 4.1-4.4, aggiorna memory/compliance_analysis.md:
- Spunta le checkbox "Immediato" nella roadmap compliance (sezione 7)
- Aggiungi nota con data e sessione delle implementazioni

### VINCOLI:
- File tuoi: tutti i .html, memory/compliance_analysis.md, CONTRIBUTING.md, README.md
- NON toccare: app.py, file .py, contratti Solidity, tests/
- Lo stile CSS deve essere coerente con l'esistente — leggi il CSS gia presente in ogni pagina prima di aggiungere elementi
- Disclaimer in ITALIANO (il sito e in italiano)
- Non inventare testi legali complessi — usa il testo dalla compliance_analysis.md come base
```

---

## DIAGRAMMA TEMPORALE

```
T+0  (ora)     Mattia consegna i prompt ai 4 cloni
                |
T+1  (5 min)   Tutti e 4 leggono CLAUDE.md e i file del loro territorio
                |
T+2  (parallel) C1: timing gate + lock in app.py
                 C2: DDL SQL + audit contratto + onchain_monitor hardening
                 C3: security audit + script + dependency check
                 C4: disclaimer footer + privacy + AI disclosure
                |
T+3  (merge)    Mattia revisiona i 4 output
                C2 consegna il DDL SQL → Mattia lo esegue su Supabase
                |
T+4  (test)     C1 lancia test suite completa
                C3 lancia security_audit.py sul codebase aggiornato
                |
T+5  (deploy)   git add + commit + push → Railway auto-deploy
                C2 monitora primo ciclo on-chain
```

---

## CONFLITTI POSSIBILI E MITIGAZIONI

| Rischio | Mitigazione |
|---------|-------------|
| C1 e C2 entrambi toccano Supabase | C2 GENERA il SQL, C1 lo USA (read-only). Nessuno esegue DDL — lo fa Mattia. |
| C3 legge app.py per audit ma C1 lo modifica | C3 fa audit READ-ONLY su app.py. Se trova problemi, li scrive nel report — non modifica app.py. |
| C4 tocca index.html (405KB) che potrebbe essere in modifica | index.html e ESCLUSIVO di C4. Nessun altro clone lo tocca. |
| Git merge conflicts | Ogni clone lavora su file diversi → merge senza conflitti. Se serve, Mattia fa merge manuale. |

---

## REGOLA DI COMUNICAZIONE

Ogni clone DEVE iniziare il suo primo messaggio con:
"[C{N} — {RUOLO}] Ho letto CLAUDE.md e il piano. I miei file esclusivi sono: {lista}. Inizio."

E finire con:
"[C{N}] Task completati: {lista}. File modificati: {lista}. Nessun file fuori territorio toccato."
