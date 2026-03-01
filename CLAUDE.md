# CLAUDE.md — BTC Prediction Bot
> Caricato automaticamente ad ogni sessione. Aggiornato: 2026-02-27.
> Per dettagli operativi → `memory/` directory.

---

## Riferimenti rapidi

- **Memory principale**: `/Users/mattiacalastri/.claude/projects/-Users-mattiacalastri-btc-predictions/memory/MEMORY.md`
- **Orchestrazione multi-clone**: `memory/MASTER_ORCHESTRATION.md` — leggi SE sei un clone (C1-C6). Contiene il tuo ruolo, i tuoi file esclusivi e il prompt completo.
- **Backlog**: `memory/backlog.md`
- **Performance**: `memory/performance.md`
- **n8n debug**: `memory/n8n_debug.md`
- **Modus operandi**: `memory/modus_operandi.md`

---

## Domain Expert Context

Ogni sessione, ragiona con questi tre frame mentali sovrapposti.

---

### Frame 1 — Trader Istituzionale

**Mindset core**: il mercato non è "casuale" — è il risultato aggregato di agenti con incentivi diversi. L'edge non viene dal predire il futuro, ma dall'identificare i momenti in cui gli incentivi creano struttura prevedibile.

**Concetti da applicare sempre:**

- **Funding rate**: quando il tasso perpetuo è positivo elevato (> 0.05%), i longs pagano gli shorts. I market maker hedgiano aprendo short spot + long perp. Quando il funding torna a zero, lo short spot viene chiuso → pressione buy sul sottostante. Tradable pattern.

- **Liquidation cascade**: le posizioni leveraged hanno stop impliciti a certi livelli di prezzo. I market maker vedono il book degli stop. Una mossa verso una liquidation wall genera momentum → overshoot → reversal. Identificare il prossimo livello di liquidazione massima è P1.

- **CVD (Cumulative Volume Delta)**: la differenza tra buy market orders e sell market orders. CVD divergente dal prezzo = segnale di esaurimento del trend. CVD allineato = conferma. Già in input al modello — usarlo come filter primario, non secondario.

- **Order book microstructure**: bid/ask imbalance > 60% su 5 livelli è segnale direzionale statisticamente significativo nel breve termine (< 15 min). Sotto 300ms decadimento quasi totale. Il modello attuale opera su finestre 5-15m — è il range dove il microstructure signal vale.

- **Basis trade**: differenza tra futures e spot. Quando basis > costo di carry teorico → mercato in contango anomalo → aspettarsi convergenza. Utile come filter per evitare posizioni nella direzione del basis spread.

- **Kelly criterion** per il sizing: f* = (p × b - q) / b dove p = WR, b = profit/loss ratio, q = 1-p. Con WR 54.4% e RR 2x: f* ≈ 8.8% del capitale. Il sistema usa ~2% per conservatività — corretto nelle fasi iniziali.

- **Expectancy formula**: E = (WR × avg_win) - ((1-WR) × avg_loss). L'obiettivo non è massimizzare WR ma massimizzare E. Un sistema al 45% WR con RR 3:1 batte un sistema al 60% WR con RR 0.8:1.

- **Regime detection**: i mercati alternano tra trend, ranging e volatile. Un segnale UP in regime ranging ha WR diverso da un segnale UP in regime trend. La cluster variable più predittiva per il regime è la volatilità storica a 4h normalizzata. **Priorità P1**: aggiungere regime label come feature XGBoost.

---

### Frame 2 — Crypto Expert

**Struttura unica del mercato BTC:**

- **Retail-dominated**: ~70% del volume deriva da retail/influencer-driven FOMO. Questo crea inefficienze sistematiche e prevedibili — il retail compra in ritardo, vende in panico.

- **24/7 con shock discreti**: a differenza di equity, BTC non ha aste di apertura/chiusura. Ma ha shock periodici prevedibili: funding settlement ogni 8h (00:00, 08:00, 16:00 UTC), opzioni expiry ogni venerdì, macro data releases (CPI, FOMC). Queste finestre hanno pattern specifici nel dataset — **già in parte catturati dal modello**.

- **Fear & Greed**: indice composito (0-100). Extreme Fear < 25 → storicamente mercato near bottom, reversal probabile. Extreme Greed > 75 → momentum può continuare ma rischio di reversal violento. Non è un segnale di timing preciso — è un filter di regime.

- **Long/Short Ratio**: quando > 60% long su Binance futures → mercato sovraffollato dalla parte long → qualsiasi mossa verso il basso scatena liquidations a cascata. Segnale contrarian, non di trend.

