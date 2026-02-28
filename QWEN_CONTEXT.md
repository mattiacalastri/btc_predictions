# BTC Prediction Bot — Qwen Context Map
> Versione: 2026-02-28 | Ottimizzato per Qwen 2.5 Coder 7B (32K ctx)
> Leggi questo file per intero prima di qualsiasi task sul repo.

---

## § 01 — Cos'è questo progetto

Un bot di trading automatico su BTC Futures (Kraken, simbolo `PF_XBTUSD`) che:
1. Ogni 15 minuti raccoglie dati di mercato (wf01A in n8n)
2. Chiede a un LLM (via OpenRouter) se il prossimo movimento è UP o DOWN con una confidence 0.0–1.0
3. Se confidence ≥ 0.65 e XGBoost concorda → piazza un ordine reale su Kraken
4. Monitora la posizione ogni 3 minuti (wf08) con SL -1.2% / TP +2.4%
5. Chiude la posizione e registra il risultato su Supabase + Polygon PoS (audit on-chain)

**Capitale attuale**: $100 USDC su Kraken Futures
**Stato corrente**: Day 0 appena eseguito (DB vuoto, raccolta dati puliti in corso)

---

## § 02 — Stack tecnico

```
Flask/Gunicorn ──→ Railway (https://web-production-e27d0.up.railway.app)
    app.py           2600+ righe — monolite (refactoring futuro in P3)
    constants.py     TAKER_FEE=0.00005, _BIAS_MAP (ordinale -2→+2)
    train_xgboost.py retrain manuale XGBoost su Railway
    build_dataset.py costruisce DataFrame da Supabase → features ML

n8n (self-hosted) ── VPS Hostinger https://n8n.srv1432354.hstgr.cloud
    16 workflow attivi (vedi § 07)

Supabase ─────────── project: oimlamjilivrcnhztwvj
    tabelle: btc_predictions, bot_state, email_drafts, contributors,
             aureo_content, aureo_subscribers, claude_tasks

Kraken Futures ───── PF_XBTUSD | Taker fee: 0.005% per lato

Polygon PoS ─────── Contratto BTCBotAudit.sol
    0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55
    Funzioni: commit(bet_id, hash) · resolve(bet_id, correct, pnl)
```

---

## § 03 — Schema Supabase: tabella `btc_predictions`

Colonne chiave (non modificare i nomi — usati da n8n, app.py, build_dataset.py):

```
id                    BIGINT PK autoincrement
created_at            TIMESTAMPTZ
direction             TEXT  "UP" | "DOWN"
confidence            FLOAT  0.0–1.0
bet_taken             BOOL   true se ordine piazzato, false se SKIP
correct               BOOL   NULL=aperto, true=WIN, false=LOSS
pnl_usd               FLOAT  netto (gross - fee - funding)
classification        TEXT   "BET"|"SKIP"|"NOISE"|"ALERT"
bet_size              FLOAT  BTC (es. 0.002)
entry_fill_price      FLOAT  prezzo fill reale Kraken
btc_price_entry       FLOAT  prezzo Binance al momento del segnale
signal_price          FLOAT  prezzo al momento del segnale LLM
fear_greed_value      INT    0–100 (Fear & Greed Index)
rsi14                 FLOAT  RSI 14 periodi
technical_score       FLOAT  punteggio tecnico LLM (-3→+3)
technical_bias        TEXT   "strong_bullish"|"bullish"|...|"strong_bearish"
technical_bias_score  INT    ordinale -2→+2 (da _BIAS_MAP in constants.py)
signal_fg_fear        BOOL   true se fear_greed_value < 45
funding_rate          FLOAT  tasso Binance Futures perpetui
ls_ratio              FLOAT  Long/Short ratio
onchain_commit_hash   TEXT   keccak hash commitato su Polygon
onchain_commit_tx     TEXT   txHash Polygon del commit
onchain_resolve_tx    TEXT   txHash Polygon del resolve
fetch_failed          BOOL   true se fetch macro calendar fallito
pyramid_count         INT    numero add-on alla posizione
sl_price              FLOAT  prezzo stop-loss reale piazzato su Kraken
tp_price              FLOAT  prezzo take-profit calcolato
rr_ratio              FLOAT  TP/SL distance ratio (default: 2.0)
```

