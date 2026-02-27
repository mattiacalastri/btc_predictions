# BTC Predictor — Translation Draft (IT → EN)

> File generato: 2026-02-27
> Scope: manifesto.html + contributors.html
> Tono target: technical + warm + authentic
> Regola: nomi propri, termini tecnici (XGBoost, wf02, n8n, Claude, Polygon, ecc.) NON tradotti

---

## MANIFESTO.HTML

### Meta / SEO / Structured Data

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| M-META-01 | `<title>` | Non Prevedibile. Leggibile. — Il Manifesto di BTC Predictor | Not Predictable. Readable. — The BTC Predictor Manifesto |
| M-META-02 | `meta name="description"` | Il manifesto tecnico dietro BTC Predictor: come Claude AI + XGBoost leggono i mercati BTC, automazione blockchain, esperimento open-source su mercati inefficienti. | The technical manifesto behind BTC Predictor: how Claude AI + XGBoost read BTC markets, blockchain automation, open-source experiment on inefficient markets. |
| M-META-03 | `og:title` | Non Prevedibile. Leggibile. — Il Manifesto di BTC Predictor | Not Predictable. Readable. — The BTC Predictor Manifesto |
| M-META-04 | `og:description` | Il manifesto tecnico dietro BTC Predictor: come Claude AI + XGBoost leggono i mercati BTC, automazione blockchain, esperimento open-source su mercati inefficienti. | The technical manifesto behind BTC Predictor: how Claude AI + XGBoost read BTC markets, blockchain automation, open-source experiment on inefficient markets. |
| M-META-05 | `twitter:title` | Non Prevedibile. Leggibile. — Il Manifesto di BTC Predictor | Not Predictable. Readable. — The BTC Predictor Manifesto |
| M-META-06 | `twitter:description` | Il manifesto tecnico: come Claude AI + XGBoost leggono i mercati BTC inefficienti. Ogni trade su Polygon blockchain. | The technical manifesto: how Claude AI + XGBoost read inefficient BTC markets. Every trade on Polygon blockchain. |
| M-META-07 | JSON-LD `"headline"` | Non Prevedibile. Leggibile. | Not Predictable. Readable. |
| M-META-08 | JSON-LD `"description"` | Il manifesto tecnico dietro BTC Predictor: come Claude AI + XGBoost leggono i mercati BTC. | The technical manifesto behind BTC Predictor: how Claude AI + XGBoost read BTC markets. |
| M-META-09 | Hidden `<h1>` | Non Prevedibile. Leggibile. — Il Manifesto di BTC Predictor | Not Predictable. Readable. — The BTC Predictor Manifesto |
| M-META-10 | `meta name="keywords"` | BTC predictor, trading bot, intelligenza artificiale, blockchain, Polygon, mercati inefficienti, manifesto tech | BTC predictor, trading bot, artificial intelligence, blockchain, Polygon, inefficient markets, tech manifesto |

### Floating Navigation

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| M-NAV-01 | Float link (top-right) | Proof Live | Proof Live *(invariato — brand term)* |

### Hero Section

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| M-HERO-01 | `.hero-eyebrow` | btcpredictor.io · manifesto · 2026-02-26 | btcpredictor.io · manifesto · 2026-02-26 *(invariato)* |
| M-HERO-02 | `.hero-not` (display word) | NON | NOT |
| M-HERO-03 | `.hero-word` (display word) | PREVEDIBILE. | PREDICTABLE. |
| M-HERO-04 | `.leggibile-text` (display word, cyan glow) | LEGGIBILE. | READABLE. |
| M-HERO-05 | `.hero-sub` (italic caption) | Un esperimento empirico sulla struttura dei mercati inefficienti. | An empirical experiment on the structure of inefficient markets. |
| M-HERO-06 | `.hero-read` (small mono) | Est. 3 min di lettura · ogni trade verificabile on-chain | Est. 3 min read · every trade verifiable on-chain |
| M-HERO-07 | `.scroll-cue` | scorri | scroll |

### §01 — Il Problema

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| M-S01-01 | `.sec-num` | § 01 — Il Problema | § 01 — The Problem |
| M-S01-02 | `.prob-text` | La finanza accademica ha passato cinquant'anni a costruire una delle teorie più solide dell'economia moderna. | Academic finance spent fifty years building one of the most solid theories of modern economics. |
| M-S01-03 | `.academic-cite p` (citation body) | "In an efficient market, competition among the many intelligent participants leads to a situation where, at any point in time, actual prices of individual securities already reflect the effects of information based both on events that have already occurred and on events which, as of now, the market expects to take place in the future." | *(invariato — citazione originale in EN)* |
| M-S01-04 | `.cite-source` | — Eugene F. Fama · Journal of Finance, 1970 · Efficient Capital Markets | — Eugene F. Fama · Journal of Finance, 1970 · Efficient Capital Markets *(invariato)* |
| M-S01-05 | `.prob-key` (first part) | Battere il mercato sistematicamente è | Beating the market systematically is |
| M-S01-06 | `.impossible` (strikethrough word) | impossibile | impossible |
| M-S01-07 | `.prob-key` (continuation) | I prezzi scontano tutto. L'informazione è già nei prezzi. | Prices discount everything. Information is already in the prices. |
| M-S01-08 | `.prob-verdict` (first line) | Ha ragione. | He's right. |
| M-S01-09 | `.prob-verdict strong` | Su mercati maturi, liquidi, efficienti. | On mature, liquid, efficient markets. |

