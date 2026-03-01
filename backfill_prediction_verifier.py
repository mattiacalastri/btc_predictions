#!/usr/bin/env python3
"""
Backfill ghost_correct for historical SKIP signals.

Aligns with the gold standard in app.py /ghost-evaluate:
  - Exit price = Binance BTCUSDT close at T+30min from signal creation
  - Correct = exit_price > signal_price (UP) or exit_price < signal_price (DOWN)
  - Updates: ghost_correct, ghost_exit_price, ghost_evaluated_at,
             correct, btc_price_exit, pnl_pct, actual_direction

After running, `build_dataset.py --include-ghost` will find all backfilled rows.

Usage:
    SUPABASE_URL=... SUPABASE_KEY=... python backfill_prediction_verifier.py [--dry-run] [--limit N]
"""

import argparse
import os
import sys
import time
import requests
from datetime import datetime, timezone, timedelta

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY env vars required")
    sys.exit(1)

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

EXIT_DELAY_MINUTES = 30  # gold standard: T+30min (same as /ghost-evaluate)


def get_pending_rows(limit: int = 5000) -> list:
    """Fetch ghost signals not yet evaluated (ghost_evaluated_at IS NULL)."""
    all_rows = []
    page_size = 1000
    offset = 0
    while len(all_rows) < limit:
        batch = min(page_size, limit - len(all_rows))
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/btc_predictions",
            headers=HEADERS,
            params={
                "select": "id,direction,signal_price,btc_price_entry,created_at,classification",
                "bet_taken": "eq.false",
                "ghost_evaluated_at": "is.null",
                "signal_price": "not.is.null",
                "order": "created_at.asc",
                "limit": batch,
                "offset": offset,
            },
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
        all_rows.extend(rows)
        if len(rows) < batch:
            break
        offset += batch
    return all_rows


def get_binance_price(ts_ms: int) -> float | None:
    """Get BTCUSDT close price from Binance 1m kline at given timestamp."""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1m", "startTime": ts_ms, "limit": 1},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data and len(data) > 0:
            return float(data[0][4])  # close price
    except Exception as e:
        print(f"  [WARN] Binance error: {e}")
    return None


def parse_ts(ts_str: str) -> datetime:
    """Parse Supabase timestamp to UTC datetime."""
    ts_str = ts_str.replace(" ", "T")
    if ts_str.endswith("+00"):
        ts_str += ":00"
    return datetime.fromisoformat(ts_str)


def update_row(row_id: int, payload: dict):
    """PATCH a single row in Supabase."""
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/btc_predictions",
        headers=HEADERS,
        params={"id": f"eq.{row_id}"},
        json=payload,
        timeout=8,
    )
    r.raise_for_status()


def main():
    parser = argparse.ArgumentParser(description="Backfill ghost_correct on historical SKIP signals")
    parser.add_argument("--dry-run", action="store_true", help="Calculate but don't write to Supabase")
    parser.add_argument("--limit", type=int, default=5000, help="Max rows to process (default 5000)")
    args = parser.parse_args()

    now_utc = datetime.now(timezone.utc)
    # Only evaluate signals where T+30min is in the past
    cutoff = now_utc - timedelta(minutes=EXIT_DELAY_MINUTES + 5)

    rows = get_pending_rows(limit=args.limit)
    # Filter out rows too recent
    rows = [r for r in rows if parse_ts(r["created_at"]) < cutoff]

    print(f"Righe da backfillare: {len(rows)}")
    if args.dry_run:
        print("  (DRY RUN — nessuna scrittura su Supabase)")
    print()

    updated = 0
    skipped = 0
    errors = 0
    eval_ts = now_utc.isoformat()

    for i, row in enumerate(rows):
        row_id = row["id"]
        direction = (row.get("direction") or "").upper()
        signal_price = row.get("signal_price")

        if direction not in ("UP", "DOWN") or signal_price is None:
            skipped += 1
            continue

        try:
            sp = float(signal_price)
        except (TypeError, ValueError):
            skipped += 1
            continue

        if sp <= 0:
            print(f"  [SKIP] id={row_id}: signal_price={sp} invalido")
            skipped += 1
            continue

        created_at = parse_ts(row["created_at"])
        exit_time = created_at + timedelta(minutes=EXIT_DELAY_MINUTES)
        exit_ts_ms = int(exit_time.timestamp() * 1000)

        exit_price = get_binance_price(exit_ts_ms)
        if exit_price is None:
            print(f"  [ERROR] id={row_id}: Binance price unavailable at {exit_time.isoformat()}")
            errors += 1
            time.sleep(0.5)
            continue

        ghost_correct = (exit_price > sp) if direction == "UP" else (exit_price < sp)
        pnl_pct = ((exit_price - sp) / sp) if direction == "UP" else ((sp - exit_price) / sp)
        actual_direction = direction if ghost_correct else ("DOWN" if direction == "UP" else "UP")

        tag = "WIN" if ghost_correct else "LOSS"
        print(
            f"  [{tag}] id={row_id} | {direction} | "
            f"entry={sp:.2f} exit={exit_price:.2f} pnl={pnl_pct:+.4%} | "
            f"{row.get('classification', '?')}"
        )

        if not args.dry_run:
            try:
                update_row(row_id, {
                    "ghost_exit_price": round(exit_price, 2),
                    "ghost_correct": ghost_correct,
                    "ghost_evaluated_at": eval_ts,
                    "correct": ghost_correct,
                    "btc_price_exit": round(exit_price, 2),
                    "pnl_pct": round(pnl_pct if ghost_correct else -abs(pnl_pct), 6),
                    "actual_direction": actual_direction,
                })
                updated += 1
            except Exception as e:
                print(f"  [ERROR] id={row_id}: Supabase update failed: {e}")
                errors += 1

        # Binance rate limit: 1200 req/min → 0.05s per request is safe
        if (i + 1) % 100 == 0:
            print(f"  ... {i + 1}/{len(rows)} processed ({updated} updated, {errors} errors)")
        time.sleep(0.1)

    print(f"\n{'=' * 50}")
    print(f"BACKFILL {'(DRY RUN) ' if args.dry_run else ''}COMPLETATO")
    print(f"  Processate: {updated + skipped + errors}")
    print(f"  Aggiornate: {updated}")
    print(f"  Skippate:   {skipped}")
    print(f"  Errori:     {errors}")
    if updated > 0 and not args.dry_run:
        print(f"\nProssimo step:")
        print(f"  python build_dataset.py --include-ghost")
        print(f"  python train_xgboost.py --data datasets/features.csv")


if __name__ == "__main__":
    main()
