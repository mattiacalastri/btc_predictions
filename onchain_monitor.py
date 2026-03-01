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
import threading
import requests
from datetime import datetime, timezone

# ── Nonce lock ─────────────────────────────────────────────────────────────────
# Previene race condition su nonce Polygon quando due chiamate on-chain
# vengono avviate quasi simultaneamente (es. backfill + chiamata live wf01B).
# Garantisce serializzazione: ogni read-nonce + sign + send è atomico.
_nonce_lock = threading.Lock()

# ── Retry config ───────────────────────────────────────────────────────────────
RETRY_MAX      = 3        # tentativi totali
RETRY_BASE_SEC = 2.0      # backoff: 2s → 4s → 8s

# Errori che indicano nonce collision → merita retry dedicato
_NONCE_ERRORS = ("replacement transaction underpriced", "nonce too low", "already known")

# ── Config da env ──────────────────────────────────────────────────────────────
BOT_API_KEY      = os.environ.get("BOT_API_KEY", "")
SUPABASE_URL     = os.environ.get("SUPABASE_URL", "https://oimlamjilivrcnhztwvj.supabase.co")
SUPABASE_KEY     = os.environ.get("SUPABASE_KEY", "")
RAILWAY_URL      = os.environ.get("RAILWAY_URL", "https://web-production-e27d0.up.railway.app")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT    = os.environ.get("TELEGRAM_CHAT_ID", "368092324")
TABLE            = "btc_predictions"
MAX_PER_RUN      = 30    # limite TX per singola esecuzione (sicurezza)
RECEIPT_TIMEOUT  = 30    # secondi attesa conferma on-chain
DRY_RUN          = "--dry-run" in sys.argv


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


# ── Task 2.2 — Generic retry with exponential backoff ─────────────────────────

def _with_retry(fn, *args, retries: int = 3, delays: tuple = (2, 4, 8)):
    """
    Esegue fn(*args) con retry esponenziale.
    Usato per RPC Polygon (contract.functions.*.call(), send_raw_transaction())
    e per chiamate HTTP che non rientrano nei casi di _retry_call.

    In caso di eccezione aspetta delays[i] secondi e riprova.
    Se tutti i tentativi falliscono: loga ERROR [ONCHAIN_RETRY] e rilancia.
    """
    last_err = None
    for attempt in range(retries):
        try:
            return fn(*args)
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait = delays[attempt] if attempt < len(delays) else delays[-1]
                log(f"  [ONCHAIN_RETRY] {type(e).__name__}: {str(e)[:80]} — "
                    f"tentativo {attempt + 1}/{retries}, attendo {wait}s")
                time.sleep(wait)
    log(f"  [ONCHAIN_RETRY] ERROR — tutti i {retries} tentativi falliti: {last_err}")
    raise last_err  # type: ignore[misc]


def _retry_call(fn, label: str = "call"):
    """
    Esegue fn() con retry esponenziale: 3 tentativi, 2s/4s/8s.
    Riprova su: Timeout, ConnectionError, HTTPError 5xx.
    Propaga immediatamente su HTTPError 4xx (bad request — non retriable).
    """
    last_err = None
    for attempt in range(RETRY_MAX):
        try:
            return fn()
        except requests.exceptions.Timeout as e:
            last_err = e
        except requests.exceptions.ConnectionError as e:
            last_err = e
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code < 500:
                raise  # 4xx: non retriable
            last_err = e
            log(f"    [RETRY] HTTP {e.response.status_code if e.response else '?'} su {label}")
        if attempt < RETRY_MAX - 1:
            delay = RETRY_BASE_SEC * (2 ** attempt)
            log(f"    [RETRY] {type(last_err).__name__} su {label}, tentativo {attempt + 1}/{RETRY_MAX}, attendo {delay:.0f}s")
            time.sleep(delay)
    raise last_err  # type: ignore[misc]