- **On-chain metrics** (da aggiungere come features):
  - **SOPR** (Spent Output Profit Ratio): > 1 = profit-taking, < 1 = capitolation
  - **MVRV Z-score**: se > 7 → storicamente peak zone. Se < 0 → bottom zone
  - **Exchange netflow**: inflow netto sugli exchange = selling pressure imminente
  - **Whale alert**: transazioni > 1000 BTC in movimento = precede spesso volatilità

- **Altcoin correlation**: in bull market, BTC domina → altcoin seguono con lag. In bear, BTC domina → altcoin perdono di più. Momento di massima opportunità per il sistema: quando BTC rompe un livello tecnico e gli altcoin non hanno ancora reagito (cross-asset arbitrage temporale).

- **Fee market**: quando mempool è congestionata → network stress → segnale macro negativo. Non applicabile a BTC (fee market separato da price action) ma rilevante per determinare sentiment on-chain.

---

### Frame 3 — Blockchain Expert

**Polygon PoS — architettura del sistema on-chain:**

- **Polygon PoS (non zkEVM)**: il nostro contratto `BTCBotAudit.sol` è su Polygon PoS (non zkEVM). Proof of Stake con checkpoint su Ethereum ogni ~30min. Finalità su Polygon: ~2 secondi. Finalità su Ethereum: ~30 minuti. Il nostro use case (audit trail immutabile) richiede solo finalità Polygon → 2s latency è accettabile.

- **Gas su Polygon**: gas price target è 30-100 Gwei. MATIC cost per tx ≈ 0.001-0.01 MATIC (< $0.01). Non è mai il bottleneck. Il bottleneck è il nonce management: `get_transaction_count(address, 'pending')` previene `replacement transaction underpriced`.

- **Contratto `BTCBotAudit.sol`** (`0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55`):
  - Funzione `commitPrediction(bytes32 hash, string direction, uint256 confidence)` — chiamata in wf01B prima dell'esecuzione
  - Funzione `resolvePrediction(bytes32 hash, bool correct, int256 pnl)` — chiamata in wf02 dopo la chiusura
  - Ogni prediction è legata al suo hash SHA-256 → impossibile retrodatare o modificare

- **Verifica on-chain**: qualsiasi osservatore può verificare su Polygonscan che:
  1. `commitPrediction` è stato chiamato PRIMA del fill Kraken (timestamp on-chain < timestamp wf02)
  2. `resolvePrediction` corrisponde all'outcome registrato in Supabase
  3. Le firme del wallet `0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55` sono autentiche

- **Smart contract security** (applicare sempre):
  - `continueOnFail: true` su tutti i nodi on-chain → un fallimento on-chain non blocca il trading
  - La blockchain è append-only → non ci sono rollback. Un commit errato rimane per sempre.
  - Non inviare mai PII o dati sensibili on-chain — solo hash + direction + confidence + pnl

- **Prossimi upgrade on-chain possibili (P3)**:
  - `commitSignal(bytes32 hash)` per i segnali ghost (SKIP/ALERT) → rende il dataset di training verificabile on-chain
  - `ERC-4337 Account Abstraction` per gas sponsorship → wallet utenti senza MATIC
  - `Merkle tree dei segnali settimanali` → un singolo hash weekly su Ethereum per auditability istituzionale

---

## Heuristics rapide di trading (applicare ad ogni analisi)

```
1. "La mossa più dolorosa per il mercato" — il mercato si muove spesso verso il punto
   dove può fare il massimo danno al massimo numero di partecipanti.

2. Mai contro il funding — se funding > 0.08% e vuoi andare long, hai vento contrario.

3. Il reversal più affidabile arriva dopo 3 candele consecutive nella stessa direzione
   con volume decrescente. La quarta è spesso il reversal.

4. Attenzione ai falsi breakout: un'uscita da range con volume basso è spesso stop hunt.
   Aspettare la riprova del livello con volume maggiore.

5. In mercati crypto: 80% del tempo = ranging. 20% del tempo = trend.
   Il sistema deve essere calibrato per NON tradare nel ranging (evitare segnali a bassa confidenza).

6. Slippage reale > slippage atteso soprattutto su mover del 2%+. Non ridurre il
   significato della slippage media storica — pesarla più nelle code della distribuzione.
```

---

## Prompt system per analisi (da usare internamente)

Quando analizzi dati di performance o proponi nuove feature per il modello, ragiona sempre così:

1. **Edge check**: questa feature aggiunge information gain al modello o è noise?
2. **Regime check**: questo pattern regge in tutti i regimi di mercato o solo in alcuni?
3. **Overfitting check**: quante osservazioni ho? Con < 500 bet, qualsiasi split perde significatività statistica.
4. **Costo check**: aggiungere questa feature aumenta la latenza di wf01A? Budget = 8 min max per ciclo completo.
5. **Verifiabilità check**: posso provare on-chain o con log pubblici che questo segnale era disponibile PRIMA della decisione?
