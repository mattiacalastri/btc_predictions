#!/usr/bin/env python3
"""
build_dataset.py — BTC Prediction Bot LLM Fine-tuning Dataset Builder

Legge tutte le predizioni risolte da Supabase (correct IS NOT NULL),
genera due output:
  1. train.jsonl / val.jsonl — OpenAI fine-tuning format (80/20 split)
  2. features.csv — feature matrix per XGBoost/scikit-learn

Usage:
  python build_dataset.py [--output-dir ./datasets] [--val-ratio 0.2]

Env vars (stessi di app.py):
  SUPABASE_URL, SUPABASE_KEY
"""

import os
import json
import csv
import math
import random
import argparse
import ssl
from datetime import datetime
import urllib.request
import urllib.parse

# macOS Python 3.11 manca dei certificati CA di sistema → bypass SSL verify
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# ─── Config ────────────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

SYSTEM_PROMPT = """You are an expert BTC futures trading analyst. Your job is to predict the short-term BTC price direction (next 6 minutes) based on technical indicators, market sentiment, and on-chain data.

Respond ONLY with a JSON object in this exact format:
{"direction": "UP" or "DOWN", "confidence": 0.50-1.00, "reasoning": "brief chain-of-thought explanation"}

Rules:
- direction: "UP" if price likely rises, "DOWN" if likely falls
- confidence: 0.50 (coin flip) to 1.00 (near certain). Only bet if ≥ 0.60.
- reasoning: 1-3 sentences explaining key factors
- Never add extra fields or text outside the JSON"""

OPPOSITE = {"UP": "DOWN", "DOWN": "UP"}

# Encoding ordinale di technical_bias: preserva la gradazione semantica.
# Range: -2 (forte ribassista) → 0 (neutro) → +2 (forte rialzista).
# Sostituisce il vecchio binary "bull" match che collassava neutral=bearish.
_BIAS_MAP = {
    "strong_bearish": -2,
    "mild_bearish":   -1,
    "bearish":        -1,
    "neutral":         0,
    "mild_bullish":    1,
    "bullish":         1,
    "strong_bullish":  2,
}

# Colonne numeriche per features.csv
NUMERIC_FEATURES = [
    "confidence", "btc_price_entry", "fear_greed_value",
    "rsi14", "technical_score",
]

# ─── Supabase REST helper ───────────────────────────────────────────────────────
def supabase_get(table: str, params: dict) -> list:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise EnvironmentError(
            "SUPABASE_URL e SUPABASE_KEY devono essere settate come variabili d'ambiente"
        )
    qs = urllib.parse.urlencode(params)
    url = f"{SUPABASE_URL}/rest/v1/{table}?{qs}"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "count=exact",
    })
    with urllib.request.urlopen(req, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


# ─── CVD (Cumulative Volume Delta) proxy — infrastruttura ──────────────────────
# Il CVD approssima la pressione di acquisto/vendita aggregando le ultime N
# candele 1-minuto di Binance.
#
# Ogni kline Binance ha:
#   indice 5  → volume totale (base asset)
#   indice 9  → taker_buy_base_vol (volume acquistato dai taker)
#
# Per ogni candela:
#   cvd_delta = taker_buy_vol - (total_vol - taker_buy_vol)
#             = 2 * taker_buy_vol - total_vol
#   (>0 = pressione di acquisto netta, <0 = pressione di vendita netta)
#
# Metrica finale:
#   cvd_6m_pct = sum(cvd_delta per ultime 6 candele) / sum(total_vol) * 100
#   Range tipico: -100 (tutto vendita) a +100 (tutto acquisto)
#
# NOTA: Questa funzione richiede una chiamata HTTP a Binance per ogni riga.
#       Per retroattivo su dataset storico usare --cvd flag (non implementato yet).
#       Per nuove predizioni live, il dato va incluso nel wf01 e passato a Supabase.

def fetch_cvd_6m(timestamp_ms: int) -> float | None:
    """
    Recupera le ultime 6 kline 1-minuto di Binance BTCUSDT fino a timestamp_ms
    e calcola cvd_6m_pct (CVD proxy normalizzato sul volume totale).

    Args:
        timestamp_ms: timestamp Unix in millisecondi del momento della predizione.

    Returns:
        cvd_6m_pct come float, oppure None in caso di errore/dati mancanti.
    """
    try:
        # Binance endTime = timestamp della predizione, ultime 8 kline (6 + buffer)
        params = urllib.parse.urlencode({
            "symbol": "BTCUSDT",
            "interval": "1m",
            "endTime": timestamp_ms,
            "limit": 8,
        })
        url = f"https://api.binance.com/api/v3/klines?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "btcbot/1.0"})
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=5) as resp:
            klines = json.loads(resp.read().decode())

        # Prende le ultime 6 kline complete (escludi l'eventuale kline aperta)
        klines = klines[-6:]
        if len(klines) < 6:
            return None

        total_vol_6m = 0.0
        cvd_sum = 0.0
        for k in klines:
            total_vol     = float(k[5])   # indice 5: volume totale
            taker_buy_vol = float(k[9])   # indice 9: taker buy base volume
            cvd_delta = 2.0 * taker_buy_vol - total_vol
            cvd_sum      += cvd_delta
            total_vol_6m += total_vol

        if total_vol_6m == 0:
            return None

        cvd_6m_pct = (cvd_sum / total_vol_6m) * 100.0
        return round(cvd_6m_pct, 4)

    except Exception:
        return None


