"""
constants.py — Single source of truth for constants shared across modules.

Import from here instead of redefining locally in app.py, build_dataset.py,
backtest.py, etc.
"""

# ── Fee ────────────────────────────────────────────────────────────────────────
TAKER_FEE = 0.0005  # Kraken Futures taker fee: 0.05% per lato (Tier 1, <$100K/30d)

# ── Technical bias map ────────────────────────────────────────────────────────
# Encoding ordinale: strong_bearish=-2 → neutral=0 → strong_bullish=+2
_BIAS_MAP = {
    "strong_bearish": -2,
    "mild_bearish":   -1,
    "bearish":        -1,
    "neutral":         0,
    "mild_bullish":    1,
    "bullish":         1,
    "strong_bullish":  2,
}

# ── XGBoost hyperparameters ───────────────────────────────────────────────────
# Single source of truth: used by train_xgboost.py and backtest.py.
# backtest.py overrides n_estimators=150 for speed on the reduced dataset
# (70% train split vs full dataset used in training).
XGB_PARAMS = dict(
    n_estimators=200,
    max_depth=4,
    learning_rate=0.05,
    subsample=0.8,
    colsample_bytree=0.8,
    eval_metric="logloss",
    random_state=42,
    verbosity=0,
)
