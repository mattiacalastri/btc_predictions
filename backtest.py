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

Output:
  ./datasets/backtest_report.txt  — report leggibile
  ./datasets/backtest_data.json   — dati strutturati per dashboard

Usage:
  python3 backtest.py [--train-ratio 0.7] [--output-dir ./datasets]

Env vars: SUPABASE_URL, SUPABASE_KEY
"""

import os
import json
import ssl
import argparse
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
    "confidence", "fear_greed_value", "rsi14", "technical_score",
    "hour_sin", "hour_cos",
    "technical_bias_score", "signal_fg_fear",
    # T-01: encoding ciclico giorno settimana + sessione liquidità
    "dow_sin", "dow_cos",   # sin/cos del giorno della settimana (0=lun, 6=dom)
    "session",              # 0=Asia(0-7 UTC), 1=London(8-13 UTC), 2=NY(14-23 UTC)
    # RIMOSSI: ema_trend_up, signal_technical_buy, signal_sentiment_pos,
    # signal_volume_high — 0% importance → costanti/skewed nel dataset
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

    for col in ["confidence", "pnl_usd", "bet_size", "entry_fill_price",
                "exit_fill_price", "btc_price_entry", "fear_greed_value",
                "rsi14", "technical_score"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["correct"] = df["correct"].astype(bool)
    df["hour_utc"] = df["created_at"].str[11:13].astype(int, errors="ignore")

    df["ema_trend_up"]          = (df["ema_trend"].str.upper() == "UP").astype(int)
    _bias_map = {"strong_bearish":-2,"mild_bearish":-1,"bearish":-1,
                 "neutral":0,"mild_bullish":1,"bullish":1,"strong_bullish":2}
    df["technical_bias_score"]  = df["technical_bias"].str.lower().str.strip().map(_bias_map).fillna(0)
    df["signal_technical_buy"]  = (df["signal_technical"].str.upper() == "BUY").astype(int)
    df["signal_sentiment_pos"]  = df["signal_sentiment"].str.upper().isin(["POSITIVE","POS","BUY"]).astype(int)
    df["signal_fg_fear"]        = (df["fear_greed_value"].fillna(50).astype(float) < 45).astype(int)
    df["signal_volume_high"]    = df["signal_volume"].str.lower().str.contains("high", na=False).astype(int)

    return df


# ─── Simulatore equity ────────────────────────────────────────────────────────
def simulate(bets: pd.DataFrame) -> dict:
    """
    Calcola metriche complete per una strategia.
    Restituisce dict con tutte le metriche + equity_curve per il grafico.
    """
    n = len(bets)
    empty = {
        "n": 0, "pnl": 0.0, "wr": 0.0, "pnl_per_bet": 0.0,
        "max_dd": 0.0, "max_dd_dur": 0, "sharpe": 0.0,
        "profit_factor": 0.0, "sortino": 0.0, "equity_curve": [0.0],
    }
    if n == 0:
        return empty

    pnls    = bets["pnl_usd"].tolist()
    equity  = 0.0
    peak    = 0.0
    max_dd  = 0.0
    dd_len  = 0
    max_dd_dur = 0
    equity_curve = [0.0]

    for p in pnls:
        equity += p
        equity_curve.append(round(equity, 4))
        if equity > peak:
            peak    = equity
            dd_len  = 0
        else:
            dd_len += 1
            max_dd_dur = max(max_dd_dur, dd_len)
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    wins        = int(bets["correct"].sum())
    wr          = wins / n * 100
    total_pnl   = round(sum(pnls), 4)
    pnl_per_bet = round(total_pnl / n, 4)

    arr    = np.array(pnls)
    std    = arr.std()
    mean   = arr.mean()
    sharpe = round((mean / std * math.sqrt(n)) if std > 0 else 0.0, 2)

    # Profit Factor
    pos_sum = float(arr[arr > 0].sum())
    neg_sum = float(abs(arr[arr < 0].sum()))
    if neg_sum > 0:
        profit_factor = round(pos_sum / neg_sum, 2)
    elif pos_sum > 0:
        profit_factor = 99.0
    else:
        profit_factor = 0.0

    # Sortino (penalizza solo downside)
    neg_returns = arr[arr < 0]
    if len(neg_returns) >= 2:
        downside_std = neg_returns.std()
        sortino = round((mean / downside_std * math.sqrt(n)) if downside_std > 0 else 0.0, 2)
    else:
        sortino = round(sharpe * 1.5, 2)

    return {
        "n":            n,
        "pnl":          total_pnl,
        "wr":           round(wr, 1),
        "pnl_per_bet":  pnl_per_bet,
        "max_dd":       round(max_dd, 4),
        "max_dd_dur":   max_dd_dur,
        "sharpe":       sharpe,
        "profit_factor": profit_factor,
        "sortino":      sortino,
        "equity_curve": equity_curve,
    }


# ─── Streak analysis ──────────────────────────────────────────────────────────
def streak_analysis(bets: pd.DataFrame) -> dict:
    if bets.empty:
        return {"max_win_streak": 0, "max_loss_streak": 0}
    results = bets["correct"].tolist()
    max_win = max_loss = cur_win = cur_loss = 0
    for r in results:
        if r:
            cur_win  += 1; cur_loss = 0
        else:
            cur_loss += 1; cur_win  = 0
        max_win  = max(max_win,  cur_win)
        max_loss = max(max_loss, cur_loss)
    return {"max_win_streak": max_win, "max_loss_streak": max_loss}


# ─── Report helpers ───────────────────────────────────────────────────────────
def bar(value: float, max_val: float, width: int = 20) -> str:
    if max_val == 0:
        return " " * width
    filled = int(abs(value) / max_val * width)
    char   = "█" if value >= 0 else "░"
    return char * min(filled, width)


def fmt_pnl(v: float) -> str:
    return f"+${v:.4f}" if v >= 0 else f"-${abs(v):.4f}"


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train-ratio",      type=float, default=0.70)
    parser.add_argument("--output-dir",       default="./datasets")
    parser.add_argument("--conf-threshold",   type=float, default=0.62)
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
    train_wr = df_train["correct"].mean() * 100
    test_wr  = df_test["correct"].mean()  * 100
    log(f"  WR train: {train_wr:.1f}%  |  WR test: {test_wr:.1f}%")
    log()

    # ── 3. Parametri da TRAIN set ─────────────────────────────────────────────
    log(f"[3/5] Calcolo parametri da TRAIN set...")

    dead_hours = set()
    for h in range(24):
        sub = df_train[df_train["hour_utc"] == h]
        if len(sub) >= 4:
            if sub["correct"].mean() < 0.45:
                dead_hours.add(h)
    log(f"  Dead hours (WR < 45% su train): {sorted(dead_hours) if dead_hours else 'nessuna'}")

    df_train_clean = df_train.dropna(subset=FEATURE_COLS + ["direction"])
    xgb_model  = None
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
            cv         = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
            cv_scores  = cross_val_score(xgb_model, X_train, y_train, cv=cv, scoring="accuracy")
            xgb_cv_acc = cv_scores.mean()
        xgb_model.fit(X_train, y_train)
        log(f"  XGB direction: CV acc {xgb_cv_acc:.1%}" if xgb_cv_acc else "  XGB direction: addestrato (CV non disponibile)")
    else:
        log(f"  ⚠️  Train set troppo piccolo per XGB ({len(df_train_clean)} righe). Gate E/F disabilitato.")
    log()

    # ── 4. Applica strategie su TEST set ──────────────────────────────────────
    log(f"[4/5] Simulazione strategie su TEST set ({len(df_test)} bet)...")
    conf_thr = args.conf_threshold

    df_test_clean = df_test.dropna(subset=FEATURE_COLS + ["direction"])

    def xgb_directions(df_subset):
        if xgb_model is None or df_subset.empty:
            return pd.Series(dtype=str)
        probs = xgb_model.predict_proba(df_subset[FEATURE_COLS].values)[:, 1]
        return pd.Series(np.where(probs > 0.5, "UP", "DOWN"), index=df_subset.index)

    xgb_pred   = xgb_directions(df_test_clean)
    agree_idx  = xgb_pred[xgb_pred == df_test_clean["direction"]].index if xgb_model else []

    strategies = {
        "A_BASELINE":  df_test,
        "B_CONF_062":  df_test[df_test["confidence"] >= conf_thr],
        "C_CONF_065":  df_test[df_test["confidence"] >= 0.65],
    }
    if dead_hours:
        strategies["D_DEAD_HOURS"] = df_test[
            (df_test["confidence"] >= conf_thr) & (~df_test["hour_utc"].isin(dead_hours))
        ]
    else:
        strategies["D_DEAD_HOURS"] = strategies["B_CONF_062"].copy()

    if xgb_model is not None:
        strategies["E_XGB_GATE"] = df_test[
            (df_test["confidence"] >= conf_thr) & (df_test.index.isin(agree_idx))
        ]
        strategies["F_FULL_STACK"] = df_test[
            (df_test["confidence"] >= conf_thr) &
            (~df_test["hour_utc"].isin(dead_hours)) &
            (df_test.index.isin(agree_idx))
        ]
    else:
        strategies["E_XGB_GATE"]  = strategies["B_CONF_062"].copy()
        strategies["F_FULL_STACK"] = strategies["D_DEAD_HOURS"].copy()

    # ── 5. Report risultati ───────────────────────────────────────────────────
    log()
    log(f"[5/5] Risultati walk-forward (test set: {len(df_test)} bet)")
    log()
    log(f"  {'Strategia':<18} {'N':>4}  {'WR':>6}  {'PnL':>10}  {'$/bet':>8}  {'MaxDD':>8}  {'PF':>5}  {'Sortino':>7}  {'Sharpe':>7}")
    log(f"  " + "─"*88)

    results      = {}
    max_abs_pnl  = 0.01

    for name, bets in strategies.items():
        r = simulate(bets)
        results[name] = r
        max_abs_pnl = max(max_abs_pnl, abs(r["pnl"]))

    for name, r in results.items():
        pnl_str = fmt_pnl(r["pnl"])
        pbt_str = fmt_pnl(r["pnl_per_bet"]) if r["n"] > 0 else "  n/a"
        dd_str  = f"-${r['max_dd']:.4f}"
        pf_str  = f"{r['profit_factor']:.2f}" if r["profit_factor"] < 90 else ">99"
        flag    = " ◀ CURRENT" if name == "F_FULL_STACK" else ""
        log(
            f"  {name:<18}  {r['n']:>4}  {r['wr']:>5.1f}%  "
            f"{pnl_str:>10}  {pbt_str:>8}  {dd_str:>8}  "
            f"{pf_str:>5}  {r['sortino']:>7.2f}  {r['sharpe']:>7.2f}"
            f"{flag}"
        )

    log()
    log(f"  PnL comparativo:")
    for name, r in results.items():
        b    = bar(r["pnl"], max_abs_pnl)
        sign = "+" if r["pnl"] >= 0 else "-"
        log(f"  {name[2:]:<14} {sign}│{b:<20}│ {fmt_pnl(r['pnl'])}")
    log()

    best = max(results.items(), key=lambda x: x[1]["pnl"])
    log(f"  🏆 Miglior strategia sul test set: {best[0]} (PnL {fmt_pnl(best[1]['pnl'])})")
    log()

    # ── Analisi per confidence bucket su TEST set ─────────────────────────────
    log(f"  Confidence bucket su TEST set (con calibration check):")
    log(f"  {'Bucket':<13} {'N':>4}  {'ExpWR':>7}  {'ActWR':>7}  {'Delta':>7}  {'PnL':>10}  {'$/bet':>8}")
    log(f"  " + "─"*60)
    bins = [0.50, 0.55, 0.60, 0.62, 0.65, 0.70, 1.01]
    conf_buckets_json = []
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i+1]
        sub = df_test[(df_test["confidence"] >= lo) & (df_test["confidence"] < hi)]
        if len(sub) == 0:
            continue
        exp_wr  = sub["confidence"].mean() * 100
        act_wr  = sub["correct"].mean()    * 100
        delta   = act_wr - exp_wr
        pnl_b   = sub["pnl_usd"].sum()
        ppb_b   = pnl_b / len(sub)
        delta_s = f"+{delta:.1f}%" if delta >= 0 else f"{delta:.1f}%"
        log(f"  [{lo:.2f},{hi:.2f})  {len(sub):>4}  {exp_wr:>6.1f}%  {act_wr:>6.1f}%  {delta_s:>7}  {fmt_pnl(pnl_b):>10}  {fmt_pnl(ppb_b):>8}")
        conf_buckets_json.append({
            "bucket": f"[{lo:.2f},{hi:.2f})",
            "n": len(sub), "exp_wr": round(exp_wr, 1), "act_wr": round(act_wr, 1),
            "delta": round(delta, 1), "pnl": round(float(pnl_b), 4), "pnl_per_bet": round(float(ppb_b), 4),
        })
    log()

    # ── Analisi per ora UTC su TEST set (tutte le 24h) ────────────────────────
    log(f"  WR per ora UTC su TEST set (tutte le 24h):")
    log(f"  {'Ora':>4}  {'N':>4}  {'WR':>6}  {'PnL':>9}  {'Note'}")
    log(f"  " + "─"*44)
    hourly_json = []
    for h in range(24):
        sub = df_test[df_test["hour_utc"] == h]
        n_h = len(sub)
        if n_h == 0:
            log(f"  {h:>4}h     —      —%        —")
            hourly_json.append({"hour": h, "n": 0, "wr": None, "pnl": 0.0, "is_dead": False, "is_hot": False, "low_sample": True})
            continue
        wr_h  = sub["correct"].mean() * 100
        pnl_h = float(sub["pnl_usd"].sum())
        is_dead = h in dead_hours
        is_hot  = wr_h >= 60 and n_h >= 3
        low_s   = n_h < 3
        note = "💀 DEAD" if is_dead else ("✅ HOT" if is_hot else ("* few" if low_s else ""))
        log(f"  {h:>4}h  {n_h:>4}  {wr_h:>5.1f}%  {fmt_pnl(pnl_h):>9}  {note}")
        hourly_json.append({
            "hour": h, "n": n_h, "wr": round(wr_h, 1), "pnl": round(pnl_h, 4),
            "is_dead": is_dead, "is_hot": is_hot, "low_sample": low_s,
        })
    log()

    # ── Streak analysis ───────────────────────────────────────────────────────
    log(f"  Streak analysis (BASELINE, cronologico):")
    streaks = streak_analysis(df_test)
    log(f"  Max win streak: {streaks['max_win_streak']}  |  Max loss streak: {streaks['max_loss_streak']}")
    log()

    # ── Note metodologiche ────────────────────────────────────────────────────
    log(f"  Note:")
    log(f"  • Train: {len(df_train)} bet, Test: {len(df_test)} bet (split {args.train_ratio:.0%}/{1-args.train_ratio:.0%} cronologico)")
    log(f"  • pnl_usd da Supabase include entry+exit fee per bet recenti")
    log(f"  • XGB addestrato SOLO su train set (no data leakage)")
    log(f"  • Dead hours calcolate SOLO da train set")
    log(f"  • Bet skippate contano come $0 (no gain, no loss)")
    log(f"  • Soglia confidence: {conf_thr}")
    log(f"  • PF = Profit Factor (gross_profit / gross_loss). PF > 1.5 = buono")
    log(f"  • Sortino penalizza solo downside volatility (vs Sharpe che penalizza tutto)")
    if xgb_cv_acc:
        log(f"  • XGB CV accuracy su train: {xgb_cv_acc:.1%}")
    log()

    n_test = len(df_test)
    if n_test < 40:
        log(f"  ⚠️  ATTENZIONE: test set di soli {n_test} bet. Risultati non statisticamente robusti.")
        log(f"     Servono almeno 100 bet in test per conclusioni affidabili.")
    log()
    log(f"  Generated: {ts}")

    # ── Salva report TXT ──────────────────────────────────────────────────────
    rep_path = os.path.join(args.output_dir, "backtest_report.txt")
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n  Report TXT salvato: {rep_path}")

    # ── Salva JSON strutturato per dashboard ──────────────────────────────────
    strategies_json = {}
    for name, r in results.items():
        d = dict(r)
        d.pop("equity_curve", None)  # escludi dal summary
        strategies_json[name] = d

    equity_curves = {name: results[name]["equity_curve"] for name in results}

    data_json = {
        "generated_at":  ts,
        "n_total":       n_total,
        "train_n":       len(df_train),
        "test_n":        len(df_test),
        "train_wr":      round(train_wr, 1),
        "test_wr":       round(test_wr, 1),
        "period_start":  df_test["created_at"].iloc[0][:10],
        "period_end":    df_test["created_at"].iloc[-1][:10],
        "dead_hours":    sorted(dead_hours),
        "xgb_cv_acc":    round(xgb_cv_acc, 4) if xgb_cv_acc else None,
        "conf_threshold": conf_thr,
        "best_strategy": best[0],
        "best_pnl":      best[1]["pnl"],
        "current_strategy": "F_FULL_STACK",
        "strategies":    strategies_json,
        "equity_curves": equity_curves,
        "confidence_buckets": conf_buckets_json,
        "hourly":        hourly_json,
        "streaks":       streaks,
    }

    class _NpEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, (np.bool_,)):
                return bool(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    json_path = os.path.join(args.output_dir, "backtest_data.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data_json, f, indent=2, cls=_NpEncoder)
    print(f"  Report JSON salvato: {json_path}")


if __name__ == "__main__":
    main()
