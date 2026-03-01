#!/usr/bin/env python3
"""
Backup notturno Supabase → CSV locale.
Eseguito da launchd com.btcbot.backup_supabase ogni notte alle 02:00.
"""
import os
import sys
import csv
import json
import ssl
import urllib.request
import urllib.error
from datetime import datetime

import certifi as _certifi
_SSL_CTX = ssl.create_default_context(cafile=_certifi.where())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://oimlamjilivrcnhztwvj.supabase.co").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
BACKUP_DIR = os.path.expanduser("~/btc_predictions/datasets")
LOG_FILE = "/tmp/btcbot_backup.log"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def fetch_all_rows():
    url = f"{SUPABASE_URL}/rest/v1/btc_predictions?select=*&order=id.asc&limit=10000"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=30) as resp:
        return json.loads(resp.read().decode())

def save_csv(rows, filepath):
    if not rows:
        log("WARNING: no rows returned")
        return 0
    fieldnames = list(rows[0].keys())
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)

def main():
    log("=== BTC Bot — Supabase backup start ===")
    os.makedirs(BACKUP_DIR, exist_ok=True)

    date_str = datetime.now().strftime("%Y%m%d")
    filepath = os.path.join(BACKUP_DIR, f"backup_{date_str}.csv")

    try:
        rows = fetch_all_rows()
        count = save_csv(rows, filepath)
        log(f"OK: {count} rows saved to {filepath}")

        # Mantieni solo ultimi 30 backup
        import glob
        backups = sorted(glob.glob(os.path.join(BACKUP_DIR, "backup_*.csv")))
        for old in backups[:-30]:
            os.remove(old)
            log(f"Removed old backup: {old}")

    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)

    log("=== Backup complete ===")

if __name__ == "__main__":
    main()