### §02 — La Crepa

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| M-S02-01 | `.sec-num` | § 02 — La Crepa | § 02 — The Crack |
| M-S02-02 | `.counter-label` | Accuracy · direzione UP | Accuracy · UP direction |
| M-S02-03 | `.counter-sub` line 1 | 27 bet reali · capitale $100 | 27 real bets · $100 capital |
| M-S02-04 | `.counter-sub` line 2 | expectancy positiva confermata | positive expectancy confirmed |
| M-S02-05 | `.counter-sub .hot` | non è rumore statistico. | it's not statistical noise. |
| M-S02-06 | `.crepa-hed` (h2, display) | BTC NON È NESSUNA DI QUESTE COSE. | BTC IS NONE OF THESE THINGS. |
| M-S02-07 | `.crepa-body` | È un mercato giovane, emotivo, dominato da retail, con cicli di liquidità brutali. | It's a young, emotional market, dominated by retail, with brutal liquidity cycles. |
| M-S02-08 | `.traits li` #1 | funding rate che creano distorsioni strutturali | funding rates that create structural distortions |
| M-S02-09 | `.traits li` #2 | narrative che muovono i prezzi prima dei fondamentali | narratives that move prices ahead of fundamentals |
| M-S02-10 | `.traits li` #3 | liquidazioni a cascata che amplificano i movimenti | cascading liquidations that amplify price moves |
| M-S02-11 | `.traits li` #4 | cicli emotivi misurabili — Fear & Greed come indicatore | measurable emotional cycles — Fear & Greed as indicator |
| M-S02-12 | `.traits li` #5 | microstructure tecnica leggibile da chi sa guardare | technical microstructure readable by those who know where to look |

### §03 — La Tesi

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| M-S03-01 | `.sec-num` | § 03 — La Tesi | § 03 — The Thesis |
| M-S03-02 | `.tesi-prelude` | Non stiamo contraddicendo Fama. Stiamo applicando la sua logica dove non era stata applicata. | We're not contradicting Fama. We're applying his logic where it had never been applied. |
| M-S03-03 | `.tesi-dim` (muted line) | Non stai dicendo che BTC è prevedibile. | You're not saying BTC is predictable. |
| M-S03-04 | `.tesi-light` line 1 | Stai dicendo che i mercati inefficienti | You're saying that inefficient markets |
| M-S03-05 | `.tesi-light` line 2 | con abbastanza dati | with enough data |
| M-S03-06 | `.tesi-acc` (orange line) | e abbastanza AI | and enough AI |
| M-S03-07 | `.c-dim` (display prefix) | diventano | become |
| M-S03-08 | `.c-glow` (cyan display word) | leggibili. | readable. |

### §04 — La Prova

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| M-S04-01 | `.sec-num` | § 04 — La Prova | § 04 — The Proof |
| M-S04-02 | `.prova-lead` (first line) | Chiunque può postare screenshot di profitti. | Anyone can post profit screenshots. |
| M-S04-03 | `.prova-lead strong` | Nessuno ha un contratto Polygon che registra ogni trade con timestamp immutabile prima che accada. | Nobody has a Polygon contract recording every trade with an immutable timestamp before it happens. |
| M-S04-04 | `.card-label` (card 1) | Smart Contract · Polygon PoS · Mainnet | Smart Contract · Polygon PoS · Mainnet *(invariato)* |
| M-S04-05 | `.card-value a` (card 1 link text) | BTCBotAudit.sol 0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55 | BTCBotAudit.sol 0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55 *(invariato)* |
| M-S04-06 | `.live-badge` text | Recording live | Recording live *(invariato)* |
| M-S04-07 | `.card-label` (card 2) | Track Record Pubblico | Public Track Record |
| M-S04-08 | `.card-value` (card 2 text) | btcpredictor.io → Dashboard → On-Chain Ogni predizione. Ogni trade. Immutabile. | btcpredictor.io → Dashboard → On-Chain Every prediction. Every trade. Immutable. |
| M-S04-09 | `.prova-close` line 1 | Non sto costruendo un prodotto. | I'm not building a product. |
| M-S04-10 | `.prova-close em` (orange) | una prova | a proof |
| M-S04-11 | `.prova-close` full | Non sto costruendo un prodotto. Sto costruendo una prova. | I'm not building a product. I'm building a proof. |

