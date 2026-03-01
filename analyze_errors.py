#!/usr/bin/env python3
"""
analyze_errors.py — BTC Prediction Bot: Error Pattern Analyzer

Legge tutti i bet chiusi da Supabase, identifica cluster sistematici di errore
e genera:
  1. datasets/error_patterns.json — dati completi per dashboard/debug
  2. prompt_snippet — testo compatto da iniettare nel prompt Claude via
     /performance-stats (appended automaticamente da app.py)

Usage:
  python analyze_errors.py

Env vars (stessi di app.py):
  SUPABASE_URL, SUPABASE_KEY
"""

import os
import json
import ssl
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
MIN_SAMPLE = 8          # campioni minimi per rilevanza statistica
BAD_WR     = 0.42       # soglia "cluster negativo"
GOOD_WR    = 0.63       # soglia "cluster positivo"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "datasets")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "error_patterns.json")

# SSL context with proper CA verification
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()


# ── Supabase fetch ────────────────────────────────────────────────────────────
def _fetch_bets():
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
    if not sb_url or not sb_key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_KEY non impostati")

    fields = "id,created_at,direction,confidence,correct,pnl_usd,rsi14,fear_greed_value,technical_score"
    query  = "bet_taken=eq.true&correct=not.is.null&order=id.asc&limit=2000"
    url    = f"{sb_url}/rest/v1/btc_predictions?select={fields}&{query}"

    req = urllib.request.Request(url, headers={
        "apikey":        sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type":  "application/json",
    })
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
        return json.loads(resp.read().decode())


# ── Helpers ───────────────────────────────────────────────────────────────────
def _hour(row):
    """Estrae ora UTC da hour_utc (se presente) o da created_at."""
    h = row.get("hour_utc")
    if h is not None:
        try:
            return int(h)
        except (TypeError, ValueError):
            pass
    ts = row.get("created_at", "")
    try:
        return int(ts[11:13])
    except (IndexError, ValueError):
        return None


def _wr(bets):
    if not bets:
        return None
    return sum(1 for b in bets if b.get("correct") is True) / len(bets)


def _pnl(bets):
    return sum(float(b.get("pnl_usd") or 0) for b in bets)


def _pattern(ptype, label, bets, note=None):
    n   = len(bets)
    wr  = _wr(bets)
    pnl = _pnl(bets)
    if wr is None:
        return None
    severity = (
        "bad"  if wr < BAD_WR  else
        "good" if wr > GOOD_WR else
        "neutral"
    )
    p = {
        "type":     ptype,
        "label":    label,
        "n":        n,
        "wr":       round(wr, 3),
        "wr_pct":   round(wr * 100, 1),
        "pnl":      round(pnl, 4),
        "severity": severity,
    }
    if note:
        p["note"] = note
    return p


# ── Analisi dimensioni ────────────────────────────────────────────────────────
def _by_hour(bets):
    buckets = {}
    for b in bets:
        h = _hour(b)
        if h is None:
            continue
        buckets.setdefault(h, []).append(b)

    results = []
    for h, group in sorted(buckets.items()):
        if len(group) < MIN_SAMPLE:
            continue
        p = _pattern("hour", f"Ora {h:02d}h UTC", group)
        if p and p["severity"] != "neutral":
            results.append(p)
    return results


def _by_confidence(bets):
    ranges = [
        ((0.60, 0.65), "[0.60–0.65)"),
        ((0.65, 0.70), "[0.65–0.70)"),
        ((0.70, 0.75), "[0.70–0.75)"),
        ((0.75, 1.01), "[0.75+]"),
    ]
    results = []
    for (lo, hi), label in ranges:
        group = [b for b in bets if lo <= float(b.get("confidence") or 0) < hi]
        if len(group) < MIN_SAMPLE:
            continue
        p = _pattern("confidence", f"Conf {label}", group)
        if p and p["severity"] != "neutral":
            results.append(p)
    return results


def _by_direction(bets):
    results = []
    for direction in ("UP", "DOWN"):
        group = [b for b in bets if b.get("direction") == direction]
        if len(group) < MIN_SAMPLE:
            continue
        p = _pattern("direction", f"Direzione {direction}", group)
        if p:
            results.append(p)
    return results


def _by_rsi(bets):
    ranges = [
        ((0,  30),  "RSI oversold <30",       "evita SHORT in oversold"),
        ((30, 50),  "RSI bearish 30–50",       None),
        ((50, 70),  "RSI neutro 50–70",        None),
        ((70, 100), "RSI overbought >70",      "evita LONG in overbought"),
    ]
    results = []
    for (lo, hi), label, note in ranges:
        group = [b for b in bets
                 if b.get("rsi14") is not None
                 and lo <= float(b["rsi14"]) < hi]
        if len(group) < MIN_SAMPLE:
            continue
        p = _pattern("rsi", label, group, note)
        if p and p["severity"] != "neutral":
            results.append(p)
    return results


