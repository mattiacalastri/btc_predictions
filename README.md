# BTC Prediction Bot 🤖

> *A whiteboard sketch. $100. A question: what if a machine could decide — not just compute?*

**AI + BigData + Blockchain — an autonomous BTC futures system that thinks before it trades.**

Live at [btcpredictor.io](https://btcpredictor.io) · Every prediction on-chain · All results public · Open source

[![Live](https://img.shields.io/badge/status-LIVE-00ff88?style=flat-square)](https://btcpredictor.io/dashboard)
[![On-Chain](https://img.shields.io/badge/on--chain-Polygon%20PoS-8b5cf6?style=flat-square)](https://polygonscan.com/address/0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55)
[![Build in Public](https://img.shields.io/badge/philosophy-Build%20in%20Public-00aaff?style=flat-square)](https://btcpredictor.io)
[![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE)

---

## The Origin

In February 2026, the night before leaving for China, Alessandro sketched a diagram on a whiteboard:

`AI AGENT` · `AUTONOMIA` · `BTC TRADING` · `ONLINE INFO LIVE 24/7` · `HIGH AUTOMATION`

The next morning, Mattia opened Claude and started building.

Today the system runs on a dedicated VPS. It wakes up every 10 minutes, consults 12+ live data sources, runs two independent AI models, and — only if they agree — places a real trade on Kraken Futures with real money.

Not a backtest. Not a demo. A live, verifiable, auditable **Behavioral Data Engine**.

---

## What it does

- **Dual-gate prediction** — Claude Sonnet and XGBoost must independently agree on direction. Disagreement = silent skip. No single AI is trusted alone.
- **Autonomous execution** — accepted signals are sized, placed as `PF_XBTUSD` perpetual futures orders on Kraken, and monitored for SL/TP in real time. No human in the loop.
- **On-chain audit trail** — every prediction is committed to Polygon PoS via `BTCBotAudit.sol`. The outcome is resolved on-chain when the position closes. Cannot be edited. Cannot be faked.
- **Behavioral engine** — the bot doesn't just read prices. It reads market *behavior*: crowd positioning (L/S ratio), capital flow direction (taker buy/sell ratio), funding rate extremes, order book imbalance, multi-timeframe consensus, and pattern memory of its own past decisions.
- **Live dashboard** — [btcpredictor.io](https://btcpredictor.io) shows real-time PnL, equity curve, win rate, walk-forward backtesting across six strategies, and a War Room mode.

---

## Architecture

```
n8n wf01A — 12 data sources in parallel
  news · BTC klines · order book · L/S ratio · taker ratio
  funding rate · fear&greed · macro guard · pattern memory
          │
          ▼
n8n wf01B — Claude Sonnet 4.6 + XGBoost dual-gate
          │
          ├──► SKIP (disagreement · low confidence · macro block · dead hours)
          │
          ▼
Flask / Railway ──────────────────────────► Kraken Futures (PF_XBTUSD)
  bet sizing · /place_bet · /signals                 │
          │                                          │ fills / mark prices
          ▼                                          ▼
  Polygon PoS                              Supabase (btc_predictions)
  BTCBotAudit.sol                                    │
  commitPrediction()                                 ▼
                                  n8n wf02 — SL/TP monitor / auto-close
                                           │
                                           ▼
                                  Polygon PoS — resolvePrediction()
```

12 n8n workflows handle: data ingestion, AI inference, trade monitoring, position rescue, nightly maintenance, Telegram commander, and social publishing. Flask on Railway owns Kraken order execution and all database writes.

> **Note:** n8n workflows are the orchestration layer — they are not included in this repository. This repo contains the Flask backend, XGBoost pipeline, smart contract, and dashboard. To replicate the full system, recreate the workflow logic in your own n8n instance using the architecture above as a reference.

---

## Stack

| Component   | Technology                                        | Purpose                                      |
|-------------|---------------------------------------------------|----------------------------------------------|
| AI Engine   | Claude Sonnet 4.6 + XGBoost (~86% CV accuracy)   | Directional prediction, dual-gate filter     |
| Workflow    | n8n self-hosted VPS (12 workflows)                | Data ingestion, orchestration, scheduling    |
| Backend     | Python Flask + Gunicorn → Railway                 | Order execution, bet sizing, REST API        |
| Database    | Supabase (PostgreSQL, 52-column schema)           | Trade history, signals, calibration data     |
| Exchange    | Kraken Futures (PF_XBTUSD)                        | Perpetual BTC/USD contract execution         |
| On-Chain    | Polygon PoS — `BTCBotAudit.sol`                   | Immutable prediction audit trail             |
| Dashboard   | Vanilla JS + Chart.js (zero build step)           | Live PnL, equity curve, backtesting          |

---

## Live Performance

→ **[btcpredictor.io/dashboard](https://btcpredictor.io/dashboard)**

The dashboard shows everything, in real time:
- **Cumulative equity curve** — real PnL, every closed bet
- **Walk-forward backtesting** — strategies A→F with per-month Sharpe and max drawdown
- **War Room mode** — full-screen live monitoring with real-time PnL ticker
- **On-chain ledger** — every bet links to its Polygon transaction hash

Predictions are open. Results are on-chain. Nothing is curated.

---

## Key Features

- **Dual-gate decision** — Claude LLM + XGBoost must independently agree; disagreement silently skips (`reason: xgb_disagree`)
- **Pattern memory** — the bot stores its own past decisions as features for the next prediction cycle. It learns from itself.
- **Multi-timeframe consensus** — 5m / 15m / 4h klines synthesized into a single `MTF_CONSENSUS` signal
- **Taker buy/sell ratio** — CVD proxy tracking real capital flow direction, not just price
- **Pyramiding** — adds to winning positions at higher confidence thresholds; historically the primary driver of PnL
- **Dead hours filter** — low-WR UTC hours computed live from Supabase data; suppressed automatically
- **Macro calendar guard** — ForexFactory high-impact USD events suppress trading in a ±2h window
- **Position rescue** — orphaned bets auto-detected and closed via `/rescue-orphaned`; `continueOnFail` on every critical node
- **Telegram commander** — `/status` `/balance` `/position` `/bets` `/close` `/pause` `/resume` + conversational Claude chat
- **Auto-calibration** — confidence thresholds and dead hours recomputed from live win-rate via `/reload-calibration`
- **On-chain audit** — every prediction hashed and committed to Polygon at placement; outcome resolved at close. Immutable.

---

## Quick Start (Local)

```bash
# 1. Clone
git clone https://github.com/mattiacalastri/btc_predictions.git
cd btc_predictions

# 2. Configure environment
cp .env.example .env
# Add Kraken, Supabase, Anthropic, n8n keys
# Set DRY_RUN=true to paper-trade — no real orders placed

# NOTE: n8n workflows are not included in this repo.
# Flask runs standalone (signals, backtesting, dashboard).
# Autonomous trading requires the n8n orchestration layer.

# 3a. Docker (recommended)
docker-compose up

# 3b. Manual
pip install -r requirements.txt
flask run

# 4. Open dashboard
open http://localhost:5000/dashboard
```

Key endpoints: `/health` · `/dashboard` · `/signals` · `/backtest-report` · `/predict-xgb` · `/n8n-status`

---

## Project Structure

```
btc_predictions/
├── app.py                  # Flask API — all endpoints, order execution, bet sizing
├── index.html              # Single-page dashboard (Vanilla JS + Chart.js)
├── contracts/
│   └── BTCBotAudit.sol     # Polygon smart contract — on-chain prediction audit
├── datasets/               # Training CSVs and XGBoost performance reports
├── models/                 # Trained model files (xgb_direction.pkl, xgb_correctness.pkl)
├── tests/                  # pytest test suite
├── build_dataset.py        # Builds training dataset from Supabase history
├── train_xgboost.py        # Trains XGBoost direction + correctness classifiers
├── retrain_pipeline.sh     # End-to-end retrain: build → train → deploy
├── backup_n8n_workflows.py # Weekly n8n workflow backup to private git repo
├── docker-compose.yml      # Local stack
├── Dockerfile              # Production container image
├── Procfile                # Railway / Gunicorn entrypoint
├── requirements.txt        # Python dependencies
└── .env.example            # Environment variable template
```

---

## Environment Variables

See `.env.example` for the full list. Required groups:

| Group        | Variables                                                                        |
|--------------|----------------------------------------------------------------------------------|
| Kraken       | `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`                                           |
| Supabase     | `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_ANON_KEY`                             |
| Anthropic    | `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`                                             |
| n8n          | `N8N_URL`, `N8N_API_KEY`                                                        |
| Bot behavior | `DRY_RUN` — set `true` for paper trading (default)                              |
| On-chain     | `POLYGON_PRIVATE_KEY`, `POLYGON_CONTRACT_ADDRESS`, `POLYGON_RPC_URL`            |

Never commit `.env`. Rotate any accidentally exposed key immediately.

---

## On-Chain Verification

Every prediction is committed to Polygon PoS at the moment of placement — an immutable record that cannot be retroactively edited.

**Contract**: [`BTCBotAudit.sol`](contracts/BTCBotAudit.sol)
**Address**: `0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55`
**Explorer**: [polygonscan.com — verified ✅](https://polygonscan.com/address/0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55#code)

**Commit hash**: `keccak256(bet_id, direction, confidence×1e6, entry_price×1e2, bet_size×1e8, timestamp)`
**Resolve hash**: `keccak256(bet_id, exit_price×1e2, pnl×1e6, won, close_timestamp)`

To verify a specific bet: find its `onchain_commit_tx` in the dashboard → look up the transaction hash on Polygonscan.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, open areas, and what not to commit.

---

## Disclaimer

**This project is for educational and research purposes only.**

This software does not constitute financial advice, investment advice, or trading advice of any kind. Trading cryptocurrency futures involves substantial risk of loss. Past performance — including win rate, PnL, and backtest results — is not indicative of future results. You may lose some or all of your capital.

By using, forking, or deploying this software you acknowledge that you take full responsibility for any trading decisions made by a deployed instance of this software. The authors provide no warranty of any kind regarding accuracy, reliability, or profitability.

**`DRY_RUN=true` is the default. Understand the system before touching real funds.**

---

## Contact

**BTC Predictor** · [btcpredictor.io](https://btcpredictor.io)
✉ [signal@btcpredictor.io](mailto:signal@btcpredictor.io)

---

*Build in public. Real numbers. Zero hype.*

---

## License

MIT