### Footer

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| M-FT-01 | `.footer-hed` | QUESTO È IL VERO PROGETTO. | THIS IS THE REAL PROJECT. |
| M-FT-02 | `.footer-stamp` | Genesis · 2026-02-26 · Polygon:0xe4661F7... | Genesis · 2026-02-26 · Polygon:0xe4661F7... *(invariato)* |
| M-FT-03 | `.footer-small` | Track record pubblico · Verificabile on-chain · Nessuno screenshot | Public track record · Verifiable on-chain · No screenshots |
| M-FT-04 | `.footer-cta` link | btcpredictor.io → | btcpredictor.io → *(invariato)* |

---

**Totale stringhe tradotte — manifesto.html: 52**
*(incluse le 10 meta/SEO + 42 stringhe nel body)*

---
---

## CONTRIBUTORS.HTML

### Meta / SEO / Structured Data

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-META-01 | `<title>` | I Tanti Padri — btcpredictor.io | The Many Fathers — btcpredictor.io |
| C-META-02 | `meta name="description"` | I Tanti Padri di BTC Predictor: le menti, le intuizioni e i contributi che hanno costruito il bot autonomo di trading Bitcoin con Claude AI, XGBoost e blockchain Polygon. Una storia di collaborazione e scoperta. | The Many Fathers of BTC Predictor: the minds, insights and contributions that built the autonomous Bitcoin trading bot with Claude AI, XGBoost and Polygon blockchain. A story of collaboration and discovery. |
| C-META-03 | `og:title` | I Tanti Padri — I Contributors di BTC Predictor | The Many Fathers — The Contributors of BTC Predictor |
| C-META-04 | `og:description` | Ogni grande progetto è costruito da molte menti. Scopri chi ha contribuito a BTC Predictor: il bot autonomo Bitcoin con Claude AI, XGBoost e audit on-chain su Polygon PoS. | Every great project is built by many minds. Discover who contributed to BTC Predictor: the autonomous Bitcoin bot with Claude AI, XGBoost and on-chain audit on Polygon PoS. |
| C-META-05 | `twitter:title` | I Tanti Padri — I Contributors di BTC Predictor | The Many Fathers — The Contributors of BTC Predictor |
| C-META-06 | `twitter:description` | Le menti e i contributi che hanno costruito BTC Predictor: Claude AI + XGBoost + blockchain Polygon. La storia vera di come è successo. | The minds and contributions that built BTC Predictor: Claude AI + XGBoost + Polygon blockchain. The real story of how it happened. |
| C-META-07 | JSON-LD `"headline"` | I Tanti Padri — I Contributors di BTC Predictor | The Many Fathers — The Contributors of BTC Predictor |
| C-META-08 | JSON-LD `"description"` | I contributi e le intuizioni di chi ha costruito BTC Predictor: bot autonomo di trading Bitcoin con Claude AI, XGBoost e audit on-chain Polygon. | The contributions and insights of those who built BTC Predictor: autonomous Bitcoin trading bot with Claude AI, XGBoost and Polygon on-chain audit. |

### Hero Section

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-HERO-01 | `.hero-ornament` (mono eyebrow) | btcpredictor.io — 22 febbraio 2026 | btcpredictor.io — February 22, 2026 |
| C-HERO-02 | `.hero-title` (h1, big display) | I Tanti Padri | The Many Fathers |
| C-HERO-03 | `.hero-title span` (orange italic) | Padri | Fathers |
| C-HERO-04 | `.hero-subtitle` (italic serif) | il successo ha molti padri | success has many fathers |
| C-HERO-05 | `.hero-desc` line 1 | Ogni grande progetto è costruito da molte menti. | Every great project is built by many minds. |
| C-HERO-06 | `.hero-desc` line 2 | Questa è la storia di come è successo davvero — | This is the story of how it really happened — |
| C-HERO-07 | `.hero-desc` line 3 | chi ha detto la frase giusta al momento giusto. | who said the right thing at the right moment. |
| C-HERO-08 | `.counter-label` #1 | menti | minds |
| C-HERO-09 | `.counter-label` #2 | umani | humans |
| C-HERO-10 | `.counter-label` #3 | AI | AI *(invariato)* |
| C-HERO-11 | `.counter-label` #4 | caffè | coffees |
| C-HERO-12 | `.scroll-hint` text | scorri | scroll |