def created_at_to_ms(created_at: str) -> int:
    """Converte 'created_at' ISO 8601 da Supabase in Unix timestamp milliseconds."""
    # Formato atteso: "2024-10-15T09:34:12.123456+00:00" oppure "...Z"
    ts = created_at.replace("Z", "+00:00")
    # Tronca i microsecondi a 6 cifre per compatibilità fromisoformat
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(ts)
        # Normalizza a UTC
        dt_utc = dt.astimezone(timezone.utc)
        return int(dt_utc.timestamp() * 1000)
    except Exception:
        return 0


def fetch_resolved_predictions() -> list:
    """Fetch all predictions with correct IS NOT NULL, ordered by created_at."""
    columns = ",".join([
        "id", "created_at", "direction", "confidence", "correct",
        "classification", "btc_price_entry",
        "fear_greed_value",
        "ema_trend", "rsi14", "technical_score", "technical_bias",
        "candle_pattern",
        "signal_technical", "signal_sentiment",
        "signal_fear_greed", "signal_volume",
        "reasoning",
    ])
    # Supabase supports up to 1000 rows per request; paginate if needed
    all_rows = []
    offset = 0
    page_size = 1000
    while True:
        rows = supabase_get("btc_predictions", {
            "select": columns,
            "correct": "not.is.null",
            "order": "created_at.asc",
            "limit": page_size,
            "offset": offset,
        })
        all_rows.extend(rows)
        if len(rows) < page_size:
            break
        offset += page_size
    return all_rows


# ─── Dataset builders ──────────────────────────────────────────────────────────
def build_user_message(row: dict) -> str:
    """Ricostruisce il contesto utente dai dati della predizione."""
    ts = row.get("created_at", "")[:16].replace("T", " ")
    hour = 0
    try:
        hour = int(row.get("created_at", "T00:")[11:13])
    except Exception:
        pass

    parts = [f"[BTC Prediction Request — {ts} UTC]", ""]

    # Prezzo
    if row.get("btc_price_entry"):
        parts.append(f"BTC Price: ${float(row['btc_price_entry']):,.0f}")

    # Fear & Greed
    fg_val = row.get("fear_greed_value")
    if fg_val is not None:
        parts.append(f"Fear & Greed Index: {fg_val}")

    # Indicatori tecnici
    ema = row.get("ema_trend", "")
    rsi = row.get("rsi14")
    tscore = row.get("technical_score")
    tbias = row.get("technical_bias", "")
    if ema:
        parts.append(f"EMA Trend: {ema}")
    if rsi is not None:
        parts.append(f"RSI 14: {float(rsi):.1f}")
    if tscore is not None:
        parts.append(f"Technical Score: {float(tscore):.2f} ({tbias})")
    if row.get("candle_pattern"):
        parts.append(f"Candle Pattern: {row['candle_pattern']}")

    # Segnali
    sigs = []
    for key, label in [
        ("signal_technical", "Technical"),
        ("signal_sentiment", "Sentiment"),
        ("signal_fear_greed", "Fear&Greed"),
        ("signal_volume", "Volume"),
    ]:
        val = row.get(key)
        if val:
            sigs.append(f"{label}={val}")
    if sigs:
        parts.append("Signals: " + ", ".join(sigs))

    # Ora del giorno
    session = "Asia" if 0 <= hour < 8 else ("London" if 8 <= hour < 14 else "NY")
    parts.append(f"Hour UTC: {hour:02d}:xx ({session} session)")

    parts.append("")
    parts.append("Predict the BTC price direction for the next 6 minutes.")

    return "\n".join(parts)


