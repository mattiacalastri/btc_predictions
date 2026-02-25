# Contributing to BTC Prediction Bot

Welcome, and thanks for your interest. This project is built in public — a real, live trading bot that
combines Claude AI + XGBoost + Kraken Futures to pursue financial liberty through open, verifiable
autonomous trading. Every bet it places is recorded publicly in Supabase. You can watch it learn.

---

## Stack

| Layer | Technology |
|-------|------------|
| Backend API | Flask + Gunicorn, deployed on Railway |
| Automation | n8n cloud (8 active workflows) |
| Database | Supabase (`btc_predictions` table, 52 columns) |
| AI / ML | Claude AI (decision agent) + XGBoost (dual-gate filter, ~85% accuracy) |
| Exchange | Kraken Futures (`PF_XBTUSD`) |

---

## Run locally

```bash
# 1. Clone the repo
git clone https://github.com/your-username/btc_predictions.git
cd btc_predictions

# 2. Set up environment variables
cp .env.example .env
# Edit .env and fill in your keys (Kraken, Supabase, etc.)
# Set DRY_RUN=true to prevent real orders while testing

# 3. Start the stack
docker-compose up
```

The API will be available at `http://localhost:5000`. Key endpoints: `/health`, `/dashboard`, `/signals`.

---

## How to contribute

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Run the test suite: `pytest tests/`
4. Open a PR to `main` — describe what you changed and why

No special linting setup exists yet. Just be consistent with the style of the file you are editing
(snake_case, f-strings, docstrings where the function is non-obvious).

---

## Areas where contributions are welcome

- **New data sources for wf01** — news feeds, on-chain metrics (Glassnode, Dune), social sentiment
- **ML model improvements** — new features for XGBoost, hyperparameter tuning, alternative models
- **Dashboard improvements** — `index.html` charts, new visualizations, mobile layout
- **Documentation** — clearer setup guides, architecture diagrams, annotated workflow exports
- **Bug fixes** — especially around edge cases in bet lifecycle (orphaned bets, partial fills, reverse positions)

---

## What NOT to do

- Do not commit real `.env` files, API keys, or Supabase credentials
- Do not commit model files (`models/*.pkl`) that were trained on private data
- Do not push directly to `main` — always use a PR

If you accidentally expose a secret, rotate it immediately and let us know via a private issue.

---

## Questions?

Open an issue or start a discussion. The project vision is big — AI + behavioral data + on-chain audit
trail — and there is room for people who want to build something real.