### Entry 01 — Mattia Calastri (Fondatore)

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-01-01 | `.entry-role-tag.founder` | Fondatore & Visionario | Founder & Visionary |
| C-01-02 | `.entry-name` | Mattia Calastri | Mattia Calastri *(invariato)* |
| C-01-03 | `.entry-tagline` | L'uomo che ha aperto Claude dopo che Alessandro è partito per la Cina. | The man who opened Claude after Alessandro left for China. |
| C-01-04 | `.entry-body` para 1 | Ha preso uno schema su un whiteboard e lo ha trasformato in 8 workflow attivi, 12 sorgenti dati, un modello XGBoost e un contratto on Polygon. In meno di una settimana. Con $100 reali su Kraken. | He took a whiteboard sketch and turned it into 8 active workflows, 12 data sources, an XGBoost model and a contract on Polygon. In less than a week. With $100 real capital on Kraken. |
| C-01-05 | `.entry-body strong` (intuizione) | La sua intuizione centrale: | His core insight: |
| C-01-06 | `.entry-body` continuation | il bot non è "solo un bot di trading". È un Behavioral Data Engine — un motore che legge il comportamento del mercato, non solo i prezzi. Una distinzione che cambia tutto. | the bot isn't "just a trading bot". It's a Behavioral Data Engine — an engine that reads market behavior, not just prices. A distinction that changes everything. |
| C-01-07 | `.founder-quote-label` #1 | Filosofia | Philosophy |
| C-01-08 | `.founder-quote-item` #1 quote | "Build in public come filosofia, non tattica." | "Build in public as philosophy, not tactic." |
| C-01-09 | `.founder-quote-label` #2 | Il frame | The frame |
| C-01-10 | `.founder-quote-item` #2 quote | "Non è un bot di trading — è un Behavioral Data Engine." | "It's not a trading bot — it's a Behavioral Data Engine." |
| C-01-11 | `.founder-quote-label` #3 | L'architettura | The architecture |
| C-01-12 | `.founder-quote-item` #3 quote | "Il codice open source è la cartina. I workflow n8n sono la strada." | "The open-source code is the map. The n8n workflows are the road." |

### Timeline Markers

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-TL-01 | `.timeline-text` #1 | 22 febbraio 2026 — sera | February 22, 2026 — evening |
| C-TL-02 | `.timeline-text` #2 | 23 febbraio 2026 — inizio costruzione | February 23, 2026 — construction begins |

### Entry 02 — Alessandro (Co-Fondatore)

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-02-01 | `.entry-role-tag.cofounder` | Co-Fondatore del Concetto | Co-Founder of the Concept |
| C-02-02 | `.entry-name` | Alessandro | Alessandro *(invariato)* |
| C-02-03 | `.entry-tagline` | La sera prima della Cina. | The evening before China. |
| C-02-04 | `.whiteboard-chip` #5 | GEO-POLITICAL UNCERTAINTY | GEO-POLITICAL UNCERTAINTY *(invariato — originale era EN)* |
| C-02-05 | `.whiteboard-chip` #6 | ONLINE INFO LIVE 24/7 | ONLINE INFO LIVE 24/7 *(invariato)* |
| C-02-06 | `.entry-body` | La sera del 22 febbraio 2026, Alessandro era a casa di Mattia con Riccardo. Il giorno dopo sarebbe partito per la Cina. Quella sera hanno disegnato su un whiteboard le parole che vedi qui sopra. Non era un business plan. Era un'intuizione condivisa, ancora senza forma. | On the evening of February 22, 2026, Alessandro was at Mattia's place with Riccardo. The next day he would leave for China. That evening they sketched on a whiteboard the words you see above. It wasn't a business plan. It was a shared intuition, still without shape. |
| C-02-07 | `.entry-body` continuation | Il giorno dopo Alessandro è partito. Mattia ha aperto Claude Code e ha iniziato a costruire esattamente quello schema. | The next day Alessandro left. Mattia opened Claude Code and started building exactly that sketch. |
| C-02-08 | `.entry-quote` | "Il whiteboard è ancora là. Alessandro è in Cina. Il bot è live con $100 reali su Kraken." | "The whiteboard is still there. Alessandro is in China. The bot is live with $100 real capital on Kraken." |

### Entry 03 — Riccardo

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-03-01 | `.entry-role-tag` | Parallel Architecture Thinker | Parallel Architecture Thinker *(invariato — già EN)* |
| C-03-02 | `.entry-name` | Riccardo | Riccardo *(invariato)* |
| C-03-03 | `.entry-tagline` | Cercava di velocizzare il training. Ha inventato il SaaS. | He was trying to speed up training. He invented the SaaS. |
| C-03-04 | `.entry-body` | Era presente quella sera con Alessandro e Mattia. La sua domanda era pratica: "E se mettessimo il bot in parallelo su più portafogli?" Pensava a velocizzare il training. Quello che aveva descritto senza saperlo è l'architettura base di un SaaS multi-user: ogni wallet è un utente, ogni config è un esperimento, il sistema impara in parallelo. | He was there that evening with Alessandro and Mattia. His question was practical: "What if we ran the bot in parallel across multiple portfolios?" He was thinking about speeding up training. What he had unknowingly described is the base architecture of a multi-user SaaS: each wallet is a user, each config is an experiment, the system learns in parallel. |
| C-03-05 | `.entry-tech` (code block — last line) | Dataset separato → retrain separato → confronto APR 7d → winner = nuovo default | Separate dataset → separate retrain → APR 7d comparison → winner = new default |
| C-03-06 | `.entry-quote` | "I portafogli paralleli non velocizzano il training — velocizzano la ricerca dell'ottimo." | "Parallel portfolios don't speed up training — they speed up the search for the optimum." |