def build_assistant_message(row: dict, flip: bool) -> str:
    """Genera la risposta corretta dell'assistente."""
    original_dir = row.get("direction", "UP")
    original_conf = float(row.get("confidence") or 0.60)
    original_reasoning = (row.get("reasoning") or "").strip()

    if flip:
        # Predizione sbagliata → invertiamo la direzione
        correct_dir = OPPOSITE[original_dir]
        # Abbassa leggermente la confidence (la predizione era incerta)
        correct_conf = round(min(original_conf, 0.65), 2)
        reasoning = (
            f"[Corrected] The market moved {correct_dir} contrary to the original "
            f"{original_dir} prediction. "
            + (original_reasoning[:120] + "..." if len(original_reasoning) > 120 else original_reasoning)
        )
    else:
        correct_dir = original_dir
        correct_conf = round(original_conf, 2)
        reasoning = original_reasoning or f"Price moved {correct_dir} as predicted."

    result = {
        "direction": correct_dir,
        "confidence": correct_conf,
        "reasoning": reasoning[:300],  # tronca reasoning troppo lungo
    }
    return json.dumps(result, ensure_ascii=False)


def row_to_jsonl(row: dict) -> dict:
    """Converte una riga Supabase in un esempio JSONL OpenAI fine-tuning."""
    correct = row.get("correct")
    # correct: True = predizione giusta, False = sbagliata
    flip = not bool(correct)

    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(row)},
            {"role": "assistant", "content": build_assistant_message(row, flip)},
        ]
    }


def row_to_csv_dict(row: dict, cvd_6m_pct: float | None = None) -> dict:
    """
    Converte una riga Supabase in dizionario per features.csv (ML approach).

    Args:
        row:         riga raw da Supabase.
        cvd_6m_pct:  CVD proxy normalizzato (opzionale, calcolato da fetch_cvd_6m).
                     Se None, la colonna viene inclusa come stringa vuota.
    """
    # ── Ora UTC ────────────────────────────────────────────────────────────────
    hour = 0
    try:
        hour = int(row.get("created_at", "T00:")[11:13])
    except Exception:
        pass

    # Encoding ciclico dell'ora UTC.
    # Sin e cos catturano la natura circolare del tempo: ora 23 è vicina a 0.
    # hour_utc viene mantenuto per analisi e reporting (non usato come feature ML).
    hour_sin = math.sin(2 * math.pi * hour / 24)
    hour_cos = math.cos(2 * math.pi * hour / 24)

    # ── T-01: Giorno della settimana (0=Lunedì, 6=Domenica) ───────────────────
    # I mercati hanno pattern settimanali ben documentati (es. lunedì volatile,
    # venerdì con profit-taking). Encoding ciclico: venerdì (4) è vicino a sabato.
    dow = 0
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(
            row.get("created_at", "2020-01-01T00:00:00+00:00").replace("Z", "+00:00")
        )
        dow = dt.astimezone(timezone.utc).weekday()  # 0=Mon, 6=Sun
    except Exception:
        pass
    dow_sin = math.sin(2 * math.pi * dow / 7)
    dow_cos = math.cos(2 * math.pi * dow / 7)

    # ── T-01: Sessione di trading ──────────────────────────────────────────────
    # Asia (0-7 UTC): bassa liquidità, movimenti lenti
    # London (8-13 UTC): alta liquidità EUR/GBP, forte direzionalità
    # NY (14-23 UTC): massima liquidità, overlap con London in apertura
    session = 0 if hour < 8 else (1 if hour < 14 else 2)

    return {
        # Metadato temporale — usato da train_xgboost.py per ordinamento
        # cronologico (walk-forward CV, T-02). NON è una feature ML.
        "created_at": row.get("created_at", ""),
        # Target
        "label": 1 if row.get("correct") else 0,
        "direction": row.get("direction", ""),
        # Numeriche
        "confidence": float(row.get("confidence") or 0),
        "btc_price_entry": float(row.get("btc_price_entry") or 0),
        "fear_greed_value": float(row.get("fear_greed_value") or 50),
        "rsi14": float(row.get("rsi14") or 50),
        "technical_score": float(row.get("technical_score") or 0),
        # Ora UTC: intero grezzo (per reporting) + encoding ciclico (per ML)
        "hour_utc": hour,
        "hour_sin": round(hour_sin, 6),   # sin(2π * hour / 24)
        "hour_cos": round(hour_cos, 6),   # cos(2π * hour / 24)
        # T-01: Giorno settimana — encoding ciclico
        "day_of_week": dow,               # 0=Lun … 6=Dom (per reporting)
        "dow_sin": round(dow_sin, 6),     # sin(2π * dow / 7) — feature ML
        "dow_cos": round(dow_cos, 6),     # cos(2π * dow / 7) — feature ML
        # T-01: Sessione — 0=Asia, 1=London, 2=NY
        "session": session,
        # CVD proxy: pressione netta acquisto/vendita ultime 6 candele 1m.
        # Popolato solo se build_dataset.py eseguito con --cvd flag.
        # Range: da -100 (tutto vendita) a +100 (tutto acquisto).
        "cvd_6m_pct": cvd_6m_pct if cvd_6m_pct is not None else "",
        # Categoriche (encoded)
        "ema_trend_up": 1 if (row.get("ema_trend") or "").upper() == "UP" else 0,
        # Ordinale -2→+2: preserva gradazione bearish/neutral/bullish.
        # Sostituisce il vecchio binary 1/0 che trattava neutral = strong_bearish.
        "technical_bias_score": _BIAS_MAP.get((row.get("technical_bias") or "").lower().strip(), 0),
        "signal_technical_buy": 1 if (row.get("signal_technical") or "").upper() == "BUY" else 0,
        "signal_sentiment_pos": 1 if (row.get("signal_sentiment") or "").upper() in ("POSITIVE", "POS", "BUY") else 0,
        # Derivato dal valore numerico fear_greed_value (0-100), non dal testo LLM.
        # fear_greed_value < 45 = zona Fear/Extreme Fear (0-44). Più affidabile
        # del campo testuale signal_fear_greed che il LLM spesso inverte.
        "signal_fg_fear": 1 if float(row.get("fear_greed_value") or 50) < 45 else 0,
        "signal_volume_high": 1 if "high" in (row.get("signal_volume") or "").lower() else 0,
        "classification": row.get("classification", ""),
    }


