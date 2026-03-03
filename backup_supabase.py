#!/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
"""
Backup notturno Supabase → CSV locale.
Eseguito da launchd com.btcbot.backup_supabase ogni notte alle 02:00.
"""
import os
import sys
import csv
import glob
import json
import ssl
import urllib.request
from datetime import datetime

import certifi as _certifi
_SSL_CTX = ssl.create_default_context(cafile=_certifi.where())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "btc_predictions")
BACKUP_DIR = os.path.expanduser("~/btc_predictions/datasets")
LOG_FILE = "/tmp/btcbot_backup.log"

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_KEY must be set")
    sys.exit(1)

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")

def fetch_all_rows():
    """Fetch all rows with pagination (1000 per page)."""
    all_rows = []
    offset = 0
    page_size = 1000
    while True:
        url = (f"{SUPABASE_URL}/rest/v1/{SUPABASE_TABLE}"
               f"?select=*&order=id.asc&limit={page_size}&offset={offset}")
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=30) as resp:
            page = json.loads(resp.read().decode())
        if not page:
            break
        all_rows.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return all_rows

def save_csv(rows, filepath):
    if not rows:
        log("WARNING: no rows returned")
        return 0
    # Collect all field names across all rows for consistency
    fieldnames = list(dict.fromkeys(k for row in rows for k in row.keys()))
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
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

        # Verify file was written correctly
        if count > 0 and os.path.getsize(filepath) < 100:
            log(f"WARNING: file suspiciously small ({os.path.getsize(filepath)} bytes)")

        # Mantieni solo ultimi 30 backup
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
