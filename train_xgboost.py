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
import glob
import json
import math
import pickle
import os
import sys
import datetime
import shutil

import requests
import pandas as pd
import numpy as np
from xgboost import XGBClassifier
from sklearn.model_selection import TimeSeriesSplit, cross_val_score
from sklearn.metrics import classification_report, confusion_matrix, accuracy_score
from constants import XGB_PARAMS
from sklearn.preprocessing import LabelEncoder

# ─── Features usate per la predizione ─────────────────────────────────────────
# Nota: hour_utc NON è incluso direttamente.
# Viene sostituito da hour_sin e hour_cos (encoding ciclico).
# Questo cattura la natura circolare del tempo: l'ora 23 è vicina all'ora 0.
# hour_utc rimane nel CSV e viene usato solo per la sezione di analisi/reporting.
#
# CVD (cvd_6m_pct): feature opzionale — inclusa automaticamente se la colonna
# è presente nel CSV con almeno l'80% dei valori non-nulli.
# Per generarla: python build_dataset.py --cvd
FEATURE_COLS = [
    "confidence",
    "fear_greed_value",
    "rsi14",
    "technical_score",
    # Encoding ciclico dell'ora UTC (sostituisce hour_utc intero)
    "hour_sin",   # sin(2π * hour_utc / 24)
    "hour_cos",   # cos(2π * hour_utc / 24)
    # Ordinale -2→+2: strong_bearish=-2, mild_bearish/bearish=-1,
    # neutral=0, mild_bullish/bullish=+1, strong_bullish=+2.
    # Sostituisce technical_bias_bullish (binary) — info loss ridotto.
    "technical_bias_score",
    # 1 se fear_greed_value < 45 (Fear/Extreme Fear), derivato dal valore
    # numerico in build_dataset.py (non dal testo LLM, che era inaffidabile).
    "signal_fg_fear",
    # T-01: Giorno della settimana — encoding ciclico
    # I mercati crypto hanno pattern settimanali (es. dump del lunedì,
    # rally del venerdì pre-weekend). Encoding ciclico: dom(6)≈lun(0).
    "dow_sin",    # sin(2π * day_of_week / 7)
    "dow_cos",    # cos(2π * day_of_week / 7)
    # T-01: Sessione di trading — 0=Asia, 1=London, 2=NY
    # Cattura il regime di liquidità: London+NY = alta liquidità, direzionalità.
    "session",
    # RIMOSSI: ema_trend_up, signal_technical_buy, signal_sentiment_pos,
    # signal_volume_high — 0% importance su 422 segnali → costanti/skewed
]

# Feature opzionali: aggiunte dinamicamente in prepare_features() se disponibili
# nel CSV e con copertura sufficiente (>= 80% non-null).
OPTIONAL_FEATURE_COLS = [
    # CVD proxy: pressione netta acquisto/vendita ultime 6 candele 1m Binance.
    # Range: -100 (tutto vendita) → +100 (tutto acquisto).
    # Generato da: python build_dataset.py --cvd
    "cvd_6m_pct",
    # P1: Regime di mercato 4h — 0=RANGING, 1=TRENDING, 2=VOLATILE
    # Calcolato su ATR(14) 4h normalizzato + trend strength EMA5/EMA20.
    # Generato da: python build_dataset.py --regime
    # La feature più predittiva per il context-switching della confidence.
    "regime_label",
]

# XGB_PARAMS importato da constants.py — unica sorgente di verità.


# ─── Helpers ──────────────────────────────────────────────────────────────────
def now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def print_section(title: str):
    print(f"\n{'='*55}")
    print(f"  {title}")
    print(f"{'='*55}")


class Reporter:
    """Stampa su stdout e accumula righe per il file di report."""
    def __init__(self):
        self.lines: list = [
            f"Generated: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}"
        ]

    def __call__(self, s: str = ""):
        print(s)
        self.lines.append(s)


