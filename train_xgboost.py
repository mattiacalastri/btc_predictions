#!/usr/bin/env python3
"""
train_xgboost.py — BTC Prediction Bot: XGBoost direction predictor

Addestra due modelli:
  1. direction_model — predice UP/DOWN dai dati di mercato
  2. correctness_model — predice se il segnale LLM sarà corretto

Output:
  ./datasets/xgb_direction.pkl   — modello direzione
  ./datasets/xgb_correctness.pkl — modello correttezza
  ./datasets/xgb_report.txt      — report completo

Usage:
  python3 train_xgboost.py [--data ./datasets/features.csv]
"""

import argparse
import pickle
import os
import csv
from datetime import datetime

import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score
from sklearn.metrics import (
    classification_report, confusion_matrix,
    accuracy_score, roc_auc_score
)
from sklearn.preprocessing import LabelEncoder

# ─── Features usate per la predizione ─────────────────────────────────────────
FEATURE_COLS = [
    "confidence",
    "fear_greed_value",
    "rsi14",
    "technical_score",
    "hour_utc",
    "ema_trend_up",
    "technical_bias_bullish",
    "signal_technical_buy",
    "signal_sentiment_pos",
    "signal_fg_fear",
    "signal_volume_high",
]

# ─── Helpers ──────────────────────────────────────────────────────────────────
def load_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    print(f"[{now()}] Dataset caricato: {len(df)} righe, {len(df.columns)} colonne")
    print(f"           Colonne: {list(df.columns)}")
    return df

def now():
    return datetime.now().strftime("%H:%M:%S")

def print_section(title: str):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")

def train_and_eval(X, y, label_name: str, pos_label=None) -> dict:
    """Addestra XGBoost con 5-fold CV e ritorna metriche."""
    model = XGBClassifier(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        use_label_encoder=False,
        eval_metric="logloss",
        random_state=42,
        verbosity=0,
    )

    # 5-fold stratified CV
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_acc = cross_val_score(model, X, y, cv=cv, scoring="accuracy")
    cv_auc = cross_val_score(model, X, y, cv=cv, scoring="roc_auc")

    # Train finale su tutto il dataset
    model.fit(X, y)

    y_pred = model.predict(X)
    y_prob = model.predict_proba(X)[:, 1]

    print(f"\n  [{label_name}]")
    print(f"  CV Accuracy:  {cv_acc.mean():.3f} ± {cv_acc.std():.3f}")
    print(f"  CV AUC-ROC:   {cv_auc.mean():.3f} ± {cv_auc.std():.3f}")
    print(f"  Train Acc:    {accuracy_score(y, y_pred):.3f}")

    return {"model": model, "cv_acc": cv_acc, "cv_auc": cv_auc}