def sb_get(path: str) -> list:
    """GET da Supabase REST API con retry esponenziale."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    def _call():
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=headers,
            timeout=15,
            # requests usa certifi per default — verify=True è il default
        )
        r.raise_for_status()
        return r.json()
    return _retry_call(_call, label=f"sb_get:{path[:50]}")


def railway_post(endpoint: str, payload: dict) -> dict:
    """POST a endpoint Flask su Railway con X-API-Key e retry esponenziale."""
    if DRY_RUN:
        log(f"  [DRY-RUN] POST {endpoint} payload={payload}")
        return {"ok": True, "tx": "0x_dry_run", "dry_run": True}
    def _call():
        r = requests.post(
            f"{RAILWAY_URL}{endpoint}",
            json=payload,
            headers={"X-API-Key": BOT_API_KEY, "Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    return _retry_call(_call, label=f"railway:{endpoint}")


# ── Task 2.3 — Wait for on-chain confirmation (fail-open) ────────────────────

def _wait_for_onchain_confirmation(bet_id: int, tx_field: str, tx_hash: str,
                                   timeout: int = RECEIPT_TIMEOUT) -> bool:
    """
    Attende fino a `timeout` secondi che Supabase rifletta il tx_hash nel campo
    tx_field (es. 'onchain_commit_tx' o 'onchain_resolve_tx').

    Questo è l'equivalente di w3.eth.wait_for_transaction_receipt() per
    l'architettura delegata a Railway: il Flask endpoint scrive il tx_hash su
    Supabase solo dopo che la TX è stata inviata (e idealmente confermata).

    In caso di timeout: loga WARNING [ONCHAIN_TIMEOUT] e procede (fail-open —
    un timeout non blocca mai il flusso di trading).

    Ritorna True se confermato entro il timeout, False altrimenti.
    """
    if DRY_RUN:
        return True

    deadline = time.time() + timeout
    poll_interval = 3  # secondi tra un poll e il successivo

    while time.time() < deadline:
        try:
            path = f"{TABLE}?select={tx_field}&id=eq.{bet_id}"
            rows = sb_get(path)
            if rows and rows[0].get(tx_field):
                confirmed_hash = rows[0][tx_field]
                # Verifica che l'hash su Supabase corrisponda a quello atteso
                if confirmed_hash == tx_hash or tx_hash.startswith("0x_dry"):
                    log(f"  [RECEIPT] bet #{bet_id} confermata — {tx_field}: {confirmed_hash[:20]}…")
                    return True
        except Exception as e:
            log(f"  [RECEIPT] poll error bet #{bet_id}: {e}")
        time.sleep(poll_interval)

    log(f"  [ONCHAIN_TIMEOUT] bet #{bet_id} {tx_field} non confermato in {timeout}s — "
        f"proceeding fail-open (tx_hash={tx_hash[:20]}…)")
    return False


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
    """
    Chiama /commit-prediction per una singola bet con nonce lock.

    Task 2.1: Il _nonce_lock serializza le chiamate a Railway che a loro volta
    eseguono get_transaction_count(address, 'pending') + sign + send. Questo
    previene "replacement transaction underpriced" quando due istanze (backfill
    + wf01B live) tentano TX simultanee verso Polygon PoS.

    In caso di nonce error nella risposta JSON: retry automatico dopo 2s (max 3x).
    """
    bet_id = row["id"]
    direction = (row.get("direction") or "UP").upper()
    confidence = float(row.get("confidence") or 0.65)
    entry_price = float(row["entry_fill_price"])
    bet_size = float(row.get("bet_size") or 0.001)
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

    # Task 2.1: nonce lock — serializza read-nonce + send-tx
    for nonce_attempt in range(3):
        try:
            with _nonce_lock:
                result = railway_post("/commit-prediction", payload)

            if result.get("ok"):
                tx_hash = result.get("tx", "")
                log(f"  ✓ bet #{bet_id} → tx {tx_hash[:20]}… [NONCE attempt={nonce_attempt + 1}]")

                # Task 2.3: wait-for-receipt invece di sleep fisso
                _wait_for_onchain_confirmation(bet_id, "onchain_commit_tx", tx_hash)
                return True

            # Nonce error nel body JSON → retry
            error_msg = str(result.get("error", "")).lower()
            if any(ne in error_msg for ne in _NONCE_ERRORS):
                log(f"  [NONCE] nonce error bet #{bet_id}: {error_msg[:60]} — retry in 2s")
                time.sleep(2)
                continue

            log(f"  ✗ bet #{bet_id} error: {result.get('error', result)}")
            return False

        except Exception as e:
            err_str = str(e).lower()
            if any(ne in err_str for ne in _NONCE_ERRORS) and nonce_attempt < 2:
                log(f"  [NONCE] nonce exception bet #{bet_id}: {str(e)[:60]} — retry in 2s")
                time.sleep(2)
                continue
            log(f"  ✗ bet #{bet_id} exception: {e}")
            return False

    log(f"  ✗ bet #{bet_id} — tutti i tentativi nonce falliti")
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
    """
    Chiama /resolve-prediction per una singola bet con nonce lock.
    Stessa logica di commit_bet: serializzazione + retry su nonce error.
    """
    bet_id = row["id"]
    exit_price = float(row["exit_fill_price"])
    pnl_usd = float(row.get("pnl_usd") or 0.0)
    won = bool(row.get("correct"))
    close_ts = int(time.time())

    payload = {
        "bet_id": bet_id,
        "exit_price": exit_price,
        "pnl_usd": pnl_usd,
        "won": won,
        "close_timestamp": close_ts,
    }

    log(f"  RESOLVE bet #{bet_id} won={won} exit=${exit_price:.2f} pnl={pnl_usd:+.2f}")

    # Task 2.1: nonce lock — serializza read-nonce + send-tx
    for nonce_attempt in range(3):
        try:
            with _nonce_lock:
                result = railway_post("/resolve-prediction", payload)

            if result.get("ok"):
                tx_hash = result.get("tx", "")
                log(f"  ✓ bet #{bet_id} → tx {tx_hash[:20]}… [NONCE attempt={nonce_attempt + 1}]")

                # Task 2.3: wait-for-receipt invece di sleep fisso
                _wait_for_onchain_confirmation(bet_id, "onchain_resolve_tx", tx_hash)
                return True

            error_msg = str(result.get("error", "")).lower()
            if any(ne in error_msg for ne in _NONCE_ERRORS):
                log(f"  [NONCE] nonce error bet #{bet_id}: {error_msg[:60]} — retry in 2s")
                time.sleep(2)
                continue

            log(f"  ✗ bet #{bet_id} error: {result.get('error', result)}")
            return False

        except Exception as e:
            err_str = str(e).lower()
            if any(ne in err_str for ne in _NONCE_ERRORS) and nonce_attempt < 2:
                log(f"  [NONCE] nonce exception bet #{bet_id}: {str(e)[:60]} — retry in 2s")
                time.sleep(2)
                continue
            log(f"  ✗ bet #{bet_id} exception: {e}")
            return False

    log(f"  ✗ bet #{bet_id} — tutti i tentativi nonce falliti")
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
            # Task 2.3: nessun sleep fisso — _wait_for_onchain_confirmation
            # gestisce l'attesa receipt dentro commit_bet/resolve_bet.
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
            # Task 2.3: nessun sleep fisso — gestito dentro resolve_bet
    except Exception as e:
        log(f"  ERRORE recupero resolve mancanti: {e}")

    # ── Task 2.4 — Summary report ──────────────────────────────────────────────
    total_ok = committed_ok + resolved_ok
    total_err = committed_err + resolved_err

    log("=== RIEPILOGO ===")
    log(f"  Commit:  {committed_ok} OK  /  {committed_err} ERR")
    log(f"  Resolve: {resolved_ok} OK  /  {resolved_err} ERR")
    if skipped_no_exit:
        log(f"  Skipped: {skipped_no_exit} bet senza exit_fill_price")

    # Formato macchina leggibile per parsing log/Sentry
    print(
        f"[ONCHAIN_SUMMARY] Committed: {committed_ok}, Resolved: {resolved_ok}, "
        f"Errors: {total_err}, Skipped: {skipped_no_exit}",
        flush=True,
    )

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
