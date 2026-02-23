#!/usr/bin/env python3
"""
Backfill script: popola pnl_pct teorico per righe NO-BET con
actual_direction valorizzato ma pnl_pct NULL.

Formula: ((exit_price - entry_price) / entry_price) * 100 * direction_mult
  direction_mult = +1 se direction=UP, -1 se direction=DOWN
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://oimlamjilivrcnhztwvj.supabase.co")
SUPABASE_KEY = os.environ.get(
    "SUPABASE_KEY",
    "REDACTED_SUPABASE_ANON_KEY",
)
HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}
WAIT_MINUTES = 6


def get_rows():
    url = f"{SUPABASE_URL}/rest/v1/btc_predictions"
    params = {
        "select": "id,direction,btc_price_entry,signal_price,btc_price_exit,created_at",
        "bet_taken": "eq.false",
        "actual_direction": "not.is.null",
        "pnl_pct": "is.null",
        "order": "id.asc",
    }
    r = requests.get(url, headers=HEADERS, params=params)
    r.raise_for_status()
    return r.json()


def get_binance_price(ts_ms: int) -> float | None:
    url = "https://api.binance.com/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1m", "startTime": ts_ms, "limit": 1}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[0][4])  # close price
    except Exception as e:
        print(f"  [WARN] Binance error: {e}")
    return None


def update_pnl_pct(row_id: int, pnl_pct: float, btc_price_exit: float | None = None):
    url = f"{SUPABASE_URL}/rest/v1/btc_predictions"
    payload: dict = {"pnl_pct": round(pnl_pct, 6)}
    if btc_price_exit is not None:
        payload["btc_price_exit"] = round(btc_price_exit, 2)
    r = requests.patch(url, headers=HEADERS, params={"id": f"eq.{row_id}"}, json=payload)
    r.raise_for_status()


def parse_ts(ts_str: str) -> datetime:
    ts_str = ts_str.replace(" ", "T")
    if ts_str.endswith("+00"):
        ts_str += ":00"
    return datetime.fromisoformat(ts_str)


def main():
    rows = get_rows()
    print(f"Righe da backfillare pnl_pct: {len(rows)}")

    updated = skipped = errors = 0

    for row in rows:
        row_id = row["id"]
        direction = row["direction"]
        entry_price = float(row.get("btc_price_entry") or row.get("signal_price") or 0)

        if not entry_price:
            print(f"  [SKIP] id={row_id}: nessun entry price")
            skipped += 1
            continue

        # Se abbiamo già btc_price_exit usiamo quello, altrimenti fetch da Binance
        exit_price_raw = row.get("btc_price_exit")
        if exit_price_raw:
            exit_price = float(exit_price_raw)
            fetched = False
        else:
            created_at = parse_ts(row["created_at"])
            exit_time = created_at + timedelta(minutes=WAIT_MINUTES)
            if exit_time > datetime.now(timezone.utc):
                print(f"  [SKIP] id={row_id}: exit_time nel futuro")
                skipped += 1
                continue
            exit_ts_ms = int(exit_time.timestamp() * 1000)
            exit_price = get_binance_price(exit_ts_ms)
            fetched = True
            if exit_price is None:
                print(f"  [ERROR] id={row_id}: Binance non ha restituito prezzo")
                errors += 1
                time.sleep(0.5)
                continue

        direction_mult = 1.0 if direction == "UP" else -1.0
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0 * direction_mult

        print(
            f"  [OK] id={row_id} | entry={entry_price:.2f} exit={exit_price:.2f} "
            f"| dir={direction} pnl_pct={pnl_pct:+.4f}%"
            + (" [binance]" if fetched else " [cached]")
        )

        try:
            update_pnl_pct(row_id, pnl_pct, exit_price if fetched else None)
            updated += 1
        except Exception as e:
            print(f"  [ERROR] id={row_id}: update fallito: {e}")
            errors += 1

        time.sleep(0.15)

    print(f"\n=== BACKFILL pnl_pct COMPLETATO ===")
    print(f"Aggiornate: {updated}")
    print(f"Skippate:   {skipped}")
    print(f"Errori:     {errors}")


if __name__ == "__main__":
    main()