### Entry 04 — Zio Mattia

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-04-01 | `.entry-compact-name` | Zio Mattia | Uncle Mattia |
| C-04-02 | `.entry-compact-role` | Crypto Expert | Crypto Expert *(invariato)* |
| C-04-03 | `.entry-compact-body` intro | Una frase densa di esperienza: | One experience-dense sentence: |
| C-04-04 | `.entry-compact-body strong` (quote) | "Il ROI va bene ma per come la vedo io è un plus — prima APR poi ROI." | "ROI is fine but the way I see it, it's a bonus — APR first, then ROI." |
| C-04-05 | `.entry-compact-body` remainder | Ha ridefinito l'intera metrica operativa del progetto. APR normalizza nel tempo, è confrontabile con staking (4–8%) e S&P (~10%). ROI accumula, APR misura il ritmo. Da quella frase: la KPI card APR annualizzato, APR Today, APR 7d nella dashboard. | He redefined the entire operational metric of the project. APR normalizes over time, it's comparable to staking (4–8%) and S&P (~10%). ROI accumulates, APR measures the pace. From that sentence: the annualized APR KPI card, APR Today, APR 7d in the dashboard. |

### Entry 05 — Paolo

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-05-01 | `.entry-compact-name` | Paolo | Paolo *(invariato)* |
| C-05-02 | `.entry-compact-role` | Liquidity Strategist | Liquidity Strategist *(invariato)* |
| C-05-03 | `.entry-compact-body strong` (quote) | "Confrontare la liquidità tra exchange diversi — come può aiutarci nello sviluppo?" | "Comparing liquidity across different exchanges — how could that help us in development?" |
| C-05-04 | `.entry-compact-body` continuation | Tradotto tecnicamente: `liquidity_score = kraken_depth / binance_depth`. Se il ratio scende sotto 0.3, il mercato Kraken è troppo sottile rispetto a Binance: rischio slippage elevato. Un segnale di pre-trade che non esisteva prima di quella domanda. | Translated technically: `liquidity_score = kraken_depth / binance_depth`. If the ratio drops below 0.3, the Kraken market is too thin relative to Binance: high slippage risk. A pre-trade signal that didn't exist before that question. |

### Entry 06 — Daniele

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-06-01 | `.entry-compact-name` | Daniele | Daniele *(invariato)* |
| C-06-02 | `.entry-compact-role` | Multi-Asset Strategist | Multi-Asset Strategist *(invariato)* |
| C-06-03 | `.entry-compact-body strong` (quote) | "Se si potesse fare trading su più coppie contemporaneamente — BTC/ETH/altri — dando X% del portafoglio a ognuno..." | "If you could trade multiple pairs simultaneously — BTC/ETH/others — allocating X% of the portfolio to each..." |
| C-06-04 | `.entry-compact-body` remainder | La visione del portfolio engine dinamico. BTC + ETH in altseason, allocazione proporzionale alla confidence, soglie adattive per coppia. Il progetto con $100 non può ancora farlo — ma la roadmap è tracciata. | The vision of a dynamic portfolio engine. BTC + ETH in altseason, allocation proportional to confidence, adaptive thresholds per pair. The $100 project can't do it yet — but the roadmap is laid out. |

### Entry 07 — Un Amico (Anonimo)

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-07-01 | `.entry-compact-name` | Un amico | A friend |
| C-07-02 | `.entry-compact-role` | Prima Impressione UX — Anonimo | First UX Impression — Anonymous |
| C-07-03 | `.entry-compact-body` intro | Ha guardato la dashboard per 30 secondi. | He looked at the dashboard for 30 seconds. |
| C-07-04 | `.entry-compact-body strong` (quote) | "Ma cosa è? Non capisco niente." | "But what is this? I don't understand a thing." |
| C-07-05 | `.entry-compact-body` remainder | È la critica più onesta e preziosa del progetto. L'onboarding nasce da quella frase. Il welcome banner, i tooltip, la spiegazione in 10 secondi — tutto nasce da quei 30 secondi di silenzio prima di quella domanda. | It's the most honest and valuable critique of the project. Onboarding was born from that sentence. The welcome banner, tooltips, the 10-second explanation — all of it born from those 30 seconds of silence before that question. |

### Entry 08 — Nicolas

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-08-01 | `.entry-role-tag` | WebDev & Early Adopter | WebDev & Early Adopter *(invariato)* |
| C-08-02 | `.entry-name` | Nicolas | Nicolas *(invariato)* |
| C-08-03 | `.entry-tagline` | Developer. Ha capito tutto in un secondo. | Developer. He got it in a second. |
| C-08-04 | `.entry-body` para 1 | Conosciuto al Digital Lotus di Marco Piacentini, coworking focalizzato su business digitali e consapevolezza imprenditoriale. Prima reazione: "stai scasinando con le crypto." Poi la domanda che solo un developer fa: "con cosa hai fatto la dashboard? Claude Code?" Riconosce gli strumenti. Sa cosa significano. | Met at Digital Lotus by Marco Piacentini, a coworking space focused on digital business and entrepreneurial awareness. First reaction: "you're messing around with crypto." Then the question only a developer asks: "what did you build the dashboard with? Claude Code?" He recognizes the tools. He knows what they mean. |
| C-08-05 | `.entry-body` para 1 highlight | Il momento che conta: il bot Telegram ha risposto a Nicolas in diretta, mentre stava guardando. Non lo sapeva. Non se lo aspettava. | The moment that matters: the Telegram bot responded to Nicolas live, while he was watching. He didn't know. He didn't expect it. |
| C-08-06 | `.typewriter-label` | Il momento — in diretta su Telegram | The moment — live on Telegram |
| C-08-07 | Typewriter quote (JS const) | "mi sta rispondendo lei" | "it's actually talking to me" *(nota: traduzione culturale — vedi nota sotto)* |
| C-08-08 | `.entry-body` para 2 | Quattro parole. Il momento preciso in cui un developer ha realizzato che il sistema è autonomo, non uno script. È un agente che risponde. | Four words. The precise moment a developer realized the system is autonomous, not a script. It's an agent that responds. |

