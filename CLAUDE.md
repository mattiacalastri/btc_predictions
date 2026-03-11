# CLAUDE.md — BTC Predictor Bot
> Aggiornato: 2026-03-10 sess.192 | Leggi PRIMA di toccare qualsiasi file o n8n

---

## STATO ATTUALE (aggiorna ad ogni sessione)
- **BOT: PAUSED** — v2.6.2 | Ghost WR 31.3% (8-9 Mar) | Phase 3 LIVE 10 Mar
- **Phase 3 deployed**: trending_down nuclear gate + dead hours {4,5,6} + threshold 0.58 base
- **Wallet**: $84.39 | **conf_threshold**: 0.62 (Railway) | **n8n base threshold**: 0.58 | **XGB gate**: 62/100
- **Council**: ATTIVO | **Dead hours**: {4, 5, 6} UTC (n8n) | **ACE bias_threshold**: 0.85
- **GO/NO-GO**: monitorare ghost WR 11-12-13 Mar. Target ≥55% per 3gg → GO LIVE

## INFRASTRUTTURA RAPIDA
```
Railway:    web-production-e27d0.up.railway.app
n8n:        n8n.srv1432354.hstgr.cloud
Supabase:   oimlamjilivrcnhztwvj.supabase.co
SSH:        ssh -i ~/.ssh/id_ed25519 srv1432354.hstgr.cloud
Creds:      .env in questa dir (COCKPIT_TOKEN, BOT_API_KEY, tutte le chiavi)
```

## n8n WORKFLOW IDs
```
wf01A  E2LdFbQHKfMTVPOI  Signal Generator
wf01B  OMgFa9Min4qXRnhq  Open Position
wf02   NnjfpzgdIyleMVBO  AI Decision / Check SL-TP
wf05   3YSec3Ny           Timeout Close
wf28   SrxIjlmru3O0Lbv1  Position Monitor v3 (cron 5min)
```
n8n cred IDs: Telegram=`DUBgkzRL1ONUstm5` | Supabase=`xaGS2AzVGYaV8WR8` | OpenRouter=`zV85OtdqGrPi0mt4`

## GOTCHA — non riscoprire queste cose
1. **Railway deploy silenzioso**: SyntaxError non si vede nei log. SEMPRE prima di push:
   `python3 -c "compile(open('app.py').read(),'app.py','exec')" && echo OK`
2. **n8n scheduleTrigger**: NON funziona su Hostinger. Usare SOLO `n8n-nodes-base.cron`
3. **n8n PATCH body**: richiede `{name, nodes, connections, settings}` — MAI staticData/activeVersion
4. **Cron dopo save**: richiede deactivate → reactivate cycle via API, non basta salvare
5. **Rescue webhook**: `POST .../webhook/rescue-wf02 {id: bet_id}` bypassa buffer → Check SL/TP diretto
6. **CB cooldown**: 30min dopo circuit breaker trip. Globals `_CB_TRIPPED_AT`
7. **Zombie bets**: usare Supabase PATCH diretto, NON rescue webhook (race condition su Update Result)
8. **wf28 IF condition**: usare `={{ $json.id }}` non `={{ .id }}`
9. **Ghost evaluate**: COCKPIT_TOKEN funziona sempre. Dual auth attivo. wf02 ogni 30min
10. **Polygon gas**: MAI hardcodare gasPrice. Usare `_get_dynamic_gas_price(w3)` (eth_gasPrice*1.2x, floor 30, ceiling 500 gwei)
11. **Polygon phantom tx**: `send_raw_transaction` ritorna hash PRIMA del mining. Verificare SEMPRE con `eth_getTransactionReceipt`
12. **PolygonScan API V1 deprecata**: usare RPC diretto o V2

## SUPABASE — schema note
- `bet_taken=true` → real bets | `correct=not.null` senza bet_taken → ghost evals
- Colonne aggiunte 7 Mar: `micro_regime_1h`, `micro_strength_1h`
- Migrazione `funding_rate`: **DONE** 7 Mar sess.124 — colonna LIVE in Supabase

## COMANDI FREQUENTI
```bash
# Health + status
curl -H "X-API-Key: $COCKPIT_TOKEN" https://web-production-e27d0.up.railway.app/health
curl -H "X-API-Key: $COCKPIT_TOKEN" .../bot-status

# Ghost evaluate manuale
curl -X POST -H "X-API-Key: $COCKPIT_TOKEN" .../ghost-evaluate

# Pre-deploy check
python3 -c "compile(open('app.py').read(),'app.py','exec')" && echo "OK — safe to push"

# Rescue zombie bet
curl -X POST https://n8n.srv1432354.hstgr.cloud/webhook/rescue-wf02 -d '{"id": BET_ID}'
```

## TOP 3 TASK APERTI
1. **P0** — Monitorare ghost WR 11-12-13 Mar → GO/NO-GO LIVE (target ≥55% 3gg)
2. **P0** — ✅ DONE Crash Prevention sess.192: 9 fix + Polygon gas dinamico + 880 phantom tx cleaned. Deploy `8c8e035`
3. **P1** — ✅ DONE Code Audit Fase 2: 79 test nuovi (portfolio+council), 4 bug fix build_dataset

## DASHBOARD — accesso unico
```
URL:    /cockpit  (COCKPIT_TOKEN login — httpOnly cookie)
Tab Ops: Piano Editoriale + Canali + Schedule (merge di marketing.html — commit 3c24d0e)
/marketing → redirect 302 → /cockpit
Gnav pubblico: link privati rimossi da tutte le 12 pagine
```

## FILE CHIAVE
```
app.py              Flask app + tutti gli endpoint Railway
adaptive_engine.py  ACE engine (starvation fix: commit 4aa9907)
bot/                docs operativi: ace_engine, bot_insights, council_prompts, n8n_debug
pages/cockpit.html  Dashboard unificato (cockpit + ops) — 2259 righe
pages/marketing.html DEPRECATED — tenuto come archivio, non più servito
```

---

## DOMAIN KNOWLEDGE (teoria — leggi solo se serve analisi)

### Frame 1 — Trader Istituzionale
- **Funding rate**: >0.05% positivo = longs pagano shorts. Quando torna a zero → pressione buy
- **CVD divergente** dal prezzo = esaurimento trend. CVD allineato = conferma
- **Kelly criterion**: con WR 54% e RR 2x → f* ≈ 8.8%. Sistema usa ~2% (corretto in fase early)
- **Expectancy**: E = (WR × avg_win) - ((1-WR) × avg_loss). Ottimizzare E, non WR

### Frame 2 — Crypto Expert
- **Funding settlement**: ogni 8h (00:00, 08:00, 16:00 UTC) → pattern tradable
- **Long/Short Ratio >60% long** → mercato sovraffollato → segnale contrarian
- **SOPR >1** = profit-taking | **<1** = capitolation

### Frame 3 — Blockchain
- Contratto `BTCBotAudit.sol` su Polygon PoS: `0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55`
- `commitPrediction()` chiamato in wf01B PRIMA del fill Kraken
- `resolvePrediction()` chiamato in wf02 DOPO chiusura
- `continueOnFail: true` su tutti i nodi on-chain — blockchain failure NON blocca trading

### Heuristics di trading
```
1. Il mercato si muove verso il punto di massimo danno per il massimo numero di partecipanti
2. Mai contro il funding se >0.08% e vuoi long
3. Reversal più affidabile: 3 candele consecutive stessa direzione + volume decrescente
4. 80% del tempo = ranging. Il sistema deve filtrare il ranging (bassa confidenza = no trade)
```
