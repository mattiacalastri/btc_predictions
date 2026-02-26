#!/usr/bin/env python3
"""
Telegram→Claude Code bridge.
Polling Supabase ogni 5s — esegue claude --print — risponde su Telegram.
"""

import os
import json
import time
import subprocess
import requests
from datetime import datetime

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://oimlamjilivrcnhztwvj.supabase.co")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
POLL_INTERVAL = 5  # secondi
CLAUDE_TIMEOUT = 120  # secondi max per un'esecuzione Claude
MAX_TG_LEN = 4096


def _headers():
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "apikey": SUPABASE_SERVICE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def fetch_pending():
    url = (
        f"{SUPABASE_URL}/rest/v1/claude_tasks"
        "?status=eq.pending&order=created_at.asc&limit=1"
    )
    resp = requests.get(url, headers=_headers(), timeout=10)
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def mark_running(task_id):
    url = f"{SUPABASE_URL}/rest/v1/claude_tasks?id=eq.{task_id}"
    payload = {"status": "running", "started_at": datetime.utcnow().isoformat() + "Z"}
    requests.patch(url, headers=_headers(), json=payload, timeout=10).raise_for_status()


def mark_done(task_id, result, status="completed"):
    url = f"{SUPABASE_URL}/rest/v1/claude_tasks?id=eq.{task_id}"
    payload = {
        "status": status,
        "result": result[:10000],  # cap DB
        "completed_at": datetime.utcnow().isoformat() + "Z",
    }
    requests.patch(url, headers=_headers(), json=payload, timeout=10).raise_for_status()


def send_telegram(chat_id, text):
    if not BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    # Suddivide in chunk se > 4096 char
    for i in range(0, len(text), MAX_TG_LEN):
        chunk = text[i:i + MAX_TG_LEN]
        requests.post(url, json={
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
        }, timeout=15)


def run_claude(command):
    proc = subprocess.run(
        ["claude", "--print", command],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        cwd="/Users/mattiacalastri/btc_predictions",
    )
    output = proc.stdout.strip()
    if proc.returncode != 0 and proc.stderr:
        output = f"[exit {proc.returncode}]\n{proc.stderr[:500]}\n{output}"
    return output or "(nessun output)"


def main():
    if not SUPABASE_SERVICE_KEY:
        print("SUPABASE_SERVICE_KEY non impostata — uscita")
        return

    task = fetch_pending()
    if not task:
        return  # nessun task, launchd richiamerà tra 5s

    task_id = task["id"]
    command = task["command"]
    chat_id = task.get("telegram_chat_id")

    print(f"[{datetime.now().isoformat()}] Task #{task_id}: {command[:80]}")
    mark_running(task_id)

    if chat_id:
        send_telegram(chat_id, f"⏳ Eseguo: <code>{command[:200]}</code>")

    try:
        result = run_claude(command)
        mark_done(task_id, result, "completed")
        if chat_id:
            send_telegram(chat_id, f"✅ <b>Risultato</b>:\n{result[:3800]}")
    except subprocess.TimeoutExpired:
        err = f"Timeout dopo {CLAUDE_TIMEOUT}s"
        mark_done(task_id, err, "error")
        if chat_id:
            send_telegram(chat_id, f"⚠️ {err}")
    except Exception as e:
        err = str(e)
        mark_done(task_id, err, "error")
        if chat_id:
            send_telegram(chat_id, f"❌ Errore: {err[:500]}")


if __name__ == "__main__":
    main()