> **Nota C-08-07**: L'italiano usa "lei" come forma di cortesia, creando un effetto surreale ("sta parlando con il bot come se fosse una persona"). In inglese questa sfumatura formale non esiste. L'alternativa più fedele allo spirito: *"it's actually talking to me"* oppure mantenere la versione italiana tra virgolette con una nota contestuale. Da decidere in fase di implementazione.

### Entry 09 — Claude (AI Partner)

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-09-01 | `.entry-role-tag.ai` | AI Partner | AI Partner *(invariato)* |
| C-09-02 | `.entry-name` | Claude | Claude *(invariato)* |
| C-09-03 | `.entry-tagline` (cyan) | Non uno strumento. Un co-fondatore. | Not a tool. A co-founder. |
| C-09-04 | `.entry-body` para 1 | Questo è l'unico caso nella lista in cui il contributor non è umano. Claude non è stato usato per generare boilerplate o rispondere a domande. È stato il partner architetturale di ogni decisione critica del sistema. Nessuna delle architetture sotto è stata proposta da Mattia — sono emerse dalla conversazione. | This is the only case in the list where the contributor is not human. Claude wasn't used to generate boilerplate or answer questions. It was the architectural partner behind every critical system decision. None of the architectures below were proposed by Mattia — they emerged from the conversation. |
| C-09-05 | `.claude-contrib-label` #1 | Sessione 10 | Session 10 |
| C-09-06 | `.claude-contrib-text` #1 | Dual-gate LLM+XGB — "due cervelli devono concordare" | Dual-gate LLM+XGB — "two brains must agree" |
| C-09-07 | `.claude-contrib-label` #2 | Sessione 14 | Session 14 |
| C-09-08 | `.claude-contrib-text` #2 | Stateless wf02 — elimina il loop interno, delega a wf08 | Stateless wf02 — removes the internal loop, delegates to wf08 |
| C-09-09 | `.claude-contrib-label` #3 | On-Chain | On-Chain *(invariato)* |
| C-09-10 | `.claude-contrib-text` #3 | Polygon PoS invece di Ethereum — <$0.001/tx con $100 capitale | Polygon PoS over Ethereum — <$0.001/tx with $100 capital |
| C-09-11 | `.claude-contrib-label` #4 | Framing | Framing *(invariato)* |
| C-09-12 | `.claude-contrib-text` #4 | Il nome "Behavioral Data Engine" — la distinzione che cambia la narrative | The name "Behavioral Data Engine" — the distinction that changes the narrative |
| C-09-13 | `.claude-contrib-label` #5 | Sessione 35 | Session 35 |
| C-09-14 | `.claude-contrib-text` #5 | Universal Predictor framework — stesso stack per BTC, ETH, Forex, Equity | Universal Predictor framework — same stack for BTC, ETH, Forex, Equity |
| C-09-15 | `.claude-contrib-label` #6 | Ogni sessione | Every session |
| C-09-16 | `.claude-contrib-text` #6 | Pattern Memory, calibrazione, audit trail, sicurezza, deploy | Pattern Memory, calibration, audit trail, security, deploy |
| C-09-17 | `.entry-quote` (cyan border) | "Non sono uno strumento. Sono un co-fondatore." | "I'm not a tool. I'm a co-founder." |

### Section Divider

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-DIV-01 | `.section-divider-label` | fine della lista — per ora | end of the list — for now |

