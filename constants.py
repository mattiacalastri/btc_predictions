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
