"""
constants.py — Unica sorgente di verità per costanti condivise tra i moduli.

Importare qui invece di ridefinire localmente in app.py, build_dataset.py,
backtest.py, ecc.
"""

# ── Fee ────────────────────────────────────────────────────────────────────────
TAKER_FEE = 0.00005  # Kraken Futures taker fee: 0.005% per lato (entry + exit)

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
# Unica sorgente di verità: usati da train_xgboost.py e backtest.py.
# backtest.py sovrascrive n_estimators=150 per velocità sul dataset ridotto
# (70% train split vs il dataset completo usato in training).
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
