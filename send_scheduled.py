#!/usr/bin/env python3
"""
BTC Channel Scheduler — legge btc_schedule.json e invia i messaggi pianificati.
Cron: 0 9,14,20 * * * /usr/bin/python3 /Users/mattiacalastri/btc_predictions/send_scheduled.py
"""
import json
import os
import sys
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BOT_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN")
CHANNEL_ID  = os.environ.get("TELEGRAM_CHANNEL_ID")
SCHEDULE_FILE = os.path.join(os.path.dirname(__file__), "btc_schedule.json")

def load_schedule():
    with open(SCHEDULE_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_schedule(data):
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def send(text, photo_path=None):
    if photo_path and os.path.exists(photo_path):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
        with open(photo_path, "rb") as photo:
            r = requests.post(url,
                data={"chat_id": CHANNEL_ID, "caption": text},
                files={"photo": photo},
                timeout=30)
    else:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r = requests.post(url,
            json={"chat_id": CHANNEL_ID, "text": text},
            timeout=30)
    return r.json()

def main():
    schedule = load_schedule()
    now = datetime.now()
    sent_any = False

    for msg in schedule["messages"]:
        if msg.get("sent"):
            continue
        due_at = datetime.fromisoformat(msg["scheduled_at"])
        if now >= due_at:
            print(f"[{now.strftime('%H:%M')}] Invio msg #{msg['id']} (pianificato {msg['scheduled_at']})")
            result = send(msg["text"], msg.get("photo"))
            if result.get("ok"):
                msg["sent"] = True
                msg["sent_at"] = now.isoformat()
                save_schedule(schedule)
                print(f"  ✅ Inviato — message_id {result['result']['message_id']}")
                sent_any = True
            else:
                print(f"  ❌ Errore: {result}")

    if not sent_any:
        print(f"[{now.strftime('%H:%M')}] Nessun messaggio in scadenza.")

if __name__ == "__main__":
    main()
