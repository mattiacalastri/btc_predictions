#!/usr/bin/env python3
"""
backtest.py — BTC Prediction Bot: Walk-Forward Backtesting

Simula 6 strategie di filtraggio sul set di test (30% più recente),
con parametri derivati esclusivamente dal train set (70% più vecchio).

Strategie testate:
  A  BASELINE     — tutti i segnali LLM
  B  CONF_062     — confidence >= 0.62 (soglia attuale)
  C  CONF_065     — confidence >= 0.65
  D  DEAD_HOURS   — CONF_062 + skip ore con WR < 45% (calcolate su train)
  E  XGB_GATE     — CONF_062 + XGB direction == LLM direction (XGB su train)
  F  FULL_STACK   — CONF_062 + dead hours + XGB gate (stack attuale completo)

Output: ./datasets/backtest_report.txt

Usage:
  python3 backtest.py [--train-ratio 0.7] [--output-dir ./datasets]

Env vars: SUPABASE_URL, SUPABASE_KEY
"""

import os
import json
import ssl
import argparse
import pickle
import math
from datetime import datetime
import urllib.request
import urllib.parse

import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score

# ─── SSL bypass macOS ──────────────────────────────────────────────────────────
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

TAKER_RATE  = 0.00005  # Kraken Futures taker fee 0.005% per lato
BASE_SIZE   = 0.002    # BTC fisso per confronto equo tra strategie

FEATURE_COLS = [
    "confidence", "fear_greed_value", "rsi14", "technical_score", "hour_utc",
    "ema_trend_up", "technical_bias_bullish", "signal_technical_buy",
    "signal_sentiment_pos", "signal_fg_fear", "signal_volume_high",
]


# ─── Supabase ─────────────────────────────────────────────────────────────────
def supabase_get(params: dict) -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise EnvironmentError("SUPABASE_URL / SUPABASE_KEY non configurati")
    qs = urllib.parse.urlencode(params)
    url = f"{SUPABASE_URL}/rest/v1/btc_predictions?{qs}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