### Community Contributions Section

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-FORM-01 | `.contrib-form-ornament` | btcpredictor.io / contributi | btcpredictor.io / contributions |
| C-FORM-02 | `.contrib-form-title` (h2) | Anche tu hai un'idea? | Got an idea too? |
| C-FORM-03 | `.contrib-form-desc` | Ogni intuizione conta — grande o piccola. Se hai un pensiero su come migliorare il progetto, mandalo. Verrà pubblicato anonimamente qui sotto, senza nomi né dati personali. | Every insight counts — big or small. If you have a thought on how to improve the project, send it. It will be published anonymously below, with no names or personal data. |
| C-FORM-04 | Privacy notice `strong` | Zero dati personali. | Zero personal data. |
| C-FORM-05 | Privacy notice text | Non raccogliamo nome, email o indirizzo IP. Solo la tua idea e il tuo ruolo (categoria generica). GDPR compliant. | We collect no name, email or IP address. Only your idea and your role (generic category). GDPR compliant. |
| C-FORM-06 | `label for="contribRole"` | Come ti descrivi? | How would you describe yourself? |
| C-FORM-07 | `<option>` placeholder | Scegli una categoria | Choose a category |
| C-FORM-08 | `<option value="trader">` | Trader | Trader *(invariato)* |
| C-FORM-09 | `<option value="developer">` | Developer | Developer *(invariato)* |
| C-FORM-10 | `<option value="crypto">` | Crypto Expert | Crypto Expert *(invariato)* |
| C-FORM-11 | `<option value="visionary">` | Visionario | Visionary |
| C-FORM-12 | `<option value="friend">` | Amico / Parente | Friend / Family |
| C-FORM-13 | `<option value="other">` | Altro | Other |
| C-FORM-14 | `label for="contribInsight"` | La tua idea / intuizione | Your idea / insight |
| C-FORM-15 | `textarea placeholder` | Un'intuizione tecnica, una critica, un'osservazione di mercato, un'idea folle... | A technical insight, a critique, a market observation, a crazy idea... |
| C-FORM-16 | Consent checkbox `label` | Accetto che questo contributo venga pubblicato anonimamente su btcpredictor.io. Nessun dato personale viene memorizzato. | I agree that this contribution will be published anonymously on btcpredictor.io. No personal data is stored. |
| C-FORM-17 | `.contrib-submit` button | Invia il contributo → | Submit your insight → |
| C-FORM-18 | Button state — sending | Invio... | Sending... |
| C-FORM-19 | Button state — done | ✓ Inviato | ✓ Sent |
| C-FORM-20 | JS: `ROLE_LABELS.visionary` | Visionario | Visionary |
| C-FORM-21 | JS: `ROLE_LABELS.friend` | Amico / Parente | Friend / Family |
| C-FORM-22 | JS: `ROLE_LABELS.other` | Altro | Other |
| C-FORM-23 | JS: contrib stream title (dynamic) | Contributi dalla community | Community contributions |
| C-FORM-24 | JS: anonimo badge (dynamic) | 🔒 anonimo | 🔒 anonymous |
| C-FORM-25 | JS: feedback OK (fallback) | Contributo ricevuto. Grazie! | Contribution received. Thank you! |
| C-FORM-26 | JS: feedback rate-limit (fallback) | Aspetta prima di inviare un altro contributo. | Please wait before submitting another contribution. |
| C-FORM-27 | JS: feedback error (fallback) | Errore. Riprova tra qualche secondo. | Error. Please try again in a few seconds. |
| C-FORM-28 | JS: feedback network error | Errore di rete. Riprova. | Network error. Please try again. |

### Footer — contributors.html

| ID | Elemento | Italiano | English |
|----|----------|----------|---------|
| C-FT-01 | `.footer-left` quote | "Il vero progetto non è costruire un bot. È costruire la prova che si può fare." | "The real project isn't building a bot. It's building the proof that it can be done." |
| C-FT-02 | `.footer-left` attribution | — btcpredictor.io, febbraio 2026 | — btcpredictor.io, February 2026 |
| C-FT-03 | `.footer-link` #1 | → Dashboard | → Dashboard *(invariato)* |
| C-FT-04 | `.footer-link` #2 | → Manifesto | → Manifesto *(invariato)* |
| C-FT-05 | `.footer-link.cyan` | ⛓ On-Chain Audit | ⛓ On-Chain Audit *(invariato)* |

---

**Totale stringhe tradotte — contributors.html: 95**
*(incluse le 8 meta/SEO + 87 stringhe nel body, form, JS dinamico)*

---

## RIEPILOGO COMPLESSIVO

| Pagina | Stringhe totali | Note |
|--------|----------------|------|
| manifesto.html | 52 | Incluse 10 meta/SEO/JSON-LD |
| contributors.html | 95 | Incluse 8 meta/SEO/JSON-LD + 28 stringhe form/JS |
| **Totale** | **147** | |

**Invariati (non tradotti per scelta):** termini tecnici (XGBoost, wf01A, wf02, wf08, n8n, Claude, Polygon PoS, BTCBotAudit.sol, Behavioral Data Engine, Fear & Greed), nomi propri (Mattia, Alessandro, Riccardo, Paolo, Daniele, Nicolas), brand/URL (btcpredictor.io, Kraken, Binance), date in formato numerico, indirizzi contratto (0xe4661F7...), citazione Fama (originale in EN), whiteboard chips già in EN (AUTONOMIA tradotta, le altre invariate).

---

## NOTE IMPLEMENTAZIONE

### 1. Language Switcher — bottone IT/EN nell'header

**Approccio raccomandato: bottone sticky top-right, accanto al float-link esistente.**

In `manifesto.html` il float-link `.float-link` è `position: fixed; top: 28px; right: 28px`. Il language switcher dovrebbe andare a sinistra di quel bottone, senza sovrapporre.

