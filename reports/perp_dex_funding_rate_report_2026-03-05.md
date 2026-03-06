# Report: Perpetual DEX, Funding Rate & AI Monetization
> Data: 2026-03-05 | Per: BTC Predictor Bot + Astra AI Monetization System

---

## 1. Executive Summary

Il settore Perp DEX ha superato $1.2T di volume nel 2025 e sta accelerando nel 2026. I funding rate sui perpetual decentralizzati rappresentano un'opportunità diretta per il nostro sistema predittivo: se il bot predice correttamente la direzione BTC, possiamo amplificare i profitti posizionandoci sul lato giusto del funding rate su DEX decentralizzati, incassando sia il movimento di prezzo che il funding.

**Opportunita chiave identificate:**
- 7 protocolli perp DEX con airdrop attivi o imminenti
- Funding rate arbitrage automatizzabile via bot AI
- Idea prodotto: AI Airdrop Picker 100% automatico

---

## 2. Protocolli Analizzati — Classifica per Priorita

### TIER 1 — Azione immediata (Mar 2026)

| Protocollo | Chain | Stato | Opportunita |
|---|---|---|---|
| **EdgeX** | Ethereum→EDGE Chain | TGE entro 31 Mar | XP→token 1:1. Farmmare ORA |
| **Paradex ($DIME)** | Starknet | Airdrop LIVE 5 Mar | Claim entro 2 settimane. 25% supply |
| **GRVT** | ZKsync | TGE tardo Q1 2026 | 22% supply airdrop. Season 1 attiva |
| **Avantis ($AVNT)** | Base | Token LIVE $0.176 | Airdrop 2 claim 5-8 Mar. Zero fee |

### TIER 2 — Posizionamento early (Q2-Q3 2026)

| Protocollo | Chain | Stato | Opportunita |
|---|---|---|---|
| **Variational ($VAR)** | Arbitrum | Points attivi | TGE Q3-Q4. 50% supply community. RFQ unico |
| **DESK (ex HMX)** | Arbitrum→Base | Rebrand completato | Points attivi. Focus AI agents + Eliza OS |
| **Cascade** | TBD | Mainnet Q1 2026 | $15M da Polychain+Variant+Coinbase. Neo-brokerage |

---

## 3. Integrazione con BTC Predictor Bot

### 3.1 Funding Rate come Segnale Predittivo

I funding rate sono un indicatore di sentiment di mercato:
- **Funding positivo alto** → mercato over-leveraged long → probabile correzione
- **Funding negativo** → mercato over-leveraged short → probabile squeeze
- Il bot puo integrare funding rate aggregati (via Loris Tools API o scraping) come feature aggiuntiva nel modello XGBoost

**Azione**: aggiungere `funding_rate_avg` e `funding_rate_skew` come feature nel dataset di training.

### 3.2 Funding Rate Arbitrage Automatico

Quando il bot ha una predizione ad alta confidenza:
1. **Predizione LONG + funding negativo** → doppio profitto (prezzo sale + incassi funding)
2. **Predizione SHORT + funding positivo** → doppio profitto (prezzo scende + incassi funding)
3. **Predizione neutrale** → delta-neutral tra 2 DEX, incassi solo differenziale funding

**Requisiti tecnici**:
- API per leggere funding rate real-time su almeno 3-4 DEX
- Smart contract interaction via Ledger (SEMPRE hardware wallet)
- Position sizing max 1-5x leva

### 3.3 Fonti Dati Funding Rate

| Fonte | Tipo | URL |
|---|---|---|
| Loris Tools | Storici + live | loris.tools/funding/historical |
| PerpScope | Ranking + confronto | perpscope.com |
| DefiLlama | TVL + volume | defillama.com/perps |
| CoinGlass | CEX funding | coinglass.com/FundingRate |

---

## 4. Sistema AI Monetization — Architettura Proposta

### 4.1 Layer 1: BTC Predictor (esistente)
- Predizione direzione BTC ogni ciclo
- Confidence score → soglia per azione automatica
- Output: LONG / SHORT / NEUTRAL + confidence %

### 4.2 Layer 2: Funding Rate Optimizer (da costruire)
- Input: predizione Layer 1 + funding rate live da N DEX
- Logica: seleziona DEX ottimale dove funding amplifica il profitto
- Output: DEX target + side + size

### 4.3 Layer 3: AI Airdrop Picker (idea nuova)
- Scansione automatica protocolli con airdrop attivi
- Eligibility check per wallet Ledger di Mattia
- Farming ottimale: alloca capitale dove il rapporto points/$ e migliore
- Claim automatico quando disponibile
- **Monetizzazione**: i token droppati vengono o holdati o venduti in base a scoring AI

### 4.4 Layer 4: Execution Engine
- Interazione on-chain SOLO via Ledger
- Multi-chain: Ethereum, Arbitrum, Base, Starknet, ZKsync, Solana
- Risk management: max exposure per protocollo, stop-loss automatici
- Logging completo su Supabase per audit trail

---

## 5. Revenue Streams Identificati

| Stream | Tipo | Stima Potenziale | Automazione |
|---|---|---|---|
| Funding rate arbitrage | Ricorrente | Variabile, 5-20% APY delta-neutral | Alta |
| Airdrop farming | Event-driven | $500-$5,000+ per drop | Media→Alta |
| Predizione + perp trading | Ricorrente | Dipende da WR e sizing | Alta |
| Token staking/LP | Passivo | 3-15% APY | Alta |

---

## 6. Rischi e Mitigazioni

| Rischio | Mitigazione |
|---|---|
| Smart contract exploit | Solo protocolli auditati (Cantina, CertiK, Hacken) |
| Liquidazione | Max 1-5x leva, stop-loss, delta-neutral come default |
| Rug pull / scam | Solo tier 1 VC-backed (Polychain, Variant, Coinbase, Bain) |
| Regulatory | Uso DEX non-custodial, Ledger personale |
| Funding rate reversal | Monitoring continuo, exit automatico se funding flippa |

---

## 7. Prossimi Step Concreti

### Immediato (questa settimana)
- [ ] Claim Paradex $DIME airdrop (entro 2 settimane)
- [ ] Claim Avantis Airdrop 2 (5-8 Mar)
- [ ] Verificare eligibility GRVT Season 1
- [ ] Registrarsi su EdgeX se non fatto (TGE entro fine mese)

### Breve termine (Mar 2026)
- [ ] Aggiungere funding rate come feature XGBoost nel bot
- [ ] Setup API Loris Tools per fetch funding rate automatico
- [ ] Monitorare lancio mainnet Cascade per early farming

### Medio termine (Q2 2026)
- [ ] Sviluppare prototipo AI Airdrop Picker
- [ ] Integrare Funding Rate Optimizer nel bot pipeline
- [ ] Iniziare farming Variational (TGE Q3-Q4)

---

## 8. Tool e Risorse

- **Loris Tools**: loris.tools — funding rate storici e live, heatmap
- **PerpScope**: perpscope.com — ranking DEX, confronto fee e airdrop
- **DefiLlama**: defillama.com/perps — volume per protocollo
- **DropsTab**: dropstab.com — point farming strategies
- **Whales Market**: whales.market — pre-market airdrop trading
- **Stacy Muur Guide**: coinlive.com — 2026 Perp DEX Airdrop Ultimate Guide

---

> Report generato da Claude per BTC Predictor Bot + Astra AI System
> REGOLA AUREA: ogni operazione on-chain SEMPRE via Ledger hardware wallet