def load_data(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    print(f"[{now()}] Dataset caricato: {len(df)} righe, {len(df.columns)} colonne")
    print(f"           Colonne: {list(df.columns)}")
    return df


def feature_importance_table(model, feature_names: list) -> str:
    scores = model.feature_importances_
    pairs = sorted(zip(feature_names, scores), key=lambda x: -x[1])
    lines = ["  Feature                    Importance"]
    lines.append("  " + "-"*38)
    for name, score in pairs:
        bar = "█" * int(score * 40)
        lines.append(f"  {name:<26} {score:.4f}  {bar}")
    return "\n".join(lines)


# ─── Feature engineering ──────────────────────────────────────────────────────
def prepare_features(df: pd.DataFrame, log: Reporter) -> tuple:
    """Aggiunge feature derivate, risolve opzionali e filtra righe incomplete.

    Ritorna (df_clean, active_feature_cols).
    """
    if "hour_sin" not in df.columns or "hour_cos" not in df.columns:
        df["hour_sin"] = df["hour_utc"].apply(lambda h: math.sin(2 * math.pi * h / 24))
        df["hour_cos"] = df["hour_utc"].apply(lambda h: math.cos(2 * math.pi * h / 24))
        log(f"[{now()}] hour_sin/hour_cos calcolati on-the-fly da hour_utc")
    else:
        log(f"[{now()}] hour_sin/hour_cos trovati nel CSV — nessun ricalcolo")

    active_cols = list(FEATURE_COLS)
    for col in OPTIONAL_FEATURE_COLS:
        if col not in df.columns:
            log(f"[{now()}] Feature opzionale '{col}' non presente nel CSV — saltata")
            continue
        df[col] = pd.to_numeric(df[col], errors="coerce")
        coverage = df[col].notna().mean()
        if coverage >= 0.80:
            active_cols.append(col)
            log(f"[{now()}] Feature opzionale '{col}' inclusa ({coverage:.1%} copertura)")
        else:
            log(f"[{now()}] Feature opzionale '{col}' saltata ({coverage:.1%} copertura < 80%)")

    df["direction_bin"] = (df["direction"] == "UP").astype(int)
    df_clean = df.dropna(subset=active_cols + ["label", "direction_bin"])
    log(f"[{now()}] Righe valide dopo dropna: {len(df_clean)}/{len(df)}")
    return df_clean, active_cols


# ─── Training ─────────────────────────────────────────────────────────────────
def train_and_eval(X, y, label_name: str) -> dict:
    """Addestra XGBoost con 5-fold TimeSeriesSplit CV e ritorna metriche.

    OBBLIGATORIO: TimeSeriesSplit invece di StratifiedKFold per dati finanziari.
    StratifiedKFold esegue shuffle casuale → il fold di test può contenere dati
    più vecchi del training → look-ahead bias indiretto → metriche CV gonfiate.
    TimeSeriesSplit garantisce che il test set sia sempre cronologicamente DOPO
    il training set — replica la realtà operativa del bot.
    """
    model = XGBClassifier(**XGB_PARAMS)

    # TimeSeriesSplit: test set sempre successivo al training set — niente look-ahead bias
    cv = TimeSeriesSplit(n_splits=5)
    cv_acc = cross_val_score(model, X, y, cv=cv, scoring="accuracy")
    cv_auc = cross_val_score(model, X, y, cv=cv, scoring="roc_auc")

    model.fit(X, y)
    train_acc = accuracy_score(y, model.predict(X))
    # T-02: train accuracy su dati di training è sempre ~1.000 (overfitted) — ignorare.
    # Usare CV Accuracy sopra o Walkforward Validation nella sezione T-02.
    marker = " ⚠️ (overfit)" if train_acc > 0.95 else ""

    print(f"\n  [{label_name}]")
    print(f"  CV Accuracy:  {cv_acc.mean():.3f} ± {cv_acc.std():.3f}")
    print(f"  CV AUC-ROC:   {cv_auc.mean():.3f} ± {cv_auc.std():.3f}")
    print(f"  Train Acc:    {train_acc:.3f}{marker}")

    return {"model": model, "cv_acc": cv_acc, "cv_auc": cv_auc}


# ─── Walkforward validation ───────────────────────────────────────────────────
def run_walkforward(df_clean: pd.DataFrame, feature_cols: list,
                    res_dir: dict, res_corr: dict, log: Reporter):
    """T-02: replica la realtà del trading — mai lookahead bias."""
    print_section("T-02 — WALKFORWARD VALIDATION (TimeSeriesSplit)")

    if "created_at" not in df_clean.columns:
        log("  ⚠️  Colonna 'created_at' non trovata nel CSV.")
        log("  Rigenera dataset: python build_dataset.py (include created_at da sessione T-01)")
        log("  T-02 saltato — shuffle CV usato come fallback.")
        return

    df_s   = df_clean.sort_values("created_at").reset_index(drop=True)
    X_wf   = df_s[feature_cols].values
    y_dir  = df_s["direction_bin"].values
    y_corr = df_s["label"].values

    wf_cv = TimeSeriesSplit(n_splits=5)
    wf_acc_dir  = cross_val_score(XGBClassifier(**XGB_PARAMS), X_wf, y_dir,  cv=wf_cv, scoring="accuracy")
    wf_auc_dir  = cross_val_score(XGBClassifier(**XGB_PARAMS), X_wf, y_dir,  cv=wf_cv, scoring="roc_auc")
    wf_acc_corr = cross_val_score(XGBClassifier(**XGB_PARAMS), X_wf, y_corr, cv=wf_cv, scoring="accuracy")
    wf_auc_corr = cross_val_score(XGBClassifier(**XGB_PARAMS), X_wf, y_corr, cv=wf_cv, scoring="roc_auc")

    gap_dir  = res_dir["cv_acc"].mean()  - wf_acc_dir.mean()
    gap_corr = res_corr["cv_acc"].mean() - wf_acc_corr.mean()

    log(f"\n  {'Metrica':<30} {'Shuffle':>9}  {'Walkforward':>11}  {'Gap':>8}")
    log("  " + "-"*63)
    log(f"  {'Direction   Accuracy':<30} {res_dir['cv_acc'].mean():>9.3f}  {wf_acc_dir.mean():>11.3f}  {gap_dir:>+8.3f}")
    log(f"  {'Direction   AUC-ROC':<30} {res_dir['cv_auc'].mean():>9.3f}  {wf_auc_dir.mean():>11.3f}")
    log(f"  {'Correctness Accuracy':<30} {res_corr['cv_acc'].mean():>9.3f}  {wf_acc_corr.mean():>11.3f}  {gap_corr:>+8.3f}")
    log(f"  {'Correctness AUC-ROC':<30} {res_corr['cv_auc'].mean():>9.3f}  {wf_auc_corr.mean():>11.3f}")
    log("")

    for name, gap in [("Direction", gap_dir), ("Correctness", gap_corr)]:
        if gap > 0.10:
            log(f"  ⚠️  {name} gap {gap:+.1%} > 10% — overfitting temporale rilevato")
        elif gap > 0.05:
            log(f"  🟡 {name} gap {gap:+.1%} 5-10% — controllare periodicità del retrain")
        else:
            log(f"  ✅ {name} gap {gap:+.1%} ≤ 5% — generalizzazione temporale OK")

    log(f"\n  Righe ordinate per created_at: {len(df_s)}")
    log(f"  Fold size approssimativo: train ~{len(df_s)//6}  test ~{len(df_s)//6}")


# ─── Analisi ──────────────────────────────────────────────────────────────────
def analyze_confidence(df_clean: pd.DataFrame, log: Reporter):
    print_section("ANALISI: Confidence LLM vs Win Rate reale")
    log("  Binning per confidence — mostra se confidence è calibrata:")
    log(f"  {'Bucket':<12} {'N':>4}  {'WinRate':>8}  {'AvgConf':>9}")
    log("  " + "-"*40)
    bins = [0.50, 0.55, 0.60, 0.62, 0.65, 0.70, 0.80, 1.01]
    for lo, hi in zip(bins, bins[1:]):
        sub = df_clean[(df_clean["confidence"] >= lo) & (df_clean["confidence"] < hi)]
        if len(sub) == 0:
            continue
        log(f"  [{lo:.2f},{hi:.2f})  {len(sub):>4}  {sub['label'].mean()*100:>7.1f}%  {sub['confidence'].mean():>9.3f}")


def analyze_hourly(df_clean: pd.DataFrame, log: Reporter):
    print_section("ANALISI: Ora UTC vs Win Rate")
    log(f"  {'Ora':>4}  {'N':>4}  {'WinRate':>8}  {'Direzione':>10}")
    log("  " + "-"*35)
    for h in range(24):
        sub = df_clean[df_clean["hour_utc"] == h]
        if len(sub) < 3:
            continue
        log(f"  {h:>4}h  {len(sub):>4}  {sub['label'].mean()*100:>7.1f}%  {sub['direction_bin'].mean()*100:>8.1f}%UP")


def print_insights(res_dir: dict, res_corr: dict, y_dir, log: Reporter):
    print_section("INSIGHT")
    dir_cv  = res_dir["cv_acc"].mean()
    corr_cv = res_corr["cv_acc"].mean()

    if dir_cv > 0.55:
        log(f"  ✅ Direction model accuracy {dir_cv:.1%} > 55% — le features hanno potere predittivo")
    else:
        log(f"  ⚠️  Direction model accuracy {dir_cv:.1%} ≤ 55% — vicino al random")

    if corr_cv > 0.55:
        log(f"  ✅ Correctness model accuracy {corr_cv:.1%} > 55% — XGB può filtrare segnali LLM")
    else:
        log(f"  ⚠️  Correctness model accuracy {corr_cv:.1%} ≤ 55% — LLM non più prevedibile delle features")

    down_pct = (1 - y_dir.mean()) * 100
    if down_pct > 65:
        log(f"  ⚠️  Forte bias DOWN ({down_pct:.0f}%) — dataset sbilanciato per mercato ribassista")

    log(f"\n  Prossimi step:")
    log(f"  1. Integra correctness_model in /bet-sizing per ridurre size se XGB=WRONG")
    log(f"  2. Usa direction_model come secondo voto: bet solo se LLM+XGB concordano")
    log(f"  3. Rigenera dataset ogni 2 settimane con build_dataset.py")


# ─── Feature Importance ───────────────────────────────────────────────────────
def save_feature_importance(res_dir: dict, res_corr: dict,
                             feature_names: list, output_dir: str, log: Reporter):
    """
    Task 5.2: Salva feature importance in datasets/feature_importance.json.
    Logga le feature con importance < 1% (candidate per rimozione).
    Tenta di salvare un plot PNG se matplotlib è disponibile.
    """
    dir_scores  = res_dir["model"].feature_importances_
    corr_scores = res_corr["model"].feature_importances_

    # Build dicts sorted by importance descending
    dir_imp  = {f: round(float(s), 6) for f, s in
                sorted(zip(feature_names, dir_scores),  key=lambda x: -x[1])}
    corr_imp = {f: round(float(s), 6) for f, s in
                sorted(zip(feature_names, corr_scores), key=lambda x: -x[1])}

    fi_data = {
        "generated_at": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_features": len(feature_names),
        "direction_model": dir_imp,
        "correctness_model": corr_imp,
    }
    fi_path = os.path.join(output_dir, "feature_importance.json")
    with open(fi_path, "w", encoding="utf-8") as f:
        json.dump(fi_data, f, indent=2)
    log(f"[{now()}] Feature importance salvata → {fi_path}")

    # Log features con importance < 1% per entrambi i modelli
    low_dir  = [f for f, s in dir_imp.items()  if s < 0.01]
    low_corr = [f for f, s in corr_imp.items() if s < 0.01]
    if low_dir:
        log(f"  ⚠️  Direction  — importance < 1%: {low_dir}")
    if low_corr:
        log(f"  ⚠️  Correctness — importance < 1%: {low_corr}")

    # Plot top-15 (matplotlib opzionale)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(1, 2, figsize=(14, 6))
        for ax, imp_dict, title in [
            (axes[0], dir_imp,  "Direction Model — Feature Importance (gain)"),
            (axes[1], corr_imp, "Correctness Model — Feature Importance (gain)"),
        ]:
            top15 = list(imp_dict.items())[:15]
            names  = [x[0] for x in top15][::-1]
            scores = [x[1] for x in top15][::-1]
            ax.barh(names, scores, color="#4C72B0")
            ax.set_xlabel("Importance (gain)")
            ax.set_title(title, fontsize=10)
            ax.tick_params(labelsize=8)

        plt.tight_layout()
        plot_path = os.path.join(output_dir, "feature_importance.png")
        plt.savefig(plot_path, dpi=120, bbox_inches="tight")
        plt.close()
        log(f"[{now()}] Feature importance plot → {plot_path}")
    except ImportError:
        log(f"[{now()}] matplotlib non disponibile — plot saltato")
    except Exception as e:
        log(f"[{now()}] Plot feature importance fallito: {e}")


# ─── Model Versioning ─────────────────────────────────────────────────────────
_MODELS_DIR = "./models"
_ARCHIVE_MAX = 10


def _archive_model(src_path: str, archive_dir: str, stamp: str, basename: str):
    """Copia src_path in archive_dir con timestamp nel nome. Mantiene max _ARCHIVE_MAX versioni."""
    os.makedirs(archive_dir, exist_ok=True)
    stem, ext = os.path.splitext(basename)
    dst = os.path.join(archive_dir, f"{stem}_{stamp}{ext}")
    shutil.copy2(src_path, dst)

    # Elimina le versioni più vecchie oltre il limite
    pattern = os.path.join(archive_dir, f"{stem}_*{ext}")
    versions = sorted(glob.glob(pattern))
    while len(versions) > _ARCHIVE_MAX:
        os.remove(versions.pop(0))


def save_model_metadata(n_samples: int, feature_names: list,
                         res_dir: dict, res_corr: dict, stamp: str):
    """Task 5.3: Scrive models/model_metadata.json."""
    os.makedirs(_MODELS_DIR, exist_ok=True)
    metadata = {
        "model": "xgb_direction + xgb_correctness",
        "trained_at": stamp,
        "n_samples": n_samples,
        "features": feature_names,
        "n_features": len(feature_names),
        "direction_cv_accuracy":  round(float(res_dir["cv_acc"].mean()),  4),
        "direction_cv_auc":       round(float(res_dir["cv_auc"].mean()),  4),
        "correctness_cv_accuracy": round(float(res_corr["cv_acc"].mean()), 4),
        "correctness_cv_auc":      round(float(res_corr["cv_auc"].mean()), 4),
    }
    meta_path = os.path.join(_MODELS_DIR, "model_metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    return meta_path


# ─── Save ─────────────────────────────────────────────────────────────────────
def save_models(res_dir: dict, res_corr: dict, report: Reporter,
                output_dir: str, n_samples: int = 0, feature_names: list = None):
    dir_path  = os.path.join(output_dir, "xgb_direction.pkl")
    corr_path = os.path.join(output_dir, "xgb_correctness.pkl")
    rep_path  = os.path.join(output_dir, "xgb_report.txt")

    with open(dir_path,  "wb") as f: pickle.dump(res_dir["model"],  f)
    with open(corr_path, "wb") as f: pickle.dump(res_corr["model"], f)
    with open(rep_path,  "w",  encoding="utf-8") as f: f.write("\n".join(report.lines))

    print(f"\n[{now()}] Salvati:")
    print(f"  {dir_path}")
    print(f"  {corr_path}")
    print(f"  {rep_path}")

    # ── Task 5.3: Model versioning ─────────────────────────────────────────────
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    archive_dir = os.path.join(_MODELS_DIR, "archive")

    # Copia i modelli live in models/ e in models/archive/ con timestamp
    for src, basename in [(dir_path, "xgb_direction.pkl"),
                          (corr_path, "xgb_correctness.pkl")]:
        live_dst = os.path.join(_MODELS_DIR, basename)
        shutil.copy2(src, live_dst)
        _archive_model(src, archive_dir, stamp, basename)

    meta_path = save_model_metadata(
        n_samples=n_samples,
        feature_names=feature_names or [],
        res_dir=res_dir,
        res_corr=res_corr,
        stamp=stamp,
    )
    print(f"  {meta_path}")
    print(f"  {archive_dir}/*_{stamp}.pkl (archivio versione)")


# ─── Telegram notification ────────────────────────────────────────────────────
def _notify_channel_retrain(n_samples, win_rate, dir_cv_acc, dir_cv_auc,
                             corr_cv_acc, corr_cv_auc):
    """Invia un post storytelling bilingue (EN + IT) al channel @BTCPredictorBot."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        print("[retrain-notify] TELEGRAM_BOT_TOKEN non impostata — skip notifica")
        return

    text = (
        "🐙 The bot doesn't stop. Not even to learn — it learns <i>while</i> running.\n\n"
        "🧠 <b>Weekly retraining complete.</b>\n"
        f"📍 {n_samples} historical signals analyzed. {win_rate*100:.1f}% win rate on record.\n\n"
        f"🎩 Two models. One goal: only bet when the edge is real.\n"
        f"• Direction accuracy: <code>{dir_cv_acc:.1%}</code>\n"
        f"• Signal quality filter: <code>{corr_cv_acc:.1%}</code>\n\n"
        "🪄 Every future prediction runs on this updated brain.\n"
        "Not magic. Architecture.\n\n"
        "🤑 Every model update — public. Every number — verifiable.\n"
        "This is what building in public looks like. 🚀\n\n"
        "━━━━━━━━━━━━━━━━\n\n"
        "🐙 Il bot non si ferma. Nemmeno per imparare — impara <i>mentre</i> gira.\n\n"
        "🧠 <b>Retraining settimanale completato.</b>\n"
        f"📍 {n_samples} segnali storici analizzati. {win_rate*100:.1f}% win rate registrato.\n\n"
        f"🎩 Due modelli. Un obiettivo: scommettere solo quando l'edge è reale.\n"
        f"• Accuratezza direzionale: <code>{dir_cv_acc:.1%}</code>\n"
        f"• Filtro qualità segnale: <code>{corr_cv_acc:.1%}</code>\n\n"
        "🪄 Ogni previsione futura gira su questo cervello aggiornato.\n"
        "Non è magia. È sistema.\n\n"
        "🤑 Ogni aggiornamento del modello — pubblico. Ogni numero — verificabile.\n"
        "Costruire in pubblico significa questo. 🚀\n\n"
        "#BuildInPublic #AlgoTrading #BTC #AI 🐙🎩🪄"
    )

    try:
        requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": "@BTCPredictorBot", "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        print("[retrain-notify] Post channel inviato ✅")
    except Exception as e:
        print(f"[retrain-notify] Errore invio Telegram: {e}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="./datasets/features.csv")
    parser.add_argument("--output-dir", default="./datasets")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    log = Reporter()

    df = load_data(args.data)
    df_clean, active_cols = prepare_features(df, log)

    X      = df_clean[active_cols].values
    y_corr = df_clean["label"].values
    y_dir  = df_clean["direction_bin"].values

    # ── Dataset stats ──────────────────────────────────────────────────────────
    print_section("DATASET STATS")
    log(f"  Totale righe:   {len(df_clean)}")
    log(f"  WIN rate:       {y_corr.mean()*100:.1f}%")
    log(f"  UP rate:        {y_dir.mean()*100:.1f}%  (DOWN: {(1-y_dir.mean())*100:.1f}%)")
    log(f"  Confidence avg: {df_clean['confidence'].mean():.3f}")
    log(f"  RSI14 avg:      {df_clean['rsi14'].mean():.1f}")
    log(f"  Tech score avg: {df_clean['technical_score'].mean():.2f}")

    # ── Modello 1: Predice DIREZIONE ───────────────────────────────────────────
    print_section("MODELLO 1 — Predice DIREZIONE (UP/DOWN)")
    log("  Impara a predire la direzione BTC dai dati di mercato,")
    log("  indipendentemente dal segnale LLM.")
    res_dir = train_and_eval(X, y_dir, "Direction Model")
    log("\n  Feature Importance (direzione):")
    log(feature_importance_table(res_dir["model"], active_cols))
    y_pred_dir = res_dir["model"].predict(X)
    log("\n  Classification Report:")
    log(classification_report(y_dir, y_pred_dir, target_names=["DOWN", "UP"], digits=3))
    cm = confusion_matrix(y_dir, y_pred_dir)
    log(f"  Confusion Matrix (DOWN/UP):")
    log(f"    Pred→  DOWN   UP")
    log(f"    DOWN   {cm[0,0]:4d}  {cm[0,1]:4d}")
    log(f"    UP     {cm[1,0]:4d}  {cm[1,1]:4d}")

    # ── Modello 2: Predice CORRETTEZZA segnale LLM ────────────────────────────
    print_section("MODELLO 2 — Predice CORRETTEZZA segnale LLM")
    log("  Dati le stesse features, il segnale LLM sarà corretto?")
    log("  Utile come filtro di qualità: bet solo se XGB dice 'correct'.")
    res_corr = train_and_eval(X, y_corr, "Correctness Model")
    log("\n  Feature Importance (correttezza):")
    log(feature_importance_table(res_corr["model"], active_cols))
    y_pred_corr = res_corr["model"].predict(X)
    log("\n  Classification Report:")
    log(classification_report(y_corr, y_pred_corr, target_names=["WRONG", "CORRECT"], digits=3))

    # ── Walkforward + analisi + insight ───────────────────────────────────────
    run_walkforward(df_clean, active_cols, res_dir, res_corr, log)
    analyze_confidence(df_clean, log)
    analyze_hourly(df_clean, log)
    print_insights(res_dir, res_corr, y_dir, log)

    # ── Task 5.2: Feature importance JSON + plot ───────────────────────────────
    save_feature_importance(res_dir, res_corr, active_cols, args.output_dir, log)

    # ── Save + notify ──────────────────────────────────────────────────────────
    save_models(res_dir, res_corr, log, args.output_dir,
                n_samples=len(df_clean), feature_names=active_cols)
    _notify_channel_retrain(
        n_samples=len(df_clean),
        win_rate=y_corr.mean(),
        dir_cv_acc=res_dir["cv_acc"].mean(),
        dir_cv_auc=res_dir["cv_auc"].mean(),
        corr_cv_acc=res_corr["cv_acc"].mean(),
        corr_cv_auc=res_corr["cv_auc"].mean(),
    )


if __name__ == "__main__":
    main()
