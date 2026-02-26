#!/usr/bin/env python3
"""
Backup settimanale n8n workflows → repo privato GitHub.
Eseguito da launchd com.btcbot.n8n_backup ogni domenica alle 09:30.
"""
import os
import sys
import json
import ssl
import subprocess
import urllib.request
import urllib.error
from datetime import datetime

LOG_FILE = "/tmp/btcbot_n8n_backup.log"
BACKUP_DIR = os.path.expanduser("~/btcbot_backups/n8n-workflows/btc-bot-n8n-backup")

N8N_API_URL = "https://mattiacalastri.app.n8n.cloud"
N8N_API_KEY = os.environ.get(
    "N8N_API_KEY",
    "REDACTED_N8N_CLOUD_API_KEY"
)

# Lista workflow: (id, nome_file)
WORKFLOWS = [
    ("9oyKlb64lZIJfZYs", "00_Error_Notifier"),
    ("CARzC6ABuXmz7NHr", "01A_BTC_AI_Inputs"),
    ("kaevyOIbHpm8vJmF", "01B_BTC_Prediction_Bot"),
    ("vallzU6ceD5gPwSP", "02_BTC_Trade_Checker"),
    ("KITZHsfVSMtVTpfx", "03_BTC_Wallet_Checker"),
    ("eLmZ6d8t9slAx5pj", "04_BTC_Talker"),
    ("xCwf53UGBq1SyP0c", "05_BTC_Prediction_Verifier"),
    ("O2ilssVhSFs9jsMF", "06_Nightly_Maintenance"),
    ("Ei1eeVdA4ZYuc4o6", "07_BTC_Telegram_Commander"),
    ("Z78ywAmykIW73lDB", "08_BTC_Position_Monitor"),
    ("KWtBSHht9kbvHovG", "09_BTC_Social_Media_Manager"),
]

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def fetch_workflow(wf_id):
    url = f"{N8N_API_URL}/api/v1/workflows/{wf_id}"
    req = urllib.request.Request(url, headers={"X-N8N-API-KEY": N8N_API_KEY})
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=30) as resp:
        return json.loads(resp.read().decode())


def run_git(cmd, cwd=BACKUP_DIR):
    result = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, shell=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git error: {result.stderr.strip()}")
    return result.stdout.strip()


def main():
    log("=== n8n workflows backup start ===")

    if not os.path.isdir(BACKUP_DIR):
        log(f"ERROR: backup dir not found: {BACKUP_DIR}")
        sys.exit(1)

    # Pull aggiornamenti remoti
    try:
        run_git(["git", "pull", "--rebase", "--autostash"])
        log("git pull OK")
    except Exception as e:
        log(f"WARNING: git pull failed (proceeding anyway): {e}")

    saved = 0
    errors = 0
    for wf_id, wf_name in WORKFLOWS:
        try:
            data = fetch_workflow(wf_id)
            filepath = os.path.join(BACKUP_DIR, f"{wf_name}.json")
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            log(f"OK: {wf_name} ({wf_id})")
            saved += 1
        except Exception as e:
            log(f"ERROR: {wf_name} ({wf_id}): {e}")
            errors += 1

    log(f"Fetched {saved}/{len(WORKFLOWS)} workflows, {errors} errors")

    if saved == 0:
        log("ERROR: no workflows saved, aborting git push")
        sys.exit(1)

    # Git commit + push
    try:
        run_git(["git", "add"] + [f"{n}.json" for _, n in WORKFLOWS])
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        msg = f"auto-backup: {saved} workflows {date_str}"
        try:
            run_git(["git", "commit", "-m", msg])
            log(f"git commit OK: {msg}")
        except RuntimeError as e:
            if "nothing to commit" in str(e):
                log("No changes since last backup — skipping push")
                return
            raise
        run_git(["git", "push"])
        log("git push OK")
    except Exception as e:
        log(f"ERROR: git push failed: {e}")
        sys.exit(1)

    log("=== Backup complete ===")


if __name__ == "__main__":
    main()