---

## § 04 — On-Chain Audit (Polygon PoS)

**Flusso**:
1. `wf01B` → POST `/commit-prediction` → `BTCBotAudit.commit(bet_id, keccak_hash)` → salva `onchain_commit_tx`
2. `wf02` → POST `/resolve-prediction` → `BTCBotAudit.resolve(bet_id, correct, pnl_scaled)` → salva `onchain_resolve_tx`

**Hash deterministico** (codificato in Solidity keccak256):
```python
Web3.solidity_keccak(
    ["uint256","string","uint256","uint256","uint256","uint256"],
    [bet_id, direction, int(confidence*1e6), int(entry_price*1e2),
     int(bet_size*1e8), ts]
)
```

**Regola di sicurezza**: tutti i nodi n8n che chiamano `/commit-prediction` e `/resolve-prediction` hanno `continueOnFail: true`. Un fallimento on-chain non blocca il trading.

**Env vars Railway richieste**: `POLYGON_PRIVATE_KEY`, `POLYGON_CONTRACT_ADDRESS`, `POLYGON_RPC_URL` (default: `https://polygon-bor-rpc.publicnode.com`), `POLYGON_CHAIN_ID` (137).

---

## § 05 — Logica di Trading (app.py)

### Gate sequenziale in `place_bet()`

```
1. Dead hours filter   → DEAD_HOURS_UTC = {5,7,10,11,17,19} UTC (fallback)
                          Si aggiorna automaticamente da dati storici (WR < 45%)
                          via /reload-calibration

2. XGBoost dual-gate  → _run_xgb_gate()
                          BYPASS AUTOMATICO se clean_bets < XGB_MIN_BETS (100)
                          Quando attivo: se XGB direction ≠ LLM direction → SKIP

3. Bot paused check   → _check_pre_flight()
                          Legge _BOT_PAUSED da Supabase bot_state (cache 5min)

4. Circuit breaker    → 3 losses consecutive → _save_bot_paused(True)
                          Riattivare con POST /resume

5. MAX_OPEN_BETS      → hard cap 2 posizioni aperte simultanee

6. Ordine Kraken      → market order → SL reale (stp, reduceOnly)
                          SL = entry ± 1.2% | TP = entry ± 2.4% (RR 2:1)
```

### Sizing (`bet_sizing()`)

```
base = 0.002 BTC (env BASE_SIZE)
× streak_mult:  1.5× (3+ wins, conf≥0.75) | 1.2× (3+ wins) | 0.5× (2+ losses)
× drawdown:     0.25× se recent PnL < -$0.15
× conf_mult:    max(0.8, min(1.2, 1.0 + (conf - 0.65) × 2.0))
clamp: 0.001 – 0.005 BTC
```

### Soglie confidence

```
CONF_THRESHOLD = 0.65  (env Railway, default 0.65 da app.py v2.5.2)
CONF_PYRAMID   = 0.75  (soglia per pyramid add-on e streak mult 1.5×)
CONF_REVERSE   = 0.75  (soglia per inversione posizione in perdita)
```

---

## § 06 — Helper critici in app.py (non ridefinire)

```python
_sb_config()          → (supabase_url, supabase_key) — SEMPRE usare questo
_check_api_key()      → verifica header X-API-Key con hmac.compare_digest
_check_read_key()     → auth read-only (READ_API_KEY o BOT_API_KEY)
_check_rate_limit()   → sliding window 60s in-memory
_get_clean_bet_count()→ conta bet pulite, cache 10min
_run_xgb_gate()       → dual-gate XGB, bypass se < 100 clean bets
_check_pre_flight()   → bot_paused + circuit_breaker check
```

**Import critici**:
```python
from constants import TAKER_FEE, _BIAS_MAP   # NON ridefinire inline
import datetime as _dt                        # usato così in tutto il file
from joblib import load as joblib_load        # NON usare pickle
```

---

## § 07 — n8n Workflows (IDs VPS)