def feature_importance_table(model, feature_names: list) -> str:
    scores = model.feature_importances_
    pairs = sorted(zip(feature_names, scores), key=lambda x: -x[1])
    lines = ["  Feature                    Importance"]
    lines.append("  " + "-"*38)
    for name, score in pairs:
        bar = "█" * int(score * 40)
        lines.append(f"  {name:<26} {score:.4f}  {bar}")
    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="./datasets/features.csv")
    parser.add_argument("--output-dir", default="./datasets")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    report_lines = []

    def log(s=""):
        print(s)
        report_lines.append(s)

    # ── Carica dati ──────────────────────────────────────────────────────────
    df = load_data(args.data)

    # Encode direction: UP=1, DOWN=0
    df["direction_bin"] = (df["direction"] == "UP").astype(int)

    # Filtra righe con features complete
    df_clean = df.dropna(subset=FEATURE_COLS + ["label", "direction_bin"])
    log(f"[{now()}] Righe valide dopo dropna: {len(df_clean)}/{len(df)}")

    X = df_clean[FEATURE_COLS].values
    y_label = df_clean["label"].values          # correct=1, wrong=0
    y_dir   = df_clean["direction_bin"].values  # UP=1, DOWN=0

    # ── Stats dataset ─────────────────────────────────────────────────────────
    print_section("DATASET STATS")
    log(f"  Totale righe:   {len(df_clean)}")
    log(f"  WIN rate:       {y_label.mean()*100:.1f}%")
    log(f"  UP rate:        {y_dir.mean()*100:.1f}%  (DOWN: {(1-y_dir.mean())*100:.1f}%)")
    log(f"  Confidence avg: {df_clean['confidence'].mean():.3f}")
    log(f"  RSI14 avg:      {df_clean['rsi14'].mean():.1f}")
    log(f"  Tech score avg: {df_clean['technical_score'].mean():.2f}")

    # ── Modello 1: Predice DIREZIONE ──────────────────────────────────────────
    print_section("MODELLO 1 — Predice DIREZIONE (UP/DOWN)")
    log("  Impara a predire la direzione BTC dai dati di mercato,")
    log("  indipendentemente dal segnale LLM.")
    res_dir = train_and_eval(X, y_dir, "Direction Model")

    log("\n  Feature Importance (direzione):")
    log(feature_importance_table(res_dir["model"], FEATURE_COLS))

    # Classification report
    y_pred_dir = res_dir["model"].predict(X)
    log("\n  Classification Report:")
    log(classification_report(
        y_dir, y_pred_dir,
        target_names=["DOWN", "UP"],
        digits=3
    ))

    # Confusion matrix
    cm = confusion_matrix(y_dir, y_pred_dir)
    log(f"  Confusion Matrix (DOWN/UP):")
    log(f"    Pred→  DOWN   UP")
    log(f"    DOWN   {cm[0,0]:4d}  {cm[0,1]:4d}")
    log(f"    UP     {cm[1,0]:4d}  {cm[1,1]:4d}")

    # ── Modello 2: Predice CORRETTEZZA del segnale LLM ───────────────────────
    print_section("MODELLO 2 — Predice CORRETTEZZA segnale LLM")
    log("  Dati le stesse features, il segnale LLM sarà corretto?")
    log("  Utile come filtro di qualità: bet solo se XGB dice 'correct'.")
    res_corr = train_and_eval(X, y_label, "Correctness Model")

    log("\n  Feature Importance (correttezza):")
    log(feature_importance_table(res_corr["model"], FEATURE_COLS))

    y_pred_corr = res_corr["model"].predict(X)
    log("\n  Classification Report:")
    log(classification_report(
        y_label, y_pred_corr,
        target_names=["WRONG", "CORRECT"],
        digits=3
    ))

    # ── Analisi: Confidence LLM vs correttezza reale ──────────────────────────
    print_section("ANALISI: Confidence LLM vs Win Rate reale")
    log("  Binning per confidence — mostra se confidence è calibrata:")
    log(f"  {'Bucket':<12} {'N':>4}  {'WinRate':>8}  {'AvgConf':>9}")
    log("  " + "-"*40)
    bins = [0.50, 0.55, 0.60, 0.62, 0.65, 0.70, 0.80, 1.01]
    for i in range(len(bins)-1):
        lo, hi = bins[i], bins[i+1]
        mask = (df_clean["confidence"] >= lo) & (df_clean["confidence"] < hi)
        sub = df_clean[mask]
        if len(sub) == 0:
            continue
        wr = sub["label"].mean() * 100
        ac = sub["confidence"].mean()
        log(f"  [{lo:.2f},{hi:.2f})  {len(sub):>4}  {wr:>7.1f}%  {ac:>9.3f}")

    # ── Analisi: Ora UTC vs Win Rate ──────────────────────────────────────────
    print_section("ANALISI: Ora UTC vs Win Rate")
    log(f"  {'Ora':>4}  {'N':>4}  {'WinRate':>8}  {'Direzione':>10}")
    log("  " + "-"*35)
    for h in range(24):
        sub = df_clean[df_clean["hour_utc"] == h]
        if len(sub) < 3:
            continue
        wr = sub["label"].mean() * 100
        up_pct = sub["direction_bin"].mean() * 100
        log(f"  {h:>4}h  {len(sub):>4}  {wr:>7.1f}%  {up_pct:>8.1f}%UP")

    # ── Insight chiave ────────────────────────────────────────────────────────
    print_section("INSIGHT")
    dir_cv = res_dir["cv_acc"].mean()
    corr_cv = res_corr["cv_acc"].mean()

    if dir_cv > 0.55:
        log(f"  ✅ Direction model accuracy {dir_cv:.1%} > 55% — le features hanno potere predittivo")
    else:
        log(f"  ⚠️  Direction model accuracy {dir_cv:.1%} ≤ 55% — vicino al random")

    if corr_cv > 0.55:
        log(f"  ✅ Correctness model accuracy {corr_cv:.1%} > 55% — XGB può filtrare segnali LLM")
    else:
        log(f"  ⚠️  Correctness model accuracy {corr_cv:.1%} ≤ 55% — LLM non più prevedibile delle features")

    # Bias DOWN
    down_pct = (1 - y_dir.mean()) * 100
    if down_pct > 65:
        log(f"  ⚠️  Forte bias DOWN ({down_pct:.0f}%) — dataset sbilanciato per mercato ribassista")

    log(f"\n  Prossimi step:")
    log(f"  1. Integra correctness_model in /bet-sizing per ridurre size se XGB=WRONG")
    log(f"  2. Usa direction_model come secondo voto: bet solo se LLM+XGB concordano")
    log(f"  3. Rigenera dataset ogni 2 settimane con build_dataset.py")

    # ── Salva modelli ─────────────────────────────────────────────────────────
    dir_path  = os.path.join(args.output_dir, "xgb_direction.pkl")
    corr_path = os.path.join(args.output_dir, "xgb_correctness.pkl")
    rep_path  = os.path.join(args.output_dir, "xgb_report.txt")

    with open(dir_path, "wb")  as f: pickle.dump(res_dir["model"],  f)
    with open(corr_path, "wb") as f: pickle.dump(res_corr["model"], f)
    with open(rep_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))

    print(f"\n[{now()}] Salvati:")
    print(f"  {dir_path}")
    print(f"  {corr_path}")
    print(f"  {rep_path}")

if __name__ == "__main__":
    main()
