# Compliance Analysis — BTC Prediction Bot
> Creato: 2026-03-01 | Revisione: trimestrale
> Scope: operatività IT/EU | Capitale: $100 USDC | Utenti Telegram: ~20 | Repo: pubblico

---

## EXECUTIVE SUMMARY

Il sistema opera in un'area grigia regolamentare **a basso rischio di enforcement** nella configurazione attuale (piccolo capitale, utenti limitati, nessun pagamento da terzi). Tuttavia **esistono esposizioni legali reali** che vanno indirizzate prima di scalare. Nessun requisito richiede azione immediata urgente, ma alcuni vanno pianificati entro 3-6 mesi.

| Area | Status | Rischio attuale | Azione richiesta |
|------|--------|----------------|-----------------|
| MiFID II / investment advice | 🟡 grigio | BASSO (no pagamenti, no utenti contrattualizzati) | Disclaimer rafforzato |
| VASP / OAM registration | 🟡 grigio | BASSO (no custodia fondi terzi) | Monitoraggio |
| EU AI Act | 🟢 conforme | BASSO (uso interno + educational signals) | Documentazione sistema |
| GDPR | 🟡 parziale | MEDIO (log Supabase, Telegram IDs) | Privacy notice |
| Disclaimer attuale | ⚠️ insufficiente | MEDIO | Upgrade testo |

---

## 1. MiFID II — Servizi di investimento

### Quadro normativo
MiFID II (Direttiva 2014/65/EU, recepita in Italia con D.Lgs. 58/1998 TUF) regola la **consulenza in materia di investimenti**. Crypto asset rientrano parzialmente a seconda dello strumento (BTC futures = strumento finanziario derivato → in scope).

### Analisi applicabilità
Un servizio di segnali crypto è "consulenza" se:
1. **Personalizzata** — raccomandazione diretta a una persona specifica
2. **In cambio di corrispettivo** — pagamento diretto o indiretto
3. **Su strumenti finanziari regolamentati** — BTC futures Kraken = sì

**Configurazione attuale**:
- I segnali Telegram sono pubblici (broadcast), non personalizzati → **esce dal perimetro MiFID II** come consulenza individuale
- Nessun pagamento da utenti → riduce rischio ulteriormente
- Il bot trada per conto proprio (prop trading) → non gestione di portafoglio terzi

**Conclusione**: a oggi la struttura non configura "consulenza in materia di investimenti" ai sensi MiFID II. **Il rischio aumenta significativamente se**: (a) si monetizza il canale, (b) si gestisce capitale altrui, (c) i segnali diventano personalizzati per singoli utenti.

### Azione raccomandata
- Disclaimer esplicito sul canale Telegram: "Segnali pubblici a scopo educativo e informativo. Non costituiscono consulenza finanziaria. Performance passate non garantiscono risultati futuri."
- NON descrivere mai i segnali come "raccomandazioni di acquisto/vendita" nella comunicazione pubblica

---

## 2. VASP / OAM — Virtual Asset Service Provider

### Quadro normativo
D.Lgs. 231/2007 (recepimento AMLD5), aggiornato con D.Lgs. 125/2019: gli operatori in valute virtuali devono iscriversi all'**OAM** (Organismo Agenti e Mediatori) se svolgono attività di:
- Cambio di valute virtuali
- Custodia e amministrazione di valute virtuali per conto terzi
- Partecipazione a servizi di offerta di strumenti finanziari

### Analisi applicabilità
**Non si applicano** all'attuale configurazione perché:
- Il bot trada **esclusivamente il proprio capitale** ($100 USDC propri)
- Nessuna custodia di fondi altrui
- Nessun servizio di cambio per terzi

**Si applicherebbe se**: si accettassero deposit di terzi, si operasse come exchange, si gestisse un fund.

**Conclusione**: registrazione OAM non richiesta nella configurazione attuale.

---

## 3. EU AI Act — Intelligenza Artificiale

### Quadro normativo
EU AI Act (Reg. 2024/1689, applicazione graduale 2025-2026). Sistemi AI in financial services classificati come **"high-risk"** (Annex III, punto 5b) se usati per:
- Credit scoring
- Risk assessment assicurativo
- Decisioni di prestito

**Sistemi "limited risk"** se usano AI per raccomandazioni non vincolanti.

### Analisi applicabilità
Il sistema rientra in **"limited risk"** perché:
- Le decisioni di trading finali sono prese da un sistema automatico propietario (no decisioni su credito/assicurazione)
- I segnali pubblici sono informativi, non vincolanti
- Non c'è interazione diretta utente-AI per decisioni finanziarie individuali (i segnali Telegram non sono chatbot AI verso l'utente finale)

### Obblighi "limited risk" applicabili
- **Trasparenza**: dichiarare che i segnali sono generati da AI — da aggiungere sul canale e sito
- **No manipolazione**: il sistema non usa tecniche subliminali o sfruttamento di vulnerabilità — OK
- **Documentazione interna**: mantenere log del sistema AI (già garantito da Supabase + Sentry + on-chain audit)

**Azione raccomandata**: aggiungere "Segnali generati da sistema AI automatico" nella bio del canale Telegram e nel footer del sito.