| ID | Nome | Trigger |
|----|------|---------|
| `E2LdFbQHKfMTVPOI` | `01A_BTC_AI_Inputs` | Cron 15min |
| `OMgFa9Min4qXRnhq` | `01B_BTC_Prediction_Bot` | Chiamato da 01A |
| `NnjfpzgdIyleMVBO` | `02_BTC_Trade_Checker` | Chiamato da 01B + wf08 |
| `Fjk7M3cOEcL1aAVf` | `08_BTC_Position_Monitor` | Cron 3min |
| `nzMMmMC6Q9eysUBP` | `07_BTC_Telegram_Commander` | Telegram webhook |
| `mKC0Y4YDjUf3I2dp` | `11_BTC_Channel_Content` | Cron |
| `O1JlHp7tgVFBfrwm` | `06_BTC_System_Watchdog` | Cron |

---

## § 08 — Endpoint Flask principali

```
GET  /health                → stato sistema, xgb_gate_active, conf_threshold
GET  /position              → posizione aperta Kraken
GET  /signals?limit=N       → ultimi N segnali da Supabase (READ_API_KEY)
GET  /performance-stats     → WR, PnL, streak, expectancy
GET  /costs                 → breakdown costi mensili
POST /place-bet             → piazza ordine (X-API-Key obbligatorio)
POST /close-position        → chiudi posizione (X-API-Key)
POST /resume                → riattiva bot dopo circuit breaker
POST /reload-calibration    → aggiorna dead hours + calibration da DB
POST /rescue-orphaned       → chiude bet PENDING senza wf02 attivo
POST /commit-prediction     → audit on-chain Polygon (commit)
POST /resolve-prediction    → audit on-chain Polygon (resolve)
GET  /marketing             → dashboard marketing pubblica
GET  /aureo                 → pagina AUREO (educazione finanziaria)
```

Autenticazione: header `X-API-Key: <BOT_API_KEY>` (Railway env).

---

## § 09 — Regole per modificare il codice

### ✅ Fai sempre
- Usa `_sb_config()` per ottenere URL e key Supabase
- Usa `SUPABASE_TABLE` (non hardcodare `"btc_predictions"`)
- Usa `from constants import TAKER_FEE` per calcolo fee
- Aggiungi `continueOnFail: true` su tutti i nodi n8n on-chain
- Dopo modifica `index.html`, valida il JS con node --check

### ❌ Non fare mai
- Non hardcodare credenziali nel codice
- Non importare `pickle` — usa `joblib`
- Non usare `str(e)` nelle risposte JSON degli endpoint pubblici → usa `"internal_error"`
- Non aggiungere nuove tabelle Supabase senza RLS policy
- Non fare `git push` diretto senza test di sintassi Python
- Non modificare `constants.py` senza aggiornare tutti i file che lo importano
- Non usare `$env.X` in n8n VPS → usa `$vars.X`

---

## § 10 — Stato corrente (Day 0 — 2026-02-28)

```
DB btc_predictions:   VUOTO (TRUNCATE eseguito oggi)
Modelli XGBoost:      RIMOSSI (pre-Day0 tautologici)
XGBoost gate:         BYPASS ATTIVO (0/100 clean bets)
Bot:                  LIVE — raccolta dataset in corso
Confidence threshold: 0.65
Segnali attesi:       SKIP finché LLM conf < 0.65 (mercato ranging)
Primo retrain pulito: ~150-200 bets (stima: 3 settimane)
```

---

## § 11 — Template prompt per Qwen

### Analisi bug
```
Leggi app.py dalla riga X alla riga Y.
Il bug è: [descrizione].
Non modificare nulla fuori da quella funzione.
Proponi il fix minimo senza refactoring aggiuntivo.
```

### Query Supabase
```
Usando il tool Supabase MCP, esegui questa query su btc_predictions:
[SQL]
Dimmi cosa mostrano i risultati in relazione a [contesto].
```

### Modifica n8n
```
Usando il tool n8n MCP, leggi il workflow [ID].
Trova il nodo [nome].
Proponi la modifica al parametro [campo] da [vecchio] a [nuovo].
Non applicare — mostrami prima il diff.
```

### Review codice
```
Leggi [file] righe X-Y.
Verifica che rispetti le regole del § 09 di QWEN_CONTEXT.md.
Segnala solo violazioni reali, non suggerire refactoring non richiesto.
```
