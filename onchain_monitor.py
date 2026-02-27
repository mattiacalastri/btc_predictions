#!/usr/bin/env python3
"""
On-Chain Audit Backfill Monitor
================================
Trova bet senza registrazione on-chain e le registra retroattivamente
chiamando i Flask endpoints /commit-prediction e /resolve-prediction su Railway.

Fasi:
  1. COMMIT  — bet con entry_fill_price ma onchain_commit_tx IS NULL
  2. RESOLVE — bet chiuse (correct IS NOT NULL) con onchain_commit_tx ma senza onchain_resolve_tx

Eseguito da launchd com.btcbot.onchain_monitor ogni ora.

Env vars richieste (plist EnvironmentVariables):
  BOT_API_KEY       — per autenticarsi agli endpoint Flask
  SUPABASE_URL      — es. https://oimlamjilivrcnhztwvj.supabase.co
  SUPABASE_KEY      — anon/service key Supabase
  RAILWAY_URL       — es. https://web-production-e27d0.up.railway.app
  TELEGRAM_BOT_TOKEN  (opzionale) — notifiche Telegram
  TELEGRAM_CHAT_ID    (opzionale) — es. 368092324
"""

import os
import sys
import time
import requests
from datetime import datetime, timezone

# ── Config da env ──────────────────────────────────────────────────────────────
BOT_API_KEY      = os.environ.get("BOT_API_KEY", "")
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "https://oimlamjilivrcnhztwvj.supabase.co")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")
RAILWAY_URL      = os.environ.get("RAILWAY_URL", "https://web-production-e27d0.up.railway.app")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT    = os.environ.get("TELEGRAM_CHAT_ID", "368092324")
TABLE            = "btc_predictions"
TX_DELAY_SEC     = 2.5   # pausa tra TX Polygon per evitare nonce collision
MAX_PER_RUN      = 30    # limite TX per singola esecuzione (sicurezza)
DRY_RUN          = "--dry-run" in sys.argv


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def sb_get(path: str) -> list:
    """GET da Supabase REST API."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def railway_post(endpoint: str, payload: dict) -> dict:
    """POST a endpoint Flask su Railway con X-API-Key."""
    if DRY_RUN:
        log(f"  [DRY-RUN] POST {endpoint} payload={payload}")
        return {"ok": True, "tx": "0x_dry_run", "dry_run": True}
    r = requests.post(
        f"{RAILWAY_URL}{endpoint}",
        json=payload,
        headers={"X-API-Key": BOT_API_KEY, "Content-Type": "application/json"},
        timeout=30,
    )
    return r.json()


def send_telegram(msg: str):
    """Invia messaggio Telegram se token disponibile."""
    if not TELEGRAM_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log(f"  [TELEGRAM] errore invio: {e}")


# ── FASE 1: Commit ─────────────────────────────────────────────────────────────

def get_missing_commits() -> list:
    """Bet classification=BET, onchain_commit_tx IS NULL, entry_fill_price disponibile."""
    path = (
        f"{TABLE}"
        "?select=id,direction,confidence,bet_size,entry_fill_price,created_at"
        "&classification=eq.BET"
        "&entry_fill_price=not.is.null"
        f"&onchain_commit_tx=is.null"
        "&order=id.asc"
        f"&limit={MAX_PER_RUN}"
    )
    rows = sb_get(path)
    # Filtra righe senza entry_fill_price > 0
    return [r for r in rows if float(r.get("entry_fill_price") or 0) > 0]


def commit_bet(row: dict) -> bool:
    """Chiama /commit-prediction per una singola bet. Ritorna True se ok."""
    bet_id = row["id"]
    direction = (row.get("direction") or "UP").upper()
    confidence = float(row.get("confidence") or 0.65)
    entry_price = float(row["entry_fill_price"])
    bet_size = float(row.get("bet_size") or 0.001)
    # Timestamp = created_at convertito in unix seconds
    created_at_str = row.get("created_at", "")
    try:
        dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        ts = int(dt.timestamp())
    except Exception:
        ts = int(time.time())

    payload = {
        "bet_id": bet_id,
        "direction": direction,
        "confidence": confidence,
        "entry_price": entry_price,
        "bet_size": bet_size,
        "timestamp": ts,
    }

    log(f"  COMMIT bet #{bet_id} {direction} entry=${entry_price:.2f} conf={confidence:.2f}")
    try:
        result = railway_post("/commit-prediction", payload)
        if result.get("ok"):
            log(f"  ✓ bet #{bet_id} → tx {result.get('tx', '?')[:20]}…")
            return True
        else:
            log(f"  ✗ bet #{bet_id} error: {result.get('error', result)}")
            return False
    except Exception as e:
        log(f"  ✗ bet #{bet_id} exception: {e}")
        return False


# ── FASE 2: Resolve ────────────────────────────────────────────────────────────

def get_missing_resolves() -> list:
    """Bet committed + chiuse (correct IS NOT NULL) senza resolve on-chain + exit_fill_price."""
    path = (
        f"{TABLE}"
        "?select=id,correct,exit_fill_price,pnl_usd"
        "&classification=eq.BET"
        "&onchain_commit_tx=not.is.null"
        "&correct=not.is.null"
        "&onchain_resolve_tx=is.null"
        "&exit_fill_price=not.is.null"
        "&order=id.asc"
        f"&limit={MAX_PER_RUN}"
    )
    rows = sb_get(path)
    return [r for r in rows if float(r.get("exit_fill_price") or 0) > 0]


def resolve_bet(row: dict) -> bool:
    """Chiama /resolve-prediction per una singola bet. Ritorna True se ok."""
    bet_id = row["id"]
    exit_price = float(row["exit_fill_price"])
    pnl_usd = float(row.get("pnl_usd") or 0.0)
    won = bool(row.get("correct"))
    close_ts = int(time.time())   # timestamp corrente — coerente con comportamento wf02

    payload = {
        "bet_id": bet_id,
        "exit_price": exit_price,
        "pnl_usd": pnl_usd,
        "won": won,
        "close_timestamp": close_ts,
    }

    log(f"  RESOLVE bet #{bet_id} won={won} exit=${exit_price:.2f} pnl={pnl_usd:+.2f}")
    try:
        result = railway_post("/resolve-prediction", payload)
        if result.get("ok"):
            log(f"  ✓ bet #{bet_id} → tx {result.get('tx', '?')[:20]}…")
            return True
        else:
            log(f"  ✗ bet #{bet_id} error: {result.get('error', result)}")
            return False
    except Exception as e:
        log(f"  ✗ bet #{bet_id} exception: {e}")
        return False


# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    if DRY_RUN:
        log("=== DRY RUN MODE — nessuna TX verrà inviata ===")

    if not BOT_API_KEY:
        log("ERRORE: BOT_API_KEY non impostata")
        sys.exit(1)

    committed_ok = 0
    committed_err = 0
    resolved_ok = 0
    resolved_err = 0
    skipped_no_exit = 0

    # ── Fase 1: Commit ─────────────────────────────────────────────────────────
    log("=== FASE 1: Ricerca bet senza commit on-chain ===")
    try:
        missing_commits = get_missing_commits()
        log(f"  Trovate {len(missing_commits)} bet da committare")
        for row in missing_commits:
            ok = commit_bet(row)
            if ok:
                committed_ok += 1
            else:
                committed_err += 1
            if not DRY_RUN:
                time.sleep(TX_DELAY_SEC)
    except Exception as e:
        log(f"  ERRORE recupero commit mancanti: {e}")

    # ── Fase 2: Resolve ────────────────────────────────────────────────────────
    log("=== FASE 2: Ricerca bet chiuse senza resolve on-chain ===")
    try:
        missing_resolves = get_missing_resolves()

        # Log separato per bet senza exit_fill_price (informativo)
        all_need_resolve_path = (
            f"{TABLE}"
            "?select=id"
            "&classification=eq.BET"
            "&onchain_commit_tx=not.is.null"
            "&correct=not.is.null"
            "&onchain_resolve_tx=is.null"
            f"&limit={MAX_PER_RUN + 50}"
        )
        all_need = sb_get(all_need_resolve_path)
        skipped_no_exit = len(all_need) - len(missing_resolves)

        log(f"  Trovate {len(missing_resolves)} bet risolvibili (skip {skipped_no_exit} senza exit_fill_price)")
        for row in missing_resolves:
            ok = resolve_bet(row)
            if ok:
                resolved_ok += 1
            else:
                resolved_err += 1
            if not DRY_RUN:
                time.sleep(TX_DELAY_SEC)
    except Exception as e:
        log(f"  ERRORE recupero resolve mancanti: {e}")

    # ── Riepilogo ──────────────────────────────────────────────────────────────
    total_ok = committed_ok + resolved_ok
    total_err = committed_err + resolved_err

    log("=== RIEPILOGO ===")
    log(f"  Commit:  {committed_ok} OK  /  {committed_err} ERR")
    log(f"  Resolve: {resolved_ok} OK  /  {resolved_err} ERR")
    if skipped_no_exit:
        log(f"  Skipped: {skipped_no_exit} bet senza exit_fill_price")

    if total_ok > 0 or total_err > 0:
        mode = " [DRY RUN]" if DRY_RUN else ""
        msg = (
            f"⛓️ <b>On-Chain Backfill{mode}</b>\n\n"
            f"✅ Commit: {committed_ok} OK\n"
            f"✅ Resolve: {resolved_ok} OK\n"
        )
        if total_err:
            msg += f"❌ Errori: {total_err}\n"
        if skipped_no_exit:
            msg += f"⚠️ Skip (no exit price): {skipped_no_exit}\n"
        send_telegram(msg)
        log(f"  Telegram inviato: {total_ok} TX completate")
    else:
        log("  Nulla da fare — tutto in regola on-chain ✓")


if __name__ == "__main__":
    main()