def _by_fear_greed(bets):
    ranges = [
        ((0,  25),  "F&G Extreme Fear (0–25)"),
        ((25, 45),  "F&G Fear (25–45)"),
        ((45, 55),  "F&G Neutral (45–55)"),
        ((55, 75),  "F&G Greed (55–75)"),
        ((75, 101), "F&G Extreme Greed (75+)"),
    ]
    results = []
    for (lo, hi), label in ranges:
        group = [b for b in bets
                 if b.get("fear_greed_value") is not None
                 and lo <= float(b["fear_greed_value"]) < hi]
        if len(group) < MIN_SAMPLE:
            continue
        p = _pattern("fear_greed", label, group)
        if p and p["severity"] != "neutral":
            results.append(p)
    return results


def _by_technical_score(bets):
    """Analisi per technical_score — cattura l'inversione confidence/WR."""
    ranges = [
        ((0.0, 0.3), "TechScore basso (0–0.3)"),
        ((0.3, 0.5), "TechScore medio-basso (0.3–0.5)"),
        ((0.5, 0.7), "TechScore medio-alto (0.5–0.7)"),
        ((0.7, 1.01), "TechScore alto (0.7+)", "alta convergenza tecnica = ingresso tardivo?"),
    ]
    results = []
    for entry in ranges:
        (lo, hi), label = entry[0], entry[1]
        note = entry[2] if len(entry) > 2 else None
        group = [b for b in bets
                 if b.get("technical_score") is not None
                 and lo <= float(b["technical_score"]) < hi]
        if len(group) < MIN_SAMPLE:
            continue
        p = _pattern("technical_score", label, group, note)
        if p and p["severity"] != "neutral":
            results.append(p)
    return results


def _by_combined(bets):
    """Pattern combinati — cattura value traps e cluster ad alto rischio."""
    results = []

    # Value trap: UP + high tech_score + extreme F&G
    value_trap = [
        b for b in bets
        if b.get("direction") == "UP"
        and b.get("technical_score") is not None
        and float(b["technical_score"]) >= 0.6
        and b.get("fear_greed_value") is not None
        and (float(b["fear_greed_value"]) < 25 or float(b["fear_greed_value"]) > 75)
    ]
    if len(value_trap) >= MIN_SAMPLE:
        p = _pattern("combined", "UP+HighTech+ExtremeFG (value trap)", value_trap,
                      "alta convergenza in regime estremo = dead cat bounce?")
        if p:
            results.append(p)

    # High confidence + wrong direction
    high_conf_wrong = [
        b for b in bets
        if b.get("confidence") is not None
        and float(b.get("confidence", 0)) >= 0.70
        and b.get("correct") is False
    ]
    if len(high_conf_wrong) >= max(3, MIN_SAMPLE // 2):
        p = _pattern("combined", "HighConf>=0.70 + WRONG", high_conf_wrong,
                      "overconfidence — modello sicuro ma sbagliato")
        if p:
            results.append(p)

    return results


# ── Prompt snippet ────────────────────────────────────────────────────────────
def _build_snippet(worst, best, total, overall_wr):
    lines = [f"📊 CALIBRAZIONE LIVE ({total} bet chiuse, WR globale {overall_wr:.0%}):"]

    if worst:
        lines.append("⚠️ Pattern negativi — EVITARE:")
        for p in worst[:4]:
            note = f" ({p['note']})" if p.get("note") else ""
            lines.append(
                f"  • {p['label']}: WR {p['wr_pct']}% su {p['n']} bet, PnL {p['pnl']:+.2f}${note}"
            )

    if best:
        lines.append("✅ Pattern positivi — FAVORIRE:")
        for p in best[:3]:
            lines.append(
                f"  • {p['label']}: WR {p['wr_pct']}% su {p['n']} bet, PnL {p['pnl']:+.2f}$"
            )

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("[analyze_errors] Fetching bets from Supabase...")
    bets = _fetch_bets()
    print(f"[analyze_errors] {len(bets)} bet loaded")

    if len(bets) < MIN_SAMPLE:
        print("[analyze_errors] Insufficient data — exiting")
        return

    overall_wr = _wr(bets)

    all_patterns = (
        _by_hour(bets)
        + _by_confidence(bets)
        + _by_direction(bets)
        + _by_rsi(bets)
        + _by_fear_greed(bets)
        + _by_technical_score(bets)
        + _by_combined(bets)
    )

    worst = sorted(
        [p for p in all_patterns if p["severity"] == "bad"],
        key=lambda x: x["wr"]
    )
    best = sorted(
        [p for p in all_patterns if p["severity"] == "good"],
        key=lambda x: -x["wr"]
    )

    snippet = _build_snippet(worst, best, len(bets), overall_wr)

    output = {
        "generated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "total_bets":    len(bets),
        "overall_wr":    round(overall_wr, 4),
        "worst_patterns": worst,
        "best_patterns":  best,
        "all_patterns":   all_patterns,
        "prompt_snippet": snippet,
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"[analyze_errors] Saved → {OUTPUT_FILE}")
    print(f"[analyze_errors] Worst patterns: {len(worst)}, Best: {len(best)}")
    print("\n--- SNIPPET ---")
    print(snippet)

    return output


if __name__ == "__main__":
    main()