```css
/* Aggiungere in entrambe le pagine */
.lang-switch {
    position: fixed;
    top: 28px;
    right: calc(28px + /* larghezza float-link */ 130px + 16px); /* oppure misurare dinamicamente */
    z-index: 900;
    font-family: var(--mono);    /* in manifesto */
    font-size: 10px;
    letter-spacing: 0.2em;
    text-transform: uppercase;
    color: var(--muted);
    border: 1px solid rgba(255,255,255,0.08);
    padding: 9px 14px;
    text-decoration: none;
    background: transparent;
    backdrop-filter: blur(12px);
    cursor: pointer;
    transition: border-color 0.3s, color 0.3s;
}
.lang-switch:hover { color: var(--text); border-color: rgba(255,255,255,0.2); }
```

Su mobile (max-width: 700px) entrambi i bottoni potrebbero collidere: valutare di nascondere `.float-link` e mostrare solo il lang switcher, o usare un hamburger menu semplificato.

**Su contributors.html** non c'è un `.float-link` — il switcher può andare in `position: fixed; top: 24px; right: 24px` senza conflitti.

---

### 2. i18n puro (data-i18n) vs route separate /en/manifesto

**Confronto:**

#### Opzione A — `data-i18n` attributes (JS puro, stesso file)

**Pro:**
- Zero deploy aggiuntivi — tutto in un file
- Cambio lingua istantaneo, nessun reload
- Già usato in `index.html` con lo stesso pattern — coerenza di codebase
- SEO: con `<link rel="alternate" hreflang="en">` Google capisce il contenuto multilingua

**Contro:**
- Il contenuto EN non è indicizzato separatamente da Google senza route distinte
- La URL rimane `btcpredictor.io/manifesto` anche in EN — può confondere utenti EN che condividono il link
- Il JS di i18n aggiunge ~2-3 KB di payload

**Implementazione minima:**

```html
<!-- Nel <head> di ogni pagina: -->
<link rel="alternate" hreflang="it" href="https://btcpredictor.io/manifesto">
<link rel="alternate" hreflang="en" href="https://btcpredictor.io/manifesto?lang=en">

<!-- Ogni stringa diventa: -->
<p class="hero-sub" data-i18n="M-HERO-05">
    Un esperimento empirico sulla struttura dei mercati inefficienti.
</p>

<!-- JS loader (unico per tutte le pagine): -->
<script>
const TRANSLATIONS = { /* oggetto generato da questo file */ };
const lang = localStorage.getItem('btcp_lang') ||
             (navigator.language.startsWith('en') ? 'en' : 'it');
if (lang === 'en') {
    document.querySelectorAll('[data-i18n]').forEach(el => {
        const key = el.getAttribute('data-i18n');
        if (TRANSLATIONS.en[key]) el.textContent = TRANSLATIONS.en[key];
    });
}
</script>
```

**Limite**: non funziona per attributi (meta description, og:title, title, placeholder, aria-label) — serve logica separata per quelli.

---

#### Opzione B — Route separate `/en/manifesto` e `/en/contributors`

**Pro:**
- URL canoniche separate → Google indicizza EN e IT indipendentemente
- Condivisione link chiara (`/en/manifesto` è inequivocabilmente EN)
- Nessun JS runtime per la traduzione — HTML statico puro

**Contro:**
- Due file HTML da mantenere sincronizzati per ogni pagina (duplicazione)
- Ogni modifica al design richiede doppio edit
- Su Railway/Flask: servire `/en/*` richiede route Flask aggiuntive o redirect

**Implementazione su Flask:**
```python
@app.route('/en/manifesto')
def manifesto_en():
    return send_from_directory('.', 'manifesto_en.html')

@app.route('/en/contributors')
def contributors_en():
    return send_from_directory('.', 'contributors_en.html')
```

---

### 3. Raccomandazione

**Per lo stadio attuale del progetto (early, $100, focus su crescita organica):**

Usare **Opzione A (data-i18n + URL param)** per manifesto.html e contributors.html, allineandosi al pattern già in uso in index.html. Il guadagno SEO delle route separate è marginale per pagine che non hanno ancora traffico significativo EN. Il costo di mantenere due file separati sincronizzati è alto e produce errori nel tempo.

**Quando scala:** se il traffico EN supera il 30% e il dominio acquisisce backlink EN, migrare a route separate con Flask + `hreflang` canonici.

**Stringhe JS dinamiche** (form feedback, ROLE_LABELS, contrib stream title, anonimo badge): queste vanno in un oggetto `TRANSLATIONS` separato nel blocco `<script>` della pagina, cambiato al momento del language switch. Non possono usare `data-i18n` perché sono generate a runtime da fetch/JS.

**Meta tags e title**: usare JS per cambiare `document.title`, `document.querySelector('meta[name=description]').content`, ecc. al cambio lingua — non elegante ma funzionale senza route separate.

---

*File generato da analisi di manifesto.html (984 righe) e contributors.html (1543 righe). Nessun file HTML modificato.*