---

## 4. GDPR — Protezione dati personali

### Dati trattati
| Dato | Dove | Base giuridica necessaria |
|------|------|--------------------------|
| Telegram user ID (`368092324`) | Hardcoded in app.py/n8n | Legittimo interesse (notifiche operative proprie) |
| Telegram channel member data | Commander bot API | Legittimo interesse (monitoring canale) |
| IP addresses nei log Flask | Railway logs | Legittimo interesse (sicurezza) |
| Timestamp segnali + confidence | Supabase `btc_predictions` | Legittimo interesse (audit trail) |
| On-chain data (direction, pnl) | Polygon blockchain | Pubblico e immutabile — nessun dato personale |

### Rischi
- **Supabase log**: se contiene dati identificativi di terzi (es. IP di chi chiama l'API) → necessaria retention policy
- **Telegram**: i Member IDs sono pseudonimi — basso rischio se non incrociati con altri dati
- **Railway logs**: conservazione automatica, verificare retention period (Railway: 7 giorni default)

### Privacy Notice
Attualmente assente sul sito `btcpredictor.io`. Necessaria se si raccolgono dati da visitatori (Analytics, form, cookie).

**Azione raccomandata**:
1. Aggiungere Privacy Notice minimale su btcpredictor.io (anche solo un anchor nel footer)
2. Verificare retention policy Supabase (truncate logs > 90 giorni)
3. Verificare che Railway non loggi body delle request (potenzialmente contengono BOT_API_KEY in clear)

---

## 5. Disclaimer attuale — Gap analysis

### Stato attuale
Disclaimer presenti in: (da verificare nel codice)
- `index.html` — probabile disclaimer footer
- Canale Telegram — da verificare

### Disclaimer minimo raccomandato

```
⚠️ DISCLAIMER: BTC Predictor è un sistema sperimentale di trading algoritmico
che opera esclusivamente su capitale proprio. I segnali pubblicati hanno scopo
puramente educativo e informativo. Non costituiscono consulenza finanziaria,
raccomandazioni di investimento o sollecitazione al pubblico risparmio ai sensi
del D.Lgs. 58/1998 (TUF) e della Direttiva MiFID II.

Performance passate non garantiscono risultati futuri. Il trading di derivati
crypto comporta rischio di perdita totale del capitale.

Segnali generati da sistema AI automatico (LLM + XGBoost).
Operatore: persona fisica privata. Nessuna autorizzazione Consob/Banca d'Italia.
```

---

## 6. Linea rossa — Cosa NON fare prima di regolarizzare

| Azione | Perché bloccante |
|--------|-----------------|
| Accettare pagamenti per i segnali (subscription) | Configura consulenza finanziaria → MiFID II in pieno |
| Gestire capitale di terzi | VASP + MiFID + AML obbligatori |
| Promettere rendimenti ("guadagna X% al mese") | Violazione TUF art. 21 + Codice del Consumo |
| Raccogliere email/dati utenti senza privacy notice | GDPR violation |
| Scalare a >100 utenti Telegram senza disclaimer | Sollecitazione al pubblico risparmio (soglia critica) |

---

## 7. Roadmap compliance

### Immediato (< 1 settimana) — ZERO costo
- [ ] Aggiungere disclaimer completo nel footer `btcpredictor.io`
- [ ] Aggiungere "Segnali generati da AI automatico" nella bio @BTCPredictorBot
- [ ] Aggiungere disclaimer pin nel canale Telegram

### Breve termine (< 1 mese)
- [ ] Privacy Notice minimale su btcpredictor.io (anche one-pager)
- [ ] Verificare Railway non logga request body con API key
- [ ] Supabase: retention policy 90gg su log interni

### Prima di monetizzare (obbligatorio)
- [ ] Legal opinion da avvocato specializzato fintech/crypto IT
- [ ] Valutare struttura societaria (SRL vs. persona fisica)
- [ ] OAM registration assessment aggiornato
- [ ] Terms of Service formali

### Prima di gestire capitali terzi (mai senza)
- [ ] MiFID II authorization (Consob) oppure strutturazione come "club deal" privato
- [ ] AML policy formale
- [ ] Audit trail completo (già in parte con Polygon — da formalizzare)

---

## 8. Asset difensivi già presenti (punti di forza)

| Asset | Valore legale/reputazionale |
|-------|-----------------------------|
| **On-chain audit trail Polygon** | Prova crittografica che i segnali sono pre-trade, non retrodatati. Irrefutabile. |
| **Repo pubblico GitHub** | Trasparenza tecnica — metodologia verificabile |
| **Sentry error monitoring** | Dimostra diligenza tecnica nella gestione errori |
| **Capital proprio ($100)** | Nessun rischio per terzi nella configurazione attuale |
| **Supabase RLS** | Data governance corretta |

---

*Nota: questo documento è un'analisi tecnico-legale preliminare, non un parere legale professionale. Per decisioni vincolanti (monetizzazione, gestione capitali terzi, >1000 utenti) consultare un avvocato specializzato in diritto finanziario e crypto IT.*

*Prossima revisione: 2026-06-01*
