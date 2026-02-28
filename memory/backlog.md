# Backlog — BTC Predictor Bot
> Aggiornato: 2026-02-28 (fine sessione)

---

## 🔴 Alta priorità — BLOCCA il database reset del 1° Marzo

> Questi 3 fix devono essere completati PRIMA del reset. Riattivare senza risolverli
> contamina il dataset certificato e invalida le claim di verifiability on-chain.

| # | Task | Note |
|---|------|------|
| 1 | Fix CNBC RSS — sostituire con Bloomberg RSS o Reuters Markets | Feed attuale ~24h ritardo → AI decide su news stantie. Contraddice "real numbers". |
| 2 | Fix PENDING cleanup — prediction non-bet restano PENDING per sempre | Dati incompleti nel DB certificato. Nodo dedicato in 08_Position_Monitor o wf separato |
| 3 | Fix ghost_exit_price — shadow evaluation non popolata | Verifica che 02_BTC_Trade_Checker scriva su `ghost_exit_price`. Fondamentale per trasparenza verifiable. |

---

## 🟠 Critico per la tesi on-chain — fare subito dopo il go-live

| # | Task | Note |
|---|------|------|
| 4 | Audit timing & sincronizzazione pipeline completa | `commitPrediction()` Polygon deve avvenire PRIMA del fill Kraken. Se il timestamp on-chain è dopo il fill, la claim di verifiability crolla. Verificare con dati reali post go-live. |
| 5 | Implementare lock/mutex tra cicli 01A/01B | Rischio: 01A si accumula e lancia 01B mentre 02 è ancora aperto → posizioni sovrapposte → dati ambigui |

---

## 🟡 Media priorità — roadmap crescita

| # | Task | Note |
|---|------|------|
| 6 | Social publishing attivo post go-live | 09A ora su OpenRouter Gemini Flash. Attivare dopo 10+ trade certificati. Il go-live del 1° Marzo è un evento narrativo forte. |
| 7 | Migrare nodi Anthropic → OpenRouter (workflow INATTIVI) | Da fare: 07_Telegram_Commander, 09B_Social_Publisher, 12_Email_Handler. Tutti INACTIVE, no urgenza. |

---

## 🟢 Bassa priorità — dipendono dai dati

| # | Task | Note |
|---|------|------|
| 8 | Pattern memory "n/a (insufficient history)" | Si risolve autonomamente dopo ~50 trade reali certificati |
| 9 | XGBoost — modello non ancora utile | Richiede 200+ prediction. Monitorare dopo milestone |
| 10 | Regime label come feature XGBoost | P1 per il prossimo ciclo di training |

---

## 🤖 Stato AI Models (aggiornato 28 Feb 2026)

> **Tutti i workflow attivi sono ora su OpenRouter. Zero dipendenza da Anthropic.**

| Workflow | Nodo AI | Modello attuale | Note |
|----------|---------|-----------------|------|
| 01B — Prediction Bot | BTC Prediction Bot | `google/gemini-2.5-flash` (OpenRouter) | ✅ Migrato (sessione mattina) |
| 02 — Exit Decision | Message a model | `mistralai/mistral-small-3.1-24b` (OpenRouter) | ✅ Migrato (sessione pomeriggio) |
| 04 — Talker | Message a model + Channel: Claude | `google/gemini-2.5-flash` (OpenRouter) | ✅ Migrato (sessione pomeriggio) |
| 09A — Social Manager | HTTP Call API | `google/gemini-2.5-flash` (OpenRouter) | ✅ Migrato (sessione pomeriggio) |
| 07, 09B, 12 | vari | Claude Haiku (Anthropic) | ⚠️ INACTIVE — bassa urgenza |

**Credenziale OpenRouter**: ID `zV85OtdqGrPi0mt4` — saldo $8.88 (28 Feb)

---

## 📅 Sequenza go-live consigliata (allineata con visione)

```
28 Feb (oggi):  Fix #1 CNBC RSS + Fix #2 PENDING + Fix #3 ghost_exit_price
1 Marzo:        Database reset → archivio dati sviluppo → riattiva bot + rimuovi banner
Settimane 1-2:  Audit #4 timing on-chain + Fix #5 lock/mutex
Settimane 3+:   Social publishing (#6) con dati reali certificati
Mese 2+:        Outreach influencer (vedi Roadmap futura sotto)
```

---

## 💡 Roadmap futura

### AI Influencer & Enthusiast Outreach automatico
- Workflow n8n dedicato (es. 13_BTC_Outreach) — outreach su X/Twitter, Telegram, Reddit
- Template personalizzati con AI (Gemini Flash) — value-first, non spam
- Target: creatori crypto/AI 1K-100K follower
- **Timing**: dopo 50+ trade certificati con WR e PnL verificabili su Polygonscan
- **Sinergia**: early-access testers → testimonial credibili → flywheel

---

## ✅ Completati — 28 Feb 2026 (ore 12:07, n8n manuale)

- Fix candela aperta (`.slice(0, -1)`) su Format Binance Klines e Format MTF
- Sostituzione Sole24Ore → CoinTelegraph RSS
- Migrazione Anthropic → OpenRouter Gemini 2.5 Flash (01B)
- Eliminazione NO_BET dal sistema (schema, system prompt, user prompt)
- Fix constraint Supabase `direction_check`
- Nuovo STEP 3 con formato ibrido `FORCE:/CAP:/PENALTY:`
- Fix `market_regime` con MTF override
- 9 nuove colonne Supabase per re-training ML
- Dashboard transparency — widget costo sviluppo one-time vs recurring
- Fix bug 08_BTC_Position_Monitor — filtro Supabase `correct.is.null` (keyValue vuoto)

## ✅ Completati — 28 Feb 2026 (sessione pomeriggio, Claude Code)

- Migrazione 02_BTC_Trade_Checker: `@langchain.anthropic` → HTTP OpenRouter `mistral-small-3.1-24b`
  - `Parse AI Decision` già leggeva `choices[0].message.content` ✅
  - Rimosso nodo `Think` (ai_tool, non necessario per EXIT/HOLD semplice)
- Migrazione 04_BTC_Talker: entrambi i nodi Anthropic → HTTP OpenRouter `gemini-2.5-flash`
  - `Parse Commentary` già leggeva `choices[0].message.content` ✅
  - `Channel Personality` (Telegram) aggiornato: `content[0].text` → `choices[0].message.content` ✅
- Migrazione 09A_BTC_Social_Manager: `HTTP — Call Claude API` → OpenRouter `gemini-2.5-flash`
  - `Code — Parse Claude Response` aggiornato: `content[0].text` → `choices[0].message.content` ✅
- Aggiornamento backlog con strategia AI ibrida e sequenza go-live allineata alla visione