# ─── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Build LLM fine-tuning dataset from Supabase")
    parser.add_argument("--output-dir", default="./datasets", help="Directory output (default: ./datasets)")
    parser.add_argument("--val-ratio", type=float, default=0.2, help="Validation split ratio (default: 0.2)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument(
        "--cvd", action="store_true",
        help=(
            "Fetch CVD proxy da Binance per ogni riga (richiede rete, ~1 req/riga). "
            "Aggiunge colonna cvd_6m_pct al features.csv. "
            "Disabilitato di default per compatibilità con dataset esistenti."
        ),
    )
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    random.seed(args.seed)

    print(f"[{datetime.now():%H:%M:%S}] Fetching resolved predictions da Supabase...")
    rows = fetch_resolved_predictions()

    if not rows:
        print("Nessuna predizione trovata. Controlla SUPABASE_URL e SUPABASE_KEY.")
        return

    total = len(rows)
    wins = sum(1 for r in rows if r.get("correct") is True)
    losses = sum(1 for r in rows if r.get("correct") is False)
    print(f"[{datetime.now():%H:%M:%S}] Trovate {total} predizioni risolte: {wins} WIN, {losses} LOSS")

    # ── Bilanciamento UP/DOWN via undersampling ────────────────────────────────
    up_rows   = [r for r in rows if r.get("direction") == "UP"]
    down_rows = [r for r in rows if r.get("direction") == "DOWN"]
    n_min = min(len(up_rows), len(down_rows))
    print(f"[{datetime.now():%H:%M:%S}] Bilanciamento: {len(up_rows)} UP, {len(down_rows)} DOWN → {n_min} per classe")
    up_balanced   = random.sample(up_rows,   n_min)
    down_balanced = random.sample(down_rows, n_min)
    rows = up_balanced + down_balanced
    random.shuffle(rows)
    total = len(rows)
    wins   = sum(1 for r in rows if r.get("correct") is True)
    losses = sum(1 for r in rows if r.get("correct") is False)
    print(f"[{datetime.now():%H:%M:%S}] Dataset bilanciato: {total} esempi ({n_min} UP + {n_min} DOWN)")

    # ── JSONL per fine-tuning ──────────────────────────────────────────────────
    jsonl_rows = [row_to_jsonl(r) for r in rows]

    # Split casuale 80/20 (mantenendo l'ordine cronologico → shuffle poi split)
    indices = list(range(len(jsonl_rows)))
    random.shuffle(indices)
    split = int(len(indices) * (1 - args.val_ratio))
    train_idx = sorted(indices[:split])
    val_idx = sorted(indices[split:])

    train_path = os.path.join(args.output_dir, "train.jsonl")
    val_path = os.path.join(args.output_dir, "val.jsonl")

    with open(train_path, "w", encoding="utf-8") as f:
        for i in train_idx:
            f.write(json.dumps(jsonl_rows[i], ensure_ascii=False) + "\n")
    print(f"[{datetime.now():%H:%M:%S}] Salvato {train_path} ({len(train_idx)} esempi)")

    with open(val_path, "w", encoding="utf-8") as f:
        for i in val_idx:
            f.write(json.dumps(jsonl_rows[i], ensure_ascii=False) + "\n")
    print(f"[{datetime.now():%H:%M:%S}] Salvato {val_path} ({len(val_idx)} esempi)")

    # ── CSV per ML ────────────────────────────────────────────────────────────
    # Se --cvd è abilitato, recupera il CVD proxy da Binance per ogni riga.
    # Ogni chiamata è ~1 req HTTP; su 400 righe impiega ~20-60s.
    if args.cvd:
        print(f"[{datetime.now():%H:%M:%S}] --cvd attivo: fetching CVD da Binance per {len(rows)} righe...")
        cvd_map: dict[str, float | None] = {}
        for i, r in enumerate(rows):
            ts_ms = created_at_to_ms(r.get("created_at", ""))
            cvd_val = fetch_cvd_6m(ts_ms) if ts_ms else None
            cvd_map[r.get("id", str(i))] = cvd_val
            if (i + 1) % 50 == 0:
                filled = sum(1 for v in cvd_map.values() if v is not None)
                print(f"  {i+1}/{len(rows)} — {filled} CVD ok")
        filled_total = sum(1 for v in cvd_map.values() if v is not None)
        print(f"[{datetime.now():%H:%M:%S}] CVD fetch completato: {filled_total}/{len(rows)} righe con dato")
        csv_rows = [row_to_csv_dict(r, cvd_6m_pct=cvd_map.get(r.get("id", ""), None)) for r in rows]
    else:
        # CVD non richiesto: colonna inclusa ma vuota (compatibilità forward)
        csv_rows = [row_to_csv_dict(r) for r in rows]

    csv_path = os.path.join(args.output_dir, "features.csv")
    if csv_rows:
        fieldnames = list(csv_rows[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"[{datetime.now():%H:%M:%S}] Salvato {csv_path} ({len(csv_rows)} righe, {len(fieldnames)} colonne)")

    # ── Stats ──────────────────────────────────────────────────────────────────
    print()
    print("=" * 50)
    print("DATASET STATS")
    print("=" * 50)
    print(f"  Totale esempi:   {total}")
    print(f"  Train:           {len(train_idx)}")
    print(f"  Validation:      {len(val_idx)}")
    print(f"  WIN rate:        {wins/total*100:.1f}%")
    print(f"  LOSS rate:       {losses/total*100:.1f}%")
    print()
    # Distribuzione direzioni
    ups = sum(1 for r in rows if r.get("direction") == "UP")
    downs = total - ups
    print(f"  Direzioni — UP: {ups} ({ups/total*100:.1f}%), DOWN: {downs} ({downs/total*100:.1f}%)")
    print()
    print("NEXT STEPS:")
    print("  1. Upload su OpenAI fine-tuning:")
    print(f"     openai api fine_tuning.jobs.create \\")
    print(f"       --training-file {train_path} \\")
    print(f"       --validation-file {val_path} \\")
    print(f"       --model gpt-4o-mini-2024-07-18")
    print()
    print("  2. Oppure usa features.csv con XGBoost:")
    print(f"     python train_xgboost.py --data {csv_path}")
    print()
    print("  3. Aggiorna MODEL_ID in app.py dopo il fine-tuning.")
    print("=" * 50)


if __name__ == "__main__":
    main()
