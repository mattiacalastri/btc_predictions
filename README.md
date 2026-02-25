# BTC Prediction Bot 🤖

**AI + BigData + Blockchain — autonomous BTC futures prediction system**

A live, open-source trading bot that combines Claude LLM with XGBoost in a dual-gate architecture to predict and trade BTC/USD perpetual futures on Kraken. Every decision is logged to Supabase and committed to the Polygon blockchain — immutable, publicly verifiable proof of performance.

---

## What it does

- **Dual-gate AI prediction**: Claude Sonnet analyzes market data (order book, funding rate, L/S ratio, news sentiment, technical indicators) and issues a directional prediction. XGBoost must independently agree before any order is placed.
- **Autonomous execution**: accepted signals are sized, placed as Kraken Futures orders (`PF_XBTUSD`), and monitored for SL/TP in real time — no human in the loop.
- **On-chain audit trail**: every prediction is committed to Polygon PoS via `BTCBotAudit.sol` — an immutable, verifiable proof of performance that cannot be retroactively edited.
- **Live dashboard**: a single-page app at the Railway deployment URL shows real-time PnL, win rate, walk-forward backtesting across six strategies (A→F), and a War Room mode.

---

## Architecture

```
n8n wf01A (fetch data)
  └─► n8n wf01B (Claude AI decision)
          │
          ▼
  Flask / Railway ──► Kraken Futures (PF_XBTUSD)
  (bet sizing,              │
   place_bet,               │ fills / prices
   /signals)                │
          │                 ▼
          └──────► Supabase (btc_predictions)
                        │
                        ▼
              n8n wf02 (monitor / SL-TP / close)
                        │
                        ▼
               Polygon PoS (BTCBotAudit.sol)
               on-chain audit trail
```

Nine n8n workflows handle data ingestion, AI inference, trade monitoring, nightly maintenance, and Telegram commands. Flask on Railway is the stateless API that owns Kraken order execution and database writes.

---

## Stack

| Component   | Technology                              | Purpose                                    |
|-------------|------------------------------------------|--------------------------------------------|
| AI Engine   | Claude Sonnet + XGBoost (~86% accuracy) | Directional prediction, dual-gate filter   |
| Workflow    | n8n Cloud (9 workflows)                 | Data fetch, orchestration, scheduling      |
| Backend     | Python Flask + Gunicorn on Railway      | Order execution, bet sizing, REST API      |
| Database    | Supabase (PostgreSQL, 52-column schema) | Trade history, signals, calibration data   |
| Exchange    | Kraken Futures (PF_XBTUSD)             | Perpetual BTC/USD contract execution       |
| On-Chain    | Polygon PoS — `BTCBotAudit.sol`        | Immutable prediction audit trail           |
| Dashboard   | Vanilla JS + Chart.js                   | Live PnL, backtesting, War Room mode       |

---

## Live Performance

Dashboard: `https://web-production-e27d0.up.railway.app/dashboard`

The dashboard includes a **War Room mode** — a full-screen, dark-theme view of the latest signal, open position, and running PnL, designed for live monitoring. Walk-forward backtesting (strategies A→F) is available under the Training tab.

---

## Quick Start (Local)

```bash
# 1. Clone the repository
git clone https://github.com/your-username/btc_predictions.git
cd btc_predictions

# 2. Configure environment variables
cp .env.example .env
# Edit .env — add your Kraken, Supabase, Anthropic, and n8n keys
# Set DRY_RUN=true to paper-trade without placing real orders

# 3a. Docker (recommended)
docker-compose up

# 3b. Manual
pip install -r requirements.txt
flask run

# 4. Open the dashboard
open http://localhost:5000/dashboard
```

Key endpoints once running: `/health`, `/dashboard`, `/signals`, `/backtest-report`, `/predict-xgb`.

---

## Key Features

