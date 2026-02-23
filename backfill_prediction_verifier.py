#!/usr/bin/env python3
"""
Backfill script: popola correct/actual_direction/btc_price_exit
per tutte le righe NO-BET storiche (bet_taken=false, correct IS NULL).

Logica: usa il prezzo BTC su Binance 6 minuti dopo created_at.
"""

import time
import requests
from datetime import datetime, timezone, timedelta

SUPABASE_URL = "https://oimlamjilivrcnhztwvj.supabase.co"
SUPABASE_KEY = "REDACTED_SUPABASE_ANON_KEY"
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

WAIT_MINUTES = 6  # stesso delay del workflow 05


def get_rows():
    """Legge le righe da backfillare da Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/btc_predictions"
    params = {
        "select": "id,direction,btc_price_entry,signal_price,created_at,classification",
        "bet_taken": "eq.false",
        "correct": "is.null",
        "classification": "neq.PENDING",
        "order": "id.asc",
    }
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def get_binance_price(ts_ms: int) -> float | None:
    """Ottiene il prezzo BTC/USDT su Binance al timestamp ms dato."""
    url = "https://api.binance.com/api/v3/klines"
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "startTime": ts_ms,
        "limit": 1,
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data and len(data) > 0:
            # kline format: [openTime, open, high, low, close, ...]
            return float(data[0][4])  # close price
    except Exception as e:
        print(f"  [WARN] Binance error: {e}")
    return None


def update_row(row_id: int, actual_direction: str, correct: bool, btc_price_exit: float):
    """Aggiorna Supabase con i risultati calcolati."""
    url = f"{SUPABASE_URL}/rest/v1/btc_predictions"
    params = {"id": f"eq.{row_id}"}
    payload = {
        "actual_direction": actual_direction,
        "correct": correct,
        "btc_price_exit": round(btc_price_exit, 2),
    }
    r = requests.patch(url, headers=HEADERS, params=params, json=payload)
    r.raise_for_status()


def parse_created_at(ts_str: str) -> datetime:
    """Parsa il timestamp Supabase in datetime UTC."""
    # Formato: "2026-02-23 22:42:40.499249+00"
    ts_str = ts_str.replace(" ", "T")
    if ts_str.endswith("+00"):
        ts_str += ":00"
    return datetime.fromisoformat(ts_str)


def main():
    rows = get_rows()
    print(f"Righe da backfillare: {len(rows)}")

    updated = 0
    skipped = 0
    errors = 0

    for row in rows:
        row_id = row["id"]
        direction = row["direction"]
        entry_price = float(row.get("btc_price_entry") or row.get("signal_price") or 0)

        if not entry_price:
            print(f"  [SKIP] id={row_id}: nessun prezzo di ingresso")
            skipped += 1
            continue

        created_at = parse_created_at(row["created_at"])
        exit_time = created_at + timedelta(minutes=WAIT_MINUTES)

        # Non backfillare se l'exit_time è nel futuro
        now_utc = datetime.now(timezone.utc)
        if exit_time > now_utc:
            print(f"  [SKIP] id={row_id}: exit_time {exit_time.isoformat()} è nel futuro")
            skipped += 1
            continue

        exit_ts_ms = int(exit_time.timestamp() * 1000)
        exit_price = get_binance_price(exit_ts_ms)

        if exit_price is None:
            print(f"  [ERROR] id={row_id}: Binance non ha restituito prezzo per ts={exit_ts_ms}")
            errors += 1
            time.sleep(0.5)
            continue

        actual_direction = "UP" if exit_price > entry_price else "DOWN"
        correct = actual_direction == direction

        print(
            f"  [OK] id={row_id} | entry={entry_price:.2f} exit={exit_price:.2f} "
            f"| predicted={direction} actual={actual_direction} correct={correct} "
            f"| class={row.get('classification')}"
        )

        try:
            update_row(row_id, actual_direction, correct, exit_price)
            updated += 1
        except Exception as e:
            print(f"  [ERROR] id={row_id}: update fallito: {e}")
            errors += 1

        time.sleep(0.15)  # rispetta rate limit Binance (1200 req/min)

    print(f"\n=== BACKFILL COMPLETATO ===")
    print(f"Aggiornate: {updated}")
    print(f"Skippate:   {skipped}")
    print(f"Errori:     {errors}")


if __name__ == "__main__":
    main()
