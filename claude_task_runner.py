#!/usr/bin/env python3
"""
claude_task_runner.py — Telegram→Claude Code bridge (Mac-side)
Polls Supabase claude_tasks WHERE status='pending', runs claude -p, sends result to Telegram.
Runs every 5s via launchd com.btcbot.claude_runner.plist
"""

import html
import os
import re
import subprocess
import requests
from datetime import datetime, timezone

SUPABASE_URL  = os.environ.get("SUPABASE_URL", "https://oimlamjilivrcnhztwvj.supabase.co")
SUPABASE_KEY  = os.environ.get("SUPABASE_ANON_KEY", "")
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CLAUDE_BIN    = os.environ.get("CLAUDE_BIN", "/Users/mattiacalastri/.local/bin/claude")
WORK_DIR      = "/Users/mattiacalastri/btc_predictions"
CLAUDE_TIMEOUT = 180  # seconds
OWNER_CHAT_ID  = "368092324"   # sicurezza: solo Mattia può avere task eseguiti


def _headers():
    return {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "apikey": SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _now():
    return datetime.now(timezone.utc).isoformat()


def fetch_pending():
    url = (
        f"{SUPABASE_URL}/rest/v1/claude_tasks"
        "?status=eq.pending&order=created_at.asc&limit=1"
        "&select=id,command,telegram_chat_id"
    )
    resp = requests.get(url, headers=_headers(), timeout=10)
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


def mark_inprogress(task_id):
    """Atomic claim: aggiunge &status=eq.pending al filtro.
    Se un'altra istanza ha già claimato il task, Supabase non aggiorna nessuna riga
    e restituisce [] → restituiamo False → l'istanza corrente esce senza eseguire.
    Risolve la race condition C-01 (doppia esecuzione su launchd overlap).
    """
    url = f"{SUPABASE_URL}/rest/v1/claude_tasks?id=eq.{task_id}&status=eq.pending"
    payload = {"status": "running", "started_at": _now()}
    r = requests.patch(url, headers=_headers(), json=payload, timeout=10)
    r.raise_for_status()
    return bool(r.json())  # [] → già claimato da un'altra istanza


def mark_done(task_id, result, status="completed"):
    url = f"{SUPABASE_URL}/rest/v1/claude_tasks?id=eq.{task_id}"
    payload = {"status": status, "result": result[:10000], "completed_at": _now()}
    requests.patch(url, headers=_headers(), json=payload, timeout=10).raise_for_status()


def md_to_html(text: str) -> str:
    """Convert Claude's markdown output to Telegram HTML."""
    lines = []
    for line in text.split("\n"):
        # Horizontal rules
        if re.match(r"^-{3,}$", line.strip()):
            lines.append("──────────")
            continue
        # Headers → bold
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            content = html.escape(m.group(2))
            # Apply inline formatting inside header
            content = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", content)
            content = re.sub(r"`([^`]+)`", r"<code>\1</code>", content)
            lines.append(f"<b>{content}</b>")
            continue
        # Normal line: escape first, then apply inline patterns
        escaped = html.escape(line)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
        escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", escaped)
        escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
        lines.append(escaped)
    return "\n".join(lines)


def send_telegram(chat_id, text, command="", is_error=False):
    if not BOT_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    html_text = md_to_html(text)
    prefix = "🤖 <b>Claude:</b>\n\n" if not is_error else "❌ <b>Errore:</b>\n\n"

    # Inline keyboard — copia "/claude " negli appunti con un tap
    keyboard = {"inline_keyboard": [[
        {"text": "📋 copia /claude", "copy_text": {"text": "/claude "}},
    ]]}

    chunks = [html_text[i:i + 3800] for i in range(0, min(len(html_text), 15200), 3800)]
    for idx, chunk in enumerate(chunks):
        payload = {
            "chat_id": chat_id,
            "text": (prefix + chunk) if idx == 0 else chunk,
            "parse_mode": "HTML",
        }
        # Add button only on last chunk
        if idx == len(chunks) - 1:
            payload["reply_markup"] = keyboard
        try:
            requests.post(url, json=payload, timeout=15)
        except Exception:
            pass


def run_claude(command):
    proc = subprocess.run(
        [CLAUDE_BIN, "-p", command, "--output-format", "text"],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        cwd=WORK_DIR,
        env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
    )
    output = proc.stdout.strip()
    if proc.returncode != 0 and proc.stderr:
        output = f"[exit {proc.returncode}]\n{proc.stderr[:500]}\n{output}"
    return output or "(nessun output)"


def main():
    if not SUPABASE_KEY:
        print("SUPABASE_ANON_KEY non impostata — uscita")
        return

    task = fetch_pending()
    if not task:
        return  # nessun task pendente

    task_id = task["id"]
    command = task["command"]
    chat_id = str(task.get("telegram_chat_id") or "")

    # Sicurezza: esegui solo task creati dal proprietario
    if chat_id != OWNER_CHAT_ID:
        mark_done(task_id, "Unauthorized", "error")
        return

    print(f"[{datetime.now().isoformat()}] Task #{task_id}: {command[:80]}")
    if not mark_inprogress(task_id):
        print(f"[{datetime.now().isoformat()}] Task #{task_id} già claimato da altra istanza — uscita")
        return  # C-01: altra istanza launchd ci ha preceduti

    try:
        result = run_claude(command)
        mark_done(task_id, result, "completed")
        send_telegram(chat_id, result, command=command)
    except subprocess.TimeoutExpired:
        err = f"Timeout dopo {CLAUDE_TIMEOUT}s — prova un comando piu breve."
        mark_done(task_id, err, "error")
        send_telegram(chat_id, err, is_error=True)
    except Exception as e:
        err = str(e)
        mark_done(task_id, err, "error")
        send_telegram(chat_id, err[:500], is_error=True)


if __name__ == "__main__":
    main()