- **Dual-gate prediction** — Claude LLM and XGBoost must independently agree on direction; disagreement silently skips the trade (`xgb_disagree`)
- **Walk-forward backtesting** — six strategies (A → F) with per-month breakdown, Sharpe ratio, and max drawdown
- **Pyramiding** — adds to winning positions at higher confidence thresholds; historically accounts for the majority of PnL
- **Dead hours filter** — auto-computed from live Supabase data; low-win-rate UTC hours are suppressed automatically
- **Macro calendar guard** — ForexFactory high-impact USD events suppress trading in a ±2h window
- **On-chain audit trail** — every bet hashed and committed to Polygon; resolved on close
- **Telegram commander** — `/status`, `/balance`, `/position`, `/bets`, `/close`, `/pause`, `/resume` plus conversational Claude chat
- **War Room dashboard** — full-screen live monitoring mode with real-time PnL ticker
- **Auto-calibration** — confidence thresholds and dead hours recomputed from live win-rate data via `/reload-calibration`

---

## Project Structure

```
btc_predictions/
├── app.py                  # Flask API — order execution, bet sizing, endpoints
├── index.html              # Single-page dashboard (Vanilla JS + Chart.js)
├── contracts/
│   └── BTCBotAudit.sol     # Polygon smart contract for on-chain audit trail
├── datasets/               # Training CSVs and XGBoost reports
├── models/                 # Trained model files (xgb_direction.pkl, xgb_correctness.pkl)
├── tests/                  # pytest test suite
├── build_dataset.py        # Builds training dataset from Supabase history
├── train_xgboost.py        # Trains XGBoost direction + correctness models
├── retrain_pipeline.sh     # End-to-end retrain script (build → train → deploy)
├── docker-compose.yml      # Local stack definition
├── Dockerfile              # Production container image
├── Procfile                # Railway / Gunicorn entrypoint
├── requirements.txt        # Python dependencies
└── .env.example            # Environment variable template
```

---

## Environment Variables

See `.env.example` for the full list. Required groups:

| Group        | Variables                                                |
|--------------|----------------------------------------------------------|
| Kraken       | `KRAKEN_API_KEY`, `KRAKEN_API_SECRET`                   |
| Supabase     | `SUPABASE_URL`, `SUPABASE_KEY`, `SUPABASE_ANON_KEY`     |
| Anthropic    | `ANTHROPIC_API_KEY`, `CLAUDE_MODEL`                     |
| n8n          | `N8N_API_KEY`, `N8N_EXECUTION_LIMIT`                    |
| Bot behavior | `DRY_RUN` (set `true` for paper trading)                |

Never commit `.env` to version control. Rotate any accidentally exposed secrets immediately.

---

## On-Chain Verification

Contract: [`BTCBotAudit.sol`](contracts/BTCBotAudit.sol)
Deployed on: Polygon PoS — `0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55`
Explorer: `https://polygonscan.com/address/0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55`

Every prediction is committed as a keccak256 hash of `(bet_id, direction, confidence, timestamp)` at the time of placement. When the position closes, the outcome is resolved on-chain. To verify a specific bet: look up its `on_chain_tx` hash in Supabase, then query Polygonscan for the transaction data.

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup instructions, open areas, and what not to commit.

---

## Disclaimer

**This project is for educational and research purposes only.**

This software does not constitute financial advice, investment advice, trading advice, or any other sort of advice. Nothing in this repository should be interpreted as a recommendation to buy, sell, or hold any financial instrument.

Trading cryptocurrency futures involves substantial risk of loss. Past performance of the bot — including win rate, PnL, and backtest results — is not indicative of future results. You may lose some or all of your capital.

The authors and contributors of this project are not licensed financial advisors. By using, forking, or deploying this software you acknowledge that:

- You take full responsibility for any trading decisions made by you or by a deployed instance of this software.
- The authors provide no warranty of any kind, express or implied, regarding the accuracy, reliability, or profitability of the system.
- You have read and understood the applicable laws and regulations in your jurisdiction regarding automated trading systems (including but not limited to MiCA / ESMA in the EU).

**Run with `DRY_RUN=true` (the default in `.env.example`) to paper-trade and understand the system before using real funds.**

---

## License

MIT