def fetch_bets() -> pd.DataFrame:
    """Fetch tutte le bet chiuse con pnl_usd, ordinate cronologicamente."""
    cols = ",".join([
        "id", "created_at", "direction", "confidence", "correct",
        "pnl_usd", "bet_size", "entry_fill_price", "exit_fill_price",
        "btc_price_entry", "fear_greed_value", "rsi14", "technical_score",
        "ema_trend", "technical_bias", "signal_technical",
        "signal_sentiment", "signal_fear_greed", "signal_volume",
    ])
    all_rows, offset = [], 0
    while True:
        rows = supabase_get({
            "select": cols,
            "bet_taken": "eq.true",
            "correct":   "not.is.null",
            "pnl_usd":   "not.is.null",
            "order":     "created_at.asc",
            "limit":     1000,
            "offset":    offset,
        })
        all_rows.extend(rows)
        if len(rows) < 1000:
            break
        offset += 1000

    df = pd.DataFrame(all_rows)
    if df.empty:
        return df

    # Tipi
    for col in ["confidence", "pnl_usd", "bet_size", "entry_fill_price",
                "exit_fill_price", "btc_price_entry", "fear_greed_value",
                "rsi14", "technical_score"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["correct"] = df["correct"].astype(bool)

    # Ora UTC
    df["hour_utc"] = df["created_at"].str[11:13].astype(int, errors="ignore")

    # Feature binarie
    df["ema_trend_up"]          = (df["ema_trend"].str.upper() == "UP").astype(int)
    df["technical_bias_bullish"]= df["technical_bias"].str.lower().str.contains("bull", na=False).astype(int)
    df["signal_technical_buy"]  = (df["signal_technical"].str.upper() == "BUY").astype(int)
    df["signal_sentiment_pos"]  = df["signal_sentiment"].str.upper().isin(["POSITIVE","POS","BUY"]).astype(int)
    df["signal_fg_fear"]        = (df["signal_fear_greed"].str.upper() == "FEAR").astype(int)
    df["signal_volume_high"]    = df["signal_volume"].str.lower().str.contains("high", na=False).astype(int)

    return df


# ─── Simulatore equity ────────────────────────────────────────────────────────
def simulate(bets: pd.DataFrame, strategy_name: str, report: list) -> dict:
    """
    Prende le bet che la strategia ha filtrato (subset di test),
    calcola equity curve con PnL reale da Supabase + fee stima entry.

    pnl_usd in Supabase ora include entry+exit fee per le bet recenti.
    Per bet vecchie (entry fee mancante), l'errore è ~$0.005/bet — accettabile.
    """
    n = len(bets)
    if n == 0:
        return {"n": 0, "pnl": 0.0, "wr": 0.0, "pnl_per_bet": 0.0,
                "max_dd": 0.0, "sharpe": 0.0}

    pnls   = bets["pnl_usd"].tolist()
    equity = 0.0
    peak   = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    wins        = bets["correct"].sum()
    wr          = wins / n * 100
    total_pnl   = round(sum(pnls), 4)
    pnl_per_bet = round(total_pnl / n, 4)

    # Sharpe semplificato: avg / std * sqrt(n) — proxy per risk-adjusted return
    arr  = np.array(pnls)
    std  = arr.std()
    sharpe = round((arr.mean() / std * math.sqrt(n)) if std > 0 else 0.0, 2)

    return {
        "n":           n,
        "pnl":         total_pnl,
        "wr":          round(wr, 1),
        "pnl_per_bet": pnl_per_bet,
        "max_dd":      round(max_dd, 4),
        "sharpe":      sharpe,
    }


# ─── Report helpers ───────────────────────────────────────────────────────────
def bar(value: float, max_val: float, width: int = 20) -> str:
    if max_val == 0:
        return " " * width
    filled = int(abs(value) / max_val * width)
    char = "█" if value >= 0 else "░"
    return char * min(filled, width)


def fmt_pnl(v: float) -> str:
    return f"+${v:.4f}" if v >= 0 else f"-${abs(v):.4f}"


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-ratio", type=float, default=0.70)
    parser.add_argument("--output-dir",  default="./datasets")
    parser.add_argument("--conf-threshold", type=float, default=0.62)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    lines = []

    def log(s=""):
        print(s)
        lines.append(s)

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log(f"╔══════════════════════════════════════════════════════╗")
    log(f"  BTC Prediction Bot — Walk-Forward Backtest  {ts}")
    log(f"╚══════════════════════════════════════════════════════╝")
    log()

    # ── 1. Fetch dati ─────────────────────────────────────────────────────────
    log(f"[1/5] Fetching bet da Supabase...")
    df = fetch_bets()
    n_total = len(df)
    if n_total < 20:
        log(f"  ⚠️  Solo {n_total} bet con pnl_usd. Troppo pochi per un backtest significativo.")
        return

    log(f"  Bet totali con pnl_usd: {n_total}")
    log(f"  Periodo: {df['created_at'].iloc[0][:10]} → {df['created_at'].iloc[-1][:10]}")
    log()

    # ── 2. Walk-forward split (cronologico, NO shuffle) ───────────────────────
    split_idx  = int(n_total * args.train_ratio)
    df_train   = df.iloc[:split_idx].copy()
    df_test    = df.iloc[split_idx:].copy()

    log(f"[2/5] Walk-forward split (train {args.train_ratio:.0%} / test {1-args.train_ratio:.0%})")
    log(f"  TRAIN: {len(df_train)} bet  ({df_train['created_at'].iloc[0][:10]} → {df_train['created_at'].iloc[-1][:10]})")
    log(f"  TEST:  {len(df_test)}  bet  ({df_test['created_at'].iloc[0][:10]}  → {df_test['created_at'].iloc[-1][:10]})")

    # WR train set
    train_wr = df_train["correct"].mean() * 100
    test_wr  = df_test["correct"].mean() * 100
    log(f"  WR train: {train_wr:.1f}%  |  WR test: {test_wr:.1f}%")
    log()

    # ── 3. Parametri da TRAIN set ─────────────────────────────────────────────
    log(f"[3/5] Calcolo parametri da TRAIN set...")

    # Dead hours: ore UTC con WR < 45% e almeno 4 bet
    dead_hours = set()
    for h in range(24):
        sub = df_train[df_train["hour_utc"] == h]
        if len(sub) >= 4:
            wr_h = sub["correct"].mean()
            if wr_h < 0.45:
                dead_hours.add(h)

    log(f"  Dead hours (WR < 45% su train): {sorted(dead_hours) if dead_hours else 'nessuna'}")

    # XGB direction model — addestrato solo su train
    df_train_clean = df_train.dropna(subset=FEATURE_COLS + ["direction"])
    xgb_model = None
    xgb_cv_acc = None

    if len(df_train_clean) >= 15:
        X_train = df_train_clean[FEATURE_COLS].values
        y_train = (df_train_clean["direction"] == "UP").astype(int).values

        xgb_model = XGBClassifier(
            n_estimators=150, max_depth=4, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8,
            eval_metric="logloss", random_state=42, verbosity=0,
        )
        n_splits = min(5, len(np.unique(y_train)))
        if n_splits >= 2 and sum(y_train) >= 2 and sum(1-y_train) >= 2:
            cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
            cv_scores = cross_val_score(xgb_model, X_train, y_train, cv=cv, scoring="accuracy")
            xgb_cv_acc = cv_scores.mean()
        xgb_model.fit(X_train, y_train)
        log(f"  XGB direction: CV acc {xgb_cv_acc:.1%}" if xgb_cv_acc else "  XGB direction: addestrato (CV non disponibile)")
    else:
        log(f"  ⚠️  Train set troppo piccolo per XGB ({len(df_train_clean)} righe). Gate E/F disabilitato.")

    log()

    # ── 4. Applica strategie su TEST set ──────────────────────────────────────
    log(f"[4/5] Simulazione strategie su TEST set ({len(df_test)} bet)...")
    conf_thr = args.conf_threshold

    # Prepara features test per XGB
    df_test_clean = df_test.dropna(subset=FEATURE_COLS + ["direction"])

    def xgb_directions(df_subset):
        """Predice direzione XGB per un subset di test. Restituisce Series con index=df_subset.index."""
        if xgb_model is None or df_subset.empty:
            return pd.Series(dtype=str)
        feat = df_subset[FEATURE_COLS].values
        probs = xgb_model.predict_proba(feat)[:, 1]  # P(UP)
        preds = pd.Series(
            np.where(probs > 0.5, "UP", "DOWN"),
            index=df_subset.index,
        )
        return preds

    xgb_pred = xgb_directions(df_test_clean)

    strategies = {}

    # A — BASELINE: tutti i segnali LLM
    strategies["A_BASELINE"] = df_test

    # B — CONF_062
    strategies["B_CONF_062"] = df_test[df_test["confidence"] >= conf_thr]

    # C — CONF_065
    strategies["C_CONF_065"] = df_test[df_test["confidence"] >= 0.65]

    # D — DEAD_HOURS: conf >= thr + skip dead hours
    if dead_hours:
        strategies["D_DEAD_HOURS"] = df_test[
            (df_test["confidence"] >= conf_thr) &
            (~df_test["hour_utc"].isin(dead_hours))
        ]
    else:
        strategies["D_DEAD_HOURS"] = strategies["B_CONF_062"].copy()

    # E — XGB_GATE: conf >= thr + XGB direction == LLM direction
    if xgb_model is not None:
        agree_idx = xgb_pred[xgb_pred == df_test_clean["direction"]].index
        strategies["E_XGB_GATE"] = df_test[
            (df_test["confidence"] >= conf_thr) &
            (df_test.index.isin(agree_idx))
        ]
    else:
        strategies["E_XGB_GATE"] = strategies["B_CONF_062"].copy()

    # F — FULL_STACK: conf >= thr + dead hours + XGB gate
    if xgb_model is not None:
        strategies["F_FULL_STACK"] = df_test[
            (df_test["confidence"] >= conf_thr) &
            (~df_test["hour_utc"].isin(dead_hours)) &
            (df_test.index.isin(agree_idx))
        ]
    else:
        strategies["F_FULL_STACK"] = strategies["D_DEAD_HOURS"].copy()

    # ── 5. Report risultati ───────────────────────────────────────────────────
    log()
    log(f"[5/5] Risultati walk-forward (test set: {len(df_test)} bet)")
    log()
    log(f"  {'Strategia':<18} {'N':>4}  {'WR':>6}  {'PnL totale':>11}  {'$/bet':>8}  {'MaxDD':>8}  {'Sharpe':>7}")
    log(f"  " + "─"*72)

    results = {}
    max_abs_pnl = 0.01

    for name, bets in strategies.items():
        r = simulate(bets, name, lines)
        results[name] = r
        max_abs_pnl = max(max_abs_pnl, abs(r["pnl"]))

    for name, r in results.items():
        label = name[2:]  # rimuovi prefisso "A_" ecc.
        pnl_str = fmt_pnl(r["pnl"])
        pbt_str = fmt_pnl(r["pnl_per_bet"]) if r["n"] > 0 else "  n/a"
        dd_str  = f"-${r['max_dd']:.4f}"
        flag    = " ◀ CURRENT" if name == "F_FULL_STACK" else ""
        log(
            f"  {name:<18}  {r['n']:>4}  {r['wr']:>5.1f}%  "
            f"{pnl_str:>11}  {pbt_str:>8}  {dd_str:>8}  {r['sharpe']:>7.2f}"
            f"{flag}"
        )

    log()

    # Barre di confronto PnL
    log(f"  PnL comparativo:")
    for name, r in results.items():
        b = bar(r["pnl"], max_abs_pnl)
        sign = "+" if r["pnl"] >= 0 else "-"
        log(f"  {name[2:]:<14} {sign}│{b:<20}│ {fmt_pnl(r['pnl'])}")

    log()

    # Best strategy
    best = max(results.items(), key=lambda x: x[1]["pnl"])
    log(f"  🏆 Miglior strategia sul test set: {best[0]} (PnL {fmt_pnl(best[1]['pnl'])})")
    log()

    # ── Analisi per confidence bucket su TEST set ─────────────────────────────
    log(f"  Confidence bucket su TEST set:")
    log(f"  {'Bucket':<13} {'N':>4}  {'WR':>6}  {'PnL totale':>11}  {'$/bet':>8}")
    log(f"  " + "─"*45)
    bins = [0.50, 0.55, 0.60, 0.62, 0.65, 0.70, 1.01]
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i+1]
        sub = df_test[(df_test["confidence"] >= lo) & (df_test["confidence"] < hi)]
        if len(sub) == 0:
            continue
        wr_b   = sub["correct"].mean() * 100
        pnl_b  = sub["pnl_usd"].sum()
        ppb_b  = pnl_b / len(sub)
        log(f"  [{lo:.2f},{hi:.2f})  {len(sub):>4}  {wr_b:>5.1f}%  {fmt_pnl(pnl_b):>11}  {fmt_pnl(ppb_b):>8}")

    log()

    # ── Analisi per ora UTC su TEST set ──────────────────────────────────────
    log(f"  WR per ora UTC su TEST set:")
    log(f"  {'Ora':>4}  {'N':>4}  {'WR':>6}  {'PnL':>9}  {'Note'}")
    log(f"  " + "─"*42)
    for h in range(24):
        sub = df_test[df_test["hour_utc"] == h]
        if len(sub) < 3:
            continue
        wr_h  = sub["correct"].mean() * 100
        pnl_h = sub["pnl_usd"].sum()
        note = "💀 DEAD" if h in dead_hours else ("✅ HOT" if wr_h >= 60 else "")
        log(f"  {h:>4}h  {len(sub):>4}  {wr_h:>5.1f}%  {fmt_pnl(pnl_h):>9}  {note}")

    log()

    # ── Note metodologiche ────────────────────────────────────────────────────
    log(f"  Note:")
    log(f"  • Train: {len(df_train)} bet, Test: {len(df_test)} bet (split {args.train_ratio:.0%}/{1-args.train_ratio:.0%} cronologico)")
    log(f"  • pnl_usd da Supabase include entry+exit fee per bet recenti")
    log(f"  • XGB addestrato SOLO su train set (no data leakage)")
    log(f"  • Dead hours calcolate SOLO da train set")
    log(f"  • Bet skippate contano come $0 (no gain, no loss)")
    log(f"  • Soglia confidence: {conf_thr}")
    if xgb_cv_acc:
        log(f"  • XGB CV accuracy su train: {xgb_cv_acc:.1%}")
    log()

    # ── Avvertenza statistica ─────────────────────────────────────────────────
    n_test = len(df_test)
    if n_test < 40:
        log(f"  ⚠️  ATTENZIONE: test set di soli {n_test} bet. Risultati non statisticamente robusti.")
        log(f"     Servono almeno 100 bet in test per conclusions affidabili.")
    log()
    log(f"  Generated: {ts}")

    # ── Salva report ──────────────────────────────────────────────────────────
    rep_path = os.path.join(args.output_dir, "backtest_report.txt")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  Report salvato: {rep_path}")


if __name__ == "__main__":
    main()
