import os
import re
import json
import math
import time
import hashlib
import threading
import datetime as _dt
import hmac as _hmac
from joblib import load as joblib_load
import requests
import sentry_sdk
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, redirect
from kraken.futures import Trade, User
from constants import TAKER_FEE, _BIAS_MAP

sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN", ""),
    traces_sample_rate=0.0,   # solo error monitoring, no performance tracing
    send_default_pii=False,
)

app = Flask(__name__)


@app.before_request
def redirect_www():
    """Redirect www.btcpredictor.io → btcpredictor.io (canonical)."""
    host = request.host.lower()
    if host.startswith("www."):
        canonical = host[4:]
        scheme = request.headers.get("X-Forwarded-Proto", "https")
        return redirect(f"{scheme}://{canonical}{request.full_path}", code=301)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains; preload"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com "
        "https://js-de.sentry-cdn.com https://www.googletagmanager.com https://www.clarity.ms; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "connect-src 'self' https://*.railway.app https://oimlamjilivrcnhztwvj.supabase.co "
        "https://sentry.io https://*.sentry-cdn.com https://www.clarity.ms "
        "https://www.google-analytics.com https://n8n.srv1432354.hstgr.cloud;"
    )
    return response


def _sb_config() -> tuple:
    """Restituisce (supabase_url, supabase_key). Unica sorgente env vars Supabase."""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")
    return url, key


# ── Rate limiting (S-20) ──────────────────────────────────────────────────────
_RATE_STORE: dict = {}  # key → (count, window_start)
_RATE_LOCK = threading.Lock()  # thread-safe per Gunicorn multi-worker (stesso processo)
_RL_WINDOW = 60         # secondi per finestra
_RL_MAX_DEFAULT = 100   # chiamate per finestra default
_XGB_GATE_MIN_BETS = int(os.environ.get("XGB_MIN_BETS", "100"))  # bet pulite necessarie per attivare il gate


def _check_rate_limit(key: str, max_calls: int = _RL_MAX_DEFAULT) -> bool:
    """Restituisce True se la chiamata è consentita, False se rate-limited.
    Utilizza finestre scorrevoli di _RL_WINDOW secondi, con cleanup automatico.
    Thread-safe tramite _RATE_LOCK (Gunicorn threaded mode).
    """
    now = time.time()
    with _RATE_LOCK:
        # cleanup entries scadute (evita crescita illimitata)
        expired = [k for k, v in _RATE_STORE.items() if now - v[1] >= _RL_WINDOW * 2]
        for k in expired:
            _RATE_STORE.pop(k, None)
        count, ts = _RATE_STORE.get(key, (0, now))
        if now - ts >= _RL_WINDOW:
            count, ts = 0, now
        count += 1
        _RATE_STORE[key] = (count, ts)
        return count <= max_calls


# ── Input validation helpers (H-2, H-4) ──────────────────────────────────────

def _safe_float(val, default: float, min_v: float | None = None, max_v: float | None = None) -> float:
    """Converte val in float con protezione da NaN, Infinity e range non validi.
    Ritorna default se la conversione fallisce o il valore è fuori range.
    """
    try:
        v = float(val)
    except (ValueError, TypeError):
        return default
    if math.isnan(v) or math.isinf(v):
        return default
    if min_v is not None and v < min_v:
        return min_v
    if max_v is not None and v > max_v:
        return max_v
    return v


def _safe_int(val, default: int, min_v: int | None = None, max_v: int | None = None) -> int:
    """Converte val in int con clamp e fallback. Mai lancia ValueError sul chiamante."""
    try:
        v = int(float(val))  # float() prima per gestire "12.0"
    except (ValueError, TypeError):
        return default
    if min_v is not None and v < min_v:
        return min_v
    if max_v is not None and v > max_v:
        return max_v
    return v


# ── XGBoost direction model (caricato una volta all'avvio) ────────────────────
_XGB_MODEL = None
_XGB_CLEAN_BET_COUNT: int | None = None   # cache count bet pulite (post-Day0)
_XGB_CLEAN_BET_CHECKED_AT: float = 0.0   # timestamp ultimo check
_XGB_CLEAN_CACHE_TTL = 600               # 10 min
# _BIAS_MAP e TAKER_FEE importati da constants.py
# Feature order per XGBoost (usare stessa sequenza in feat_row): vedi _run_xgb_gate() riga ~974
# [confidence, fear_greed, rsi14, technical_score, hour_sin, hour_cos,
#  technical_bias_score, signal_fg_fear, dow_sin, dow_cos, session]

def _load_xgb_model():
    global _XGB_MODEL
    model_path = os.path.join(os.path.dirname(__file__), "models", "xgb_direction.pkl")
    if os.path.exists(model_path):
        _XGB_MODEL = joblib_load(model_path)
        from xgboost import XGBClassifier
        assert isinstance(_XGB_MODEL, XGBClassifier), "Direction model type mismatch"
        print(f"[XGB] Model loaded from {model_path}")
    else:
        print(f"[XGB] Model not found at {model_path} — /predict-xgb will return agree=True")

_load_xgb_model()

# ── XGBoost correctness model (caricato una volta all'avvio) ─────────────────
_xgb_correctness = None
try:
    _corr_path = os.path.join(os.path.dirname(__file__), "models", "xgb_correctness.pkl")
    _xgb_correctness = joblib_load(_corr_path)
    from xgboost import XGBClassifier
    assert isinstance(_xgb_correctness, XGBClassifier), "Correctness model type mismatch"
    print("[XGB] Correctness model loaded")
except Exception as _e:
    print(f"[XGB] Correctness model NOT loaded: {_e}")

# ── Confidence calibration table (storico WR per bucket) ─────────────────────
CONF_CALIBRATION = {
    # (min_conf, max_conf): win_rate_storico
    (0.50, 0.55): 0.442,
    (0.55, 0.60): 0.450,
    (0.60, 0.62): 0.571,
    (0.62, 0.65): 0.514,
    (0.65, 0.70): 0.455,
    (0.70, 1.00): 0.500,
}

def get_calibrated_wr(conf):
    for (lo, hi), wr in CONF_CALIBRATION.items():
        if lo <= conf < hi:
            return wr
    return 0.50

# ── Auto-calibration: ore morti (aggiornato da /reload-calibration) ───────────
# Calibrazione 2026-02-28 su 650 segnali pre-Day0 (xgb_report.txt hourly WR):
#   5h 42.3% | 7h 42.1% | 10h 44.0% | 11h 42.9% | 17h 43.6% | 19h 44.4%
# (soglia live: n>=8 && WR<45%; 11h ha 7 bet — incluso come prior di calibrazione)
# Post-Day0: refresh_dead_hours() non trova dati → usa questi come fallback.
DEAD_HOURS_UTC: set = {5, 7, 10, 11, 17, 19}

def refresh_calibration():
    """Aggiorna CONF_CALIBRATION da WR reale Supabase per bucket di confidence."""
    global CONF_CALIBRATION
    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return {"ok": False, "error": "no_supabase_env"}
    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=confidence,correct&bet_taken=eq.true&correct=not.is.null",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            timeout=8,
        )
        rows = r.json() if r.ok else []
        if len(rows) < 20:
            return {"ok": False, "error": "insufficient_data", "count": len(rows)}
        buckets = {(0.50,0.55):[],(0.55,0.60):[],(0.60,0.62):[],(0.62,0.65):[],(0.65,0.70):[],(0.70,1.00):[]}
        for row in rows:
            conf = float(row.get("confidence") or 0)
            c = row.get("correct")
            if c is None:
                continue
            for (lo, hi) in buckets:
                if lo <= conf < hi:
                    buckets[(lo, hi)].append(1 if c else 0)
                    break
        new_cal, stats = {}, {}
        for (lo, hi), vals in buckets.items():
            key = f"{lo:.2f}-{hi:.2f}"
            if len(vals) >= 5:
                wr = sum(vals) / len(vals)
                new_cal[(lo, hi)] = round(wr, 3)
                stats[key] = {"wr": round(wr, 3), "n": len(vals)}
            else:
                new_cal[(lo, hi)] = CONF_CALIBRATION.get((lo, hi), 0.50)
                stats[key] = {"wr": new_cal[(lo, hi)], "n": len(vals), "fallback": True}
        CONF_CALIBRATION = new_cal
        print(f"[CAL] Calibration updated: {stats}")
        return {"ok": True, "stats": stats, "total_rows": len(rows)}
    except Exception as e:
        app.logger.exception("Calibration refresh error")
        return {"ok": False, "error": "refresh_error"}

def refresh_dead_hours():
    """Aggiorna DEAD_HOURS_UTC: ore con WR < 45% e almeno 8 bet. Ora estratta da created_at."""
    global DEAD_HOURS_UTC
    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return {"ok": False, "error": "no_supabase_env"}
    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=created_at,correct&bet_taken=eq.true&correct=not.is.null",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            timeout=8,
        )
        rows = r.json() if r.ok else []
        if len(rows) < 20:
            return {"ok": False, "error": "insufficient_data", "count": len(rows)}
        from collections import defaultdict
        hour_data: dict = defaultdict(list)
        for row in rows:
            c = row.get("correct")
            ts = row.get("created_at")
            if c is None or not ts:
                continue
            try:
                h = _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
                hour_data[h].append(1 if c else 0)
            except Exception:
                continue
        dead, hour_stats = set(), {}
        for h, vals in sorted(hour_data.items()):
            wr = sum(vals) / len(vals) if vals else 0.5
            hour_stats[h] = {"wr": round(wr, 3), "n": len(vals)}
            if len(vals) >= 8 and wr < 0.45:
                dead.add(h)
        # fallback: se non ci sono ore con n>=8 e WR<45%, usa prior da calibrazione storica
        DEAD_HOURS_UTC = dead if dead else {5, 7, 10, 11, 17, 19}
        print(f"[CAL] Dead hours updated: {sorted(DEAD_HOURS_UTC)}")
        return {"ok": True, "dead_hours": sorted(DEAD_HOURS_UTC), "hour_stats": hour_stats}
    except Exception as e:
        app.logger.exception("Calibration refresh error")
        return {"ok": False, "error": "refresh_error"}

API_KEY = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PF_XBTUSD")
KRAKEN_BASE = "https://futures.kraken.com"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "btc_predictions")
_ALLOWED_TABLES = {"btc_predictions", "sandbox_btc_predictions"}
if SUPABASE_TABLE not in _ALLOWED_TABLES:
    raise ValueError(f"SUPABASE_TABLE '{SUPABASE_TABLE}' not in whitelist {_ALLOWED_TABLES}")
_BOT_PAUSED = True               # fail-safe default: paused until Supabase confirms otherwise
_BOT_PAUSED_REFRESHED_AT = 0.0  # timestamp of last Supabase read (0.0 → forces refresh on first call)
_costs_cache = {"data": None, "ts": 0.0}

# Startup security validation (H-3)
if not os.environ.get("BOT_API_KEY"):
    app.logger.warning("[SECURITY] BOT_API_KEY not set — all protected endpoints are unauthenticated!")
    sentry_sdk.capture_message("SECURITY: BOT_API_KEY missing at startup", level="warning")

# Refresh calibration all'avvio — DOPO la definizione di SUPABASE_TABLE
try:
    refresh_calibration()
    refresh_dead_hours()
    _refresh_bot_paused()   # sync _BOT_PAUSED da Supabase → evita "Bot Paused" falso al boot
except Exception:
    pass


def _refresh_bot_paused():
    """Read paused state from Supabase bot_state. Called on restart and every 5 min.
    Fail-safe: _BOT_PAUSED defaults True at boot; only set False when Supabase confirms.
    """
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT
    try:
        sb_url, sb_key = _sb_config()
        if not sb_url or not sb_key:
            return
        r = requests.get(
            f"{sb_url}/rest/v1/bot_state?key=eq.paused&select=value",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            timeout=3,
        )
        if r.ok:
            data = r.json()
            if data:
                _BOT_PAUSED = data[0].get("value", "false").lower() in ("true", "1")
            else:
                # Row doesn't exist → new install or row deleted → treat as not paused
                _BOT_PAUSED = False
            _BOT_PAUSED_REFRESHED_AT = time.time()
        # If r not ok: leave _BOT_PAUSED unchanged, don't update timestamp → retry next call
    except Exception:
        # Network error / timeout: leave _BOT_PAUSED unchanged (fail-safe True at boot)
        # Don't update _BOT_PAUSED_REFRESHED_AT → will retry on next place_bet() call
        pass


def _save_bot_paused(paused: bool):
    """Persist paused state to Supabase bot_state."""
    try:
        sb_url, sb_key = _sb_config()
        if not sb_url or not sb_key:
            return
        requests.patch(
            f"{sb_url}/rest/v1/bot_state?key=eq.paused",
            json={"value": str(paused).lower()},
            headers={
                "apikey": sb_key,
                "Authorization": f"Bearer {sb_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            },
            timeout=3,
        )
    except Exception:
        pass


def _check_api_key():
    """Verifica X-API-Key header con timing-safe compare (hmac.compare_digest).
    Se BOT_API_KEY non configurata, logga warning e passa (backwards compat).
    """
    bot_key = os.environ.get("BOT_API_KEY", "")
    if not bot_key:
        return None
    req_key = request.headers.get("X-API-Key", "")
    if not _hmac.compare_digest(req_key.encode(), bot_key.encode()):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _check_read_key():
    """Auth per endpoint read-only (signals, account-summary, equity-history, risk-metrics).
    Accetta READ_API_KEY (iniettato nel dashboard) oppure BOT_API_KEY (n8n/interni).
    Se READ_API_KEY non configurata → endpoint rimane pubblico (backwards compat).
    """
    read_key = os.environ.get("READ_API_KEY", "")
    if not read_key:
        return None  # non configurato → pubblico
    provided = request.headers.get("X-API-Key", "").encode()
    if provided and _hmac.compare_digest(provided, read_key.encode()):
        return None
    bot_key = os.environ.get("BOT_API_KEY", "")
    if bot_key and provided and _hmac.compare_digest(provided, bot_key.encode()):
        return None
    return jsonify({"error": "Unauthorized"}), 401


def _make_contribution_token(contrib_id: int, action: str, hour_bucket: int = None) -> str:
    """Genera un token HMAC-SHA256 per approve/reject link con scadenza 2h.
    Token specifico per contrib_id+action+hour_bucket: non riutilizzabile su altri endpoint.
    Non espone BOT_API_KEY nell'URL. Il bucket orario scade entro 2h (S-16).
    """
    import hashlib as _hl
    if hour_bucket is None:
        hour_bucket = int(time.time()) // 3600
    bot_key = os.environ.get("BOT_API_KEY", "anonymous")
    raw = f"{bot_key}:{contrib_id}:{action}:{hour_bucket}"
    return _hl.sha256(raw.encode()).hexdigest()[:32]


def _valid_contribution_token(token: str, contrib_id: int, action: str) -> bool:
    """Valida token HMAC accettando bucket ora corrente e precedente (finestra 2h)."""
    now_bucket = int(time.time()) // 3600
    for bucket in (now_bucket, now_bucket - 1):
        expected = _make_contribution_token(contrib_id, action, bucket)
        if _hmac.compare_digest(token, expected):
            return True
    return False


# ── SDK clients ──────────────────────────────────────────────────────────────

def get_trade_client():
    return Trade(key=API_KEY, secret=API_SECRET)

def get_user_client():
    return User(key=API_KEY, secret=API_SECRET)


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_kraken_servertime():
    try:
        r = requests.get(KRAKEN_BASE + "/derivatives/api/v3/servertime", timeout=5)
        return r.json().get("serverTime")
    except Exception:
        return None

def get_open_position(symbol: str):
    """
    Legge la posizione con lo SDK (auth=True) => niente firma manuale.
    Ritorna:
      None se flat
      { "side": "long"/"short", "size": float, "price": float } se aperta
    """
    trade = get_trade_client()
    result = trade.request(
        method="GET",
        uri="/derivatives/api/v3/openpositions",
        auth=True
    )
    open_positions = result.get("openPositions", []) or []
    for pos in open_positions:
        if (pos.get("symbol", "") or "").upper() == symbol.upper():
            size = float(pos.get("size", 0) or 0)
            if size == 0:
                return None
            side = (pos.get("side", "") or "").lower()
            if side not in ("long", "short"):
                side = "long" if size > 0 else "short"
            return {
                "side": side,
                "size": abs(size),
                "price": float(pos.get("price", 0) or 0),
            }
    return None


def wait_for_position(symbol: str, want_open: bool, retries: int = 10, sleep_s: float = 0.35):
    """
    want_open=True  -> aspetta che compaia una posizione
    want_open=False -> aspetta che sparisca (flat)
    """
    last = None
    for _ in range(retries):
        try:
            last = get_open_position(symbol)
            if want_open and last:
                return last
            if (not want_open) and (last is None):
                return None
        except Exception:
            pass
        time.sleep(sleep_s)
    return last


def _get_mark_price(symbol: str) -> float:
    """Ritorna il mark price corrente da Kraken Futures. 0.0 se fallisce."""
    try:
        trade = get_trade_client()
        result = trade.request(method="GET", uri="/derivatives/api/v3/tickers", auth=False)
        tickers = result.get("tickers", []) or []
        ticker = next((t for t in tickers if (t.get("symbol") or "").upper() == symbol.upper()), None)
        return float(ticker.get("markPrice") or 0) if ticker else 0.0
    except Exception:
        return 0.0


def _close_prev_bet_on_reverse(old_side: str, exit_price: float, closed_size: float):
    """
    Quando viene aperta una posizione opposta (reverse bet), aggiorna in Supabase
    il bet precedente che è stato chiuso automaticamente da Kraken.
    """
    try:
        supabase_url, supabase_key = _sb_config()
        if not supabase_url or not supabase_key or exit_price <= 0:
            return

        old_direction = "UP" if old_side == "long" else "DOWN"
        headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}

        # Trova il bet aperto più recente nella direzione opposta
        query = (
            f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
            f"?bet_taken=eq.true&correct=is.null&direction=eq.{old_direction}"
            f"&order=id.desc&limit=1&select=id,entry_fill_price,btc_price_entry,bet_size"
        )
        resp = requests.get(query, headers=headers, timeout=5)
        rows = resp.json() if resp.ok else []
        if not rows:
            return

        row = rows[0]
        bet_id = row["id"]
        entry_price = float(row.get("entry_fill_price") or row.get("btc_price_entry") or exit_price)
        bet_size = float(row.get("bet_size") or closed_size or 0.0005)

        if old_direction == "UP":
            pnl_gross = (exit_price - entry_price) * bet_size
        else:
            pnl_gross = (entry_price - exit_price) * bet_size

        fee = bet_size * (entry_price + exit_price) * TAKER_FEE  # entry + exit taker fee
        pnl_net = round(pnl_gross - fee, 6)
        correct = pnl_net > 0  # net-based: break-even con fee = LOSS

        # &correct=is.null → atomicità ottimistica: nessuna doppia risoluzione (S-17)
        patch_url = f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}&correct=is.null"
        patch_headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
        requests.patch(patch_url, json={
            "btc_price_exit":     exit_price,
            "exit_fill_price":    exit_price,
            "correct":            correct,
            "pnl_usd":            pnl_net,
            "close_reason":       "closed_by_reverse_bet",
            "has_real_exit_fill": False,
        }, headers=patch_headers, timeout=5)
    except Exception:
        pass  # non bloccare il flusso principale


# ── HEALTH ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    capital = float(os.environ.get("CAPITAL_USD") or os.environ.get("CAPITAL", 100))

    # wallet equity — fast, non-blocking
    wallet_equity = None
    try:
        user = get_user_client()
        flex = user.get_wallets().get("accounts", {}).get("flex", {})
        wallet_equity = float(flex.get("marginEquity") or 0) or None
    except Exception:
        pass

    # base_size — from bet-sizing logic (last 10 trades, default conf 0.62)
    base_size = 0.002
    supabase_ok = None
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            r = requests.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=correct,pnl_usd&bet_taken=eq.true&correct=not.is.null&order=id.desc&limit=10",
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                timeout=3,
            )
            supabase_ok = r.status_code == 200
            trades = r.json() if r.status_code == 200 else []
            if trades and len(trades) >= 3:
                results = [t.get("correct") for t in trades if t.get("correct") is not None]
                pnls = [float(t.get("pnl_usd") or 0) for t in trades]
                recent_pnl = sum(pnls[:5])
                streak, streak_type = 0, None
                for res in results:
                    if streak_type is None: streak_type = res; streak = 1
                    elif res == streak_type: streak += 1
                    else: break
                multiplier = 1.0
                if recent_pnl < -0.15: multiplier = 0.25
                elif streak_type == False and streak >= 2: multiplier = 0.5
                elif streak_type == True and streak >= 3:
                    _conf_h = float(request.args.get("confidence", 0.65))
                    multiplier = 1.5 if _conf_h >= 0.75 else 1.2
                base_size = round(max(0.001, min(0.005, 0.002 * multiplier)), 6)
    except Exception:
        pass

    _clean_bets = _get_clean_bet_count()
    _polygon_configured = bool(
        os.environ.get("POLYGON_PRIVATE_KEY") and os.environ.get("POLYGON_CONTRACT_ADDRESS")
    )
    return jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "serverTime": get_kraken_servertime(),
        "symbol": DEFAULT_SYMBOL,
        "api_key_set": bool(API_KEY),
        "version": "2.5.2",
        "dry_run": DRY_RUN,
        "supabase_table": SUPABASE_TABLE,
        "paused": _BOT_PAUSED,
        "bot_paused": bool(_BOT_PAUSED),
        "capital": capital,
        "wallet_equity": wallet_equity,
        "base_size": base_size,
        "confidence_threshold": float(os.environ.get("CONF_THRESHOLD", "0.55")),
        "xgb_gate_active": _clean_bets >= _XGB_GATE_MIN_BETS,
        "xgb_clean_bets": _clean_bets,
        "xgb_min_bets": _XGB_GATE_MIN_BETS,
        "polygon_configured": _polygon_configured,
        "supabase_ok": supabase_ok,
    })



# ── PUBLISH TELEGRAM ─────────────────────────────────────────────────────────

@app.route("/publish-telegram", methods=["POST"])
def publish_telegram():
    """Pubblica un messaggio (o foto+caption) sul canale Telegram pubblico.
    Protected by BOT_API_KEY. Rate-limited a 10/min.
    Body JSON: {text: str, parse_mode?: str, photo?: str}
    Se photo è presente (nome file in static/marketing_assets/) usa sendPhoto con caption.
    """
    err = _check_api_key()
    if err:
        return err
    _rl_key = f"pubtg:{request.headers.get('X-Api-Key', request.remote_addr)}"
    if not _check_rate_limit(_rl_key, max_calls=10):
        return jsonify({"error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    parse_mode = data.get("parse_mode", "HTML")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not tg_token:
        return jsonify({"error": "TELEGRAM_BOT_TOKEN not configured"}), 503
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "-1003762450968")
    photo_filename = data.get("photo")
    try:
        if photo_filename:
            # sendPhoto — caption max 1024 chars
            photo_path = os.path.join(
                os.path.dirname(__file__), "static", "marketing_assets",
                os.path.basename(photo_filename)
            )
            if not os.path.exists(photo_path):
                return jsonify({"error": f"photo not found: {photo_filename}"}), 404
            caption = text[:1024]
            with open(photo_path, "rb") as f:
                resp = requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendPhoto",
                    data={"chat_id": channel_id, "caption": caption, "parse_mode": parse_mode},
                    files={"photo": f},
                    timeout=20,
                )
        else:
            if len(text) > 4096:
                return jsonify({"error": "text too long (max 4096 chars)"}), 400
            resp = requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={"chat_id": channel_id, "text": text, "parse_mode": parse_mode},
                timeout=10,
            )
        result = resp.json()
        if not result.get("ok"):
            return jsonify({"error": result.get("description", "Telegram error")}), 502
        return jsonify({"ok": True, "message_id": result["result"]["message_id"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── BALANCE ──────────────────────────────────────────────────────────────────

@app.route("/balance", methods=["GET"])
def balance():
    err = _check_api_key()
    if err:
        return err
    try:
        user = get_user_client()
        result = user.get_wallets()
        flex = result.get("accounts", {}).get("flex", {})
        return jsonify({
            "status": "ok",
            "margin_equity": flex.get("marginEquity"),
            "available_margin": flex.get("availableMargin"),
            "pnl": flex.get("pnl"),
            "usdc": flex.get("currencies", {}).get("USDC", {}).get("available"),
            "usd": flex.get("currencies", {}).get("USD", {}).get("available"),
            "raw": result,
        })
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"status": "error", "error": "internal_error"}), 500


# ── POSITION ─────────────────────────────────────────────────────────────────

@app.route("/position", methods=["GET"])
def position():
    err = _check_api_key()
    if err:
        return err
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    try:
        pos = get_open_position(symbol)
        if pos:
            # Enrich with Supabase entry_fill_price when Kraken returns price=0
            if not pos.get("price") or float(pos.get("price") or 0) == 0:
                try:
                    sb_url, sb_key = _sb_config()
                    r = requests.get(
                        f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                        f"?bet_taken=eq.true&correct=is.null"
                        f"&select=id,entry_fill_price,btc_price_entry,direction,bet_size,pyramid_count"
                        f"&order=id.desc&limit=1",
                        headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                        timeout=3
                    )
                    if r.ok and r.json():
                        row = r.json()[0]
                        pos["price"] = float(row.get("entry_fill_price") or row.get("btc_price_entry") or 0)
                        pos["entry_fill_price"] = pos["price"]
                        pos["pyramid_count"] = row.get("pyramid_count", 0)
                except Exception:
                    pass
            return jsonify({"status": "open", "symbol": symbol, **pos})
        return jsonify({"status": "flat", "symbol": symbol})
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"status": "error", "error": "internal_error", "symbol": symbol}), 500


# ── CLOSE POSITION ───────────────────────────────────────────────────────────

@app.route("/close-position", methods=["POST"])
def close_position():
    err = _check_api_key()
    if err:
        return err
    _rl_key = f"close:{request.headers.get('X-Api-Key', request.remote_addr)}"
    if not _check_rate_limit(_rl_key, max_calls=50):
        return jsonify({"error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    if DRY_RUN:
        return jsonify({
            "status": "closed",
            "symbol": symbol,
            "dry_run": True,
            "message": "DRY_RUN active — no real order sent to Kraken",
        }), 200

    try:
        pos = get_open_position(symbol)
        if not pos:
            # A-11: SL potrebbe aver già chiuso la posizione su Kraken.
            # Controlla se esiste un bet orfano in Supabase e risolvilo.
            try:
                _sb_url, _sb_key = _sb_config()
                _sb_h   = {"apikey": _sb_key, "Authorization": f"Bearer {_sb_key}"}
                _explicit = data.get("bet_id")
                _oq = (
                    f"{_sb_url}/rest/v1/{SUPABASE_TABLE}"
                    f"?id=eq.{_explicit}&bet_taken=eq.true&correct=is.null"
                    f"&select=id,entry_fill_price,btc_price_entry,bet_size,direction"
                ) if _explicit else (
                    f"{_sb_url}/rest/v1/{SUPABASE_TABLE}"
                    f"?bet_taken=eq.true&correct=is.null"
                    f"&order=id.desc&limit=1"
                    f"&select=id,entry_fill_price,btc_price_entry,bet_size,direction"
                )
                _or = requests.get(_oq, headers=_sb_h, timeout=5)
                _orphans = _or.json() if _or.ok else []
            except Exception:
                _orphans = []

            if not _orphans:
                return jsonify({
                    "status": "no_position",
                    "message": "Nessuna posizione aperta, nulla da chiudere.",
                    "symbol": symbol
                })

            # Bet orfana trovata — SL ha già chiuso la posizione Kraken.
            # Risolvi usando il prezzo corrente da Binance.
            try:
                _px = requests.get(
                    "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                    timeout=5,
                )
                _cur_price = float(_px.json()["price"]) if _px.ok else 0.0
            except Exception:
                _cur_price = 0.0

            _row = _orphans[0]
            _obid = _row["id"]
            _entry = float(_row.get("entry_fill_price") or _row.get("btc_price_entry") or 0)
            _bsize = float(_row.get("bet_size") or 0.001)
            _dir   = _row.get("direction", "UP")

            if _cur_price > 0 and _entry > 0:
                if _dir == "UP":
                    _pg = (_cur_price - _entry) * _bsize
                    _correct = _cur_price > _entry
                    _adir = "UP" if _correct else "DOWN"
                else:
                    _pg = (_entry - _cur_price) * _bsize
                    _correct = _cur_price < _entry
                    _adir = "DOWN" if _correct else "UP"
                _fee = _bsize * (_entry + _cur_price) * TAKER_FEE
                _pnl = round(_pg - _fee, 6)
                _supabase_update(_obid, {
                    "exit_fill_price":  _cur_price,
                    "btc_price_exit":   _cur_price,
                    "correct":          _correct,
                    "actual_direction": _adir,
                    "pnl_usd":          _pnl,
                    "fees_total":       round(_fee, 6),
                    "close_reason":     "sl_already_closed",
                })
                app.logger.info(
                    f"[close-position] Orphan bet {_obid} risolta: "
                    f"SL già eseguito su Kraken. pnl={_pnl}, correct={_correct}"
                )
                return jsonify({
                    "status":          "resolved_orphan",
                    "message":         "Posizione Kraken già chiusa da SL — bet orfana risolta.",
                    "bet_id":          _obid,
                    "exit_price_used": _cur_price,
                    "pnl_usd":         _pnl,
                    "correct":         _correct,
                    "symbol":          symbol,
                })

            app.logger.warning(
                f"[close-position] Orphan bet {_obid} trovata ma prezzo non disponibile "
                f"(cur={_cur_price}, entry={_entry}) — wf02 riproverà."
            )
            return jsonify({
                "status":        "no_position",
                "message":       "Nessuna posizione aperta, nulla da chiudere.",
                "orphan_bet_id": _obid,
                "warning":       "Bet orfana trovata ma prezzo corrente non disponibile.",
                "symbol":        symbol,
            })

        close_side = "sell" if pos["side"] == "long" else "buy"
        size = pos["size"]

        trade = get_trade_client()

        # Cancella stop-loss reale se presente (evita doppia chiusura)
        sl_order_id = data.get("sl_order_id")
        if sl_order_id:
            try:
                trade.request(
                    method="POST",
                    uri="/derivatives/api/v3/cancelorder",
                    post_params={"order_id": sl_order_id},
                    auth=True,
                )
            except Exception:
                pass  # se già triggerato, non bloccare

        result = trade.create_order(
            orderType="mkt",
            symbol=symbol,
            side=close_side,
            size=size,
            reduceOnly=True,
        )

        ok = result.get("result") == "success"
        after = wait_for_position(symbol, want_open=False, retries=12, sleep_s=0.35)

        # ── Aggiorna Supabase se la chiusura è andata a buon fine ─────────────
        supabase_updated = False
        exit_fill_price = None
        pnl_net = None
        if ok:
            try:
                # Estrai fill price dall'order event Kraken
                events = result.get("sendStatus", {}).get("orderEvents", [])
                exit_fill_price = float(events[0]["price"]) if events else 0.0

                supabase_url, supabase_key = _sb_config()
                headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}

                # Cerca il bet aperto più recente (bet_id esplicito ha priorità)
                explicit_bet_id = data.get("bet_id")
                if explicit_bet_id:
                    query = (
                        f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
                        f"?id=eq.{explicit_bet_id}&select=id,entry_fill_price,btc_price_entry,bet_size,direction"
                    )
                else:
                    query = (
                        f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
                        f"?bet_taken=eq.true&correct=is.null"
                        f"&order=id.desc&limit=1"
                        f"&select=id,entry_fill_price,btc_price_entry,bet_size,direction"
                    )
                resp = requests.get(query, headers=headers, timeout=5)
                rows = resp.json() if resp.ok else []

                if rows and exit_fill_price and exit_fill_price > 0:
                    row = rows[0]
                    bet_id = row["id"]
                    entry_price = float(row.get("entry_fill_price") or row.get("btc_price_entry") or exit_fill_price)
                    bet_size = float(row.get("bet_size") or size or 0.001)
                    direction = row.get("direction", "UP")

                    if direction == "UP":
                        pnl_gross = (exit_fill_price - entry_price) * bet_size
                        actual_direction = "UP" if exit_fill_price > entry_price else "DOWN"
                    else:
                        pnl_gross = (entry_price - exit_fill_price) * bet_size
                        actual_direction = "DOWN" if exit_fill_price < entry_price else "UP"

                    fee = bet_size * (entry_price + exit_fill_price) * TAKER_FEE
                    pnl_net = round(pnl_gross - fee, 6)
                    correct = pnl_net > 0  # net-based: break-even con fee = LOSS

                    _supabase_update(bet_id, {
                        "exit_fill_price":    exit_fill_price,
                        "btc_price_exit":     exit_fill_price,
                        "correct":            correct,
                        "actual_direction":   actual_direction,
                        "pnl_usd":            pnl_net,
                        "fees_total":         round(fee, 6),
                        "has_real_exit_fill": True,
                        "close_reason":       "manual_close",
                    })
                    supabase_updated = True
                    app.logger.info(f"[close-position] Supabase updated: bet {bet_id}, pnl={pnl_net}, correct={correct}")
            except Exception as e:
                app.logger.warning(f"[close-position] Supabase update failed (non-critical): {e}")

        return jsonify({
            "status": "closed" if (ok and after is None) else ("closing" if ok else "failed"),
            "symbol": symbol,
            "closed_side": pos["side"],
            "close_order_side": close_side,
            "size": size,
            "exit_fill_price": exit_fill_price,
            "pnl_usd": pnl_net,
            "supabase_updated": supabase_updated,
            "position_after": after,
            "raw": result,
        }), (200 if ok else 400)

    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"status": "error", "error": "internal_error", "symbol": symbol}), 500


# ── BOT PAUSE / RESUME ───────────────────────────────────────────────────────

@app.route("/pause", methods=["POST"])
def pause_bot():
    err = _check_api_key()
    if err:
        return err
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT
    _BOT_PAUSED = True
    _BOT_PAUSED_REFRESHED_AT = time.time()
    _save_bot_paused(True)
    return jsonify({"paused": True, "message": "Bot in pausa — nessun nuovo trade"}), 200


@app.route("/resume", methods=["POST"])
def resume_bot():
    err = _check_api_key()
    if err:
        return err
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT
    _BOT_PAUSED = False
    _BOT_PAUSED_REFRESHED_AT = time.time()
    _save_bot_paused(False)
    return jsonify({"paused": False, "message": "Bot riattivato — trading ripreso"}), 200


# ── PLACE BET — helper privati ───────────────────────────────────────────────

def _get_clean_bet_count() -> int:
    """Restituisce il numero di bet con esito noto in SUPABASE_TABLE. Cache 10 min.
    Usato dal gate XGBoost per sapere se il dataset è abbastanza grande da fidarsi del modello."""
    global _XGB_CLEAN_BET_COUNT, _XGB_CLEAN_BET_CHECKED_AT
    if (
        _XGB_CLEAN_BET_COUNT is not None
        and time.time() - _XGB_CLEAN_BET_CHECKED_AT < _XGB_CLEAN_CACHE_TTL
    ):
        return _XGB_CLEAN_BET_COUNT
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            r = requests.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=id&bet_taken=eq.true&correct=not.is.null",
                headers={
                    "apikey": sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Prefer": "count=exact",
                    "Range": "0-0",
                },
                timeout=3,
            )
            if r.status_code in (200, 206):
                cr = r.headers.get("content-range", "")
                count = int(cr.split("/")[1]) if "/" in cr else 0
                _XGB_CLEAN_BET_COUNT = count
                _XGB_CLEAN_BET_CHECKED_AT = time.time()
                return count
    except Exception:
        pass
    return _XGB_CLEAN_BET_COUNT or 0


def _run_xgb_gate(direction: str, confidence: float, data: dict, current_hour_utc: int) -> tuple:
    """Esegue il dual-gate XGBoost. Restituisce (xgb_prob_up, early_exit_response_or_None).
    Se XGB non è disponibile o fallisce → (0.5, None) = continua normalmente."""
    import math as _math
    from datetime import datetime as _dt_xgb

    xgb_prob_up = 0.5
    if _XGB_MODEL is None:
        return xgb_prob_up, None

    # Bypass gate se dataset pulito insufficiente (modello potenzialmente tautologico)
    clean_count = _get_clean_bet_count()
    if clean_count < _XGB_GATE_MIN_BETS:
        app.logger.info(
            f"[XGB] Gate bypass — {clean_count}/{_XGB_GATE_MIN_BETS} bet pulite. "
            "Modello pre-Day0, gate disattivato fino a dataset sufficiente."
        )
        return xgb_prob_up, None

    try:
        _h = current_hour_utc
        _dow_xgb = _dt_xgb.utcnow().weekday()
        _session_xgb = 0 if _h < 8 else (1 if _h < 14 else 2)
        feat_row = [[
            confidence,
            float(data.get("fear_greed", data.get("fear_greed_value", 50))),
            float(data.get("rsi14", 50)),
            float(data.get("technical_score", 0)),
            _math.sin(2 * _math.pi * _h / 24),
            _math.cos(2 * _math.pi * _h / 24),
            float(_BIAS_MAP.get((data.get("technical_bias") or "").lower().strip(), 0)),
            1.0 if float(data.get("fear_greed_value", data.get("fear_greed", 50)) or 50) < 45 else 0.0,
            _math.sin(2 * _math.pi * _dow_xgb / 7),
            _math.cos(2 * _math.pi * _dow_xgb / 7),
            float(_session_xgb),
        ]]
        prob = _XGB_MODEL.predict_proba(feat_row)[0]  # [P(DOWN), P(UP)]
        xgb_prob_up = float(prob[1])
        xgb_direction = "UP" if prob[1] > 0.5 else "DOWN"
        if xgb_direction != direction:
            return xgb_prob_up, (jsonify({
                "status": "skipped",
                "reason": "xgb_disagree",
                "llm_direction": direction,
                "xgb_direction": xgb_direction,
                "xgb_prob_up": round(float(prob[1]), 3),
                "message": f"XGB predicts {xgb_direction}, LLM predicts {direction}. Skipping for safety.",
            }), 200)
    except Exception as e:
        app.logger.warning(f"[XGB] Check failed: {e}")
    return xgb_prob_up, None


def _check_pre_flight(direction: str, confidence: float) -> object:
    """Verifica bot_paused + circuit breaker. Restituisce Flask response se deve fermarsi, None se ok."""
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT
    if time.time() - _BOT_PAUSED_REFRESHED_AT > 300:
        _refresh_bot_paused()

    if _BOT_PAUSED:
        return jsonify({
            "status": "paused",
            "message": "Bot in pausa — nessun nuovo trade aperto",
            "direction": direction,
            "confidence": confidence,
        }), 200

    # Circuit breaker: 3 consecutive losses → auto-pause
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            r_cb = requests.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=correct&bet_taken=eq.true&correct=not.is.null"
                "&order=id.desc&limit=3",
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                timeout=5,
            )
            if r_cb.status_code == 200:
                last3 = r_cb.json()
                if len(last3) == 3 and all(row.get("correct") is False for row in last3):
                    try:
                        _save_bot_paused(True)
                    except Exception as _cb_save_err:
                        app.logger.error(f"[CIRCUIT_BREAKER] save_paused failed (DB down?): {_cb_save_err}")
                    app.logger.warning("[CIRCUIT_BREAKER] 3 consecutive losses → bot auto-paused")
                    return jsonify({
                        "status": "paused",
                        "reason": "circuit_breaker",
                        "message": "3 perdite consecutive — bot auto-pausato. Riattivare manualmente con /resume.",
                        "direction": direction,
                        "confidence": confidence,
                    }), 200
    except Exception as e:
        app.logger.warning(f"[CIRCUIT_BREAKER] check failed: {e}")
    return None


# ── PLACE BET ────────────────────────────────────────────────────────────────

@app.route("/place-bet", methods=["POST"])
def place_bet():
    err = _check_api_key()
    if err:
        return err
    _rl_key = f"bet:{request.headers.get('X-Api-Key', request.remote_addr)}"
    if not _check_rate_limit(_rl_key, max_calls=50):
        return jsonify({"error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    direction = (data.get("direction") or "").upper()
    confidence = _safe_float(data.get("confidence", 0), default=0.0, min_v=0.0, max_v=1.0)
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    raw_size = data.get("size", data.get("stake_usdc", 0.0001))
    size = _safe_float(raw_size, default=0.0001, min_v=0.0)
    if size <= 0:
        size = 0.0001

    if direction not in ("UP", "DOWN"):
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    # P0.2 — filtro ore morte (WR storico < 45% UTC, aggiornato da /reload-calibration)
    current_hour_utc = time.gmtime().tm_hour
    if current_hour_utc in DEAD_HOURS_UTC:
        return jsonify({
            "status": "skipped",
            "reason": "dead_hour",
            "hour_utc": current_hour_utc,
            "message": f"Hour {current_hour_utc}h UTC has historically low WR (<45%). Skipping bet."
        }), 200

    # Dual-gate: bet solo se XGB direction == LLM direction
    xgb_prob_up, xgb_early_exit = _run_xgb_gate(direction, confidence, data, current_hour_utc)
    if xgb_early_exit:
        return xgb_early_exit

    desired_side = "long" if direction == "UP" else "short"

    # Temporary DOWN-bet kill switch (env DISABLE_DOWN_BETS=true → skip all shorts)
    if direction == "DOWN" and os.environ.get("DISABLE_DOWN_BETS", "").lower() == "true":
        return jsonify({
            "status": "skipped",
            "reason": "DOWN bets disabled (DISABLE_DOWN_BETS=true)",
            "direction": direction,
            "confidence": confidence,
            "symbol": symbol,
        }), 200

    # Bot paused + circuit breaker
    pre_flight = _check_pre_flight(direction, confidence)
    if pre_flight:
        return pre_flight

    if DRY_RUN:
        fake_id = f"DRY_{int(time.time())}"
        return jsonify({
            "status": "placed",
            "direction": direction,
            "confidence": confidence,
            "symbol": symbol,
            "side": "buy" if direction == "UP" else "sell",
            "size": size,
            "order_id": fake_id,
            "send_status_type": "placed",
            "position_confirmed": True,
            "position": {"side": desired_side, "size": size, "price": 0},
            "previous_position_existed": False,
            "raw": {"result": "success", "dry_run": True},
            "dry_run": True,
        }), 200

    try:
        pos = get_open_position(symbol)

        # NO stacking: se stessa direzione => valuta pyramid o skip
        if pos and pos["side"] == desired_side:
            existing_bet_info = {}
            kraken_entry_price = float(pos.get("price", 0) or 0)
            # default conservativo: non pyramisare se non riusciamo a leggere Supabase
            pyramid_count_existing = 1
            try:
                sb_url, sb_key = _sb_config()
                if sb_url and sb_key:
                    # Hard cap: count ALL open bets before pyramid logic
                    r_all = requests.get(
                        f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                        "?select=id&bet_taken=eq.true&correct=is.null",
                        headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}",
                                 "Prefer": "count=exact"},
                        timeout=5,
                    )
                    open_count = 0
                    if r_all.status_code == 200:
                        cr = r_all.headers.get("content-range", "")
                        try:
                            open_count = int(cr.split("/")[1]) if "/" in cr else len(r_all.json())
                        except Exception:
                            open_count = len(r_all.json())
                    if open_count >= 2:
                        app.logger.warning(f"[pyramid] Hard cap: {open_count} open bets — blocking new position")
                        return jsonify({
                            "status": "skipped",
                            "reason": f"MAX_OPEN_BETS reached ({open_count} open bets)",
                            "symbol": symbol,
                            "direction": direction,
                            "no_stack": True,
                        }), 200
                    # Fetch latest open bet for pyramid_count info
                    r = requests.get(
                        f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                        "?select=id,created_at,direction,entry_fill_price,pyramid_count"
                        "&bet_taken=eq.true&correct=is.null&order=id.desc&limit=1",
                        headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                        timeout=5,
                    )
                    if r.status_code == 200 and r.json():
                        row = r.json()[0]
                        existing_bet_info = {
                            "existing_bet_id": row.get("id"),
                            "existing_bet_created_at": row.get("created_at"),
                            "existing_bet_entry": row.get("entry_fill_price"),
                        }
                        pyramid_count_existing = int(row.get("pyramid_count") or 0)
            except Exception:
                pass

            existing_entry_price = existing_bet_info.get("existing_bet_entry") or kraken_entry_price

            # --- Pyramid evaluation ---
            pyramid_size = max(0.001, float(os.environ.get("BASE_SIZE", "0.002")) * 0.5)
            current_pos_size = float(pos.get("size", 0))
            mark_price = _get_mark_price(symbol) or float(existing_entry_price or 0)

            # PnL% posizione corrente
            current_pnl_pct = 0.0
            if existing_entry_price and float(existing_entry_price) > 0:
                _sign = 1 if pos["side"] == "long" else -1
                current_pnl_pct = (mark_price - float(existing_entry_price)) / float(existing_entry_price) * _sign

            # Età posizione in minuti
            position_age_min = 0
            bet_created_at = existing_bet_info.get("existing_bet_created_at")
            if bet_created_at:
                try:
                    from datetime import datetime, timezone
                    _created_dt = datetime.fromisoformat(bet_created_at.replace("Z", "+00:00"))
                    position_age_min = (datetime.now(timezone.utc) - _created_dt).total_seconds() / 60
                except Exception:
                    pass

            can_pyramid = False
            pyramid_reason = None
            if pyramid_count_existing == 0 and (current_pos_size + pyramid_size) <= 0.005:
                # Condizione B — Strong signal: XGB prob > 0.70 AND conf > 0.72 (bypassa PnL)
                _strong_xgb = xgb_prob_up if direction == "UP" else (1.0 - xgb_prob_up)
                if _strong_xgb > 0.70 and confidence > 0.72:
                    can_pyramid = True
                    pyramid_reason = "B"
                # Condizione A — Standard: posizione matura, in profitto, conf alta
                if not can_pyramid and position_age_min > 15 and current_pnl_pct > 0.003 and confidence > 0.70:
                    can_pyramid = True
                    pyramid_reason = "A"

            if can_pyramid:
                try:
                    trade = get_trade_client()
                    _order_side = "buy" if direction == "UP" else "sell"
                    pyramid_result = trade.create_order(
                        orderType="mkt",
                        symbol=symbol,
                        side=_order_side,
                        size=pyramid_size,
                    )
                    # UPDATE Supabase: pyramid_count=1, bet_size aggiornata
                    bet_id = existing_bet_info.get("existing_bet_id")
                    if bet_id:
                        _sb_url, _sb_key = _sb_config()
                        try:
                            _patch_resp = requests.patch(
                                f"{_sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}",
                                json={"pyramid_count": pyramid_count_existing + 1, "bet_size": round(current_pos_size + pyramid_size, 4)},
                                headers={
                                    "apikey": _sb_key,
                                    "Authorization": f"Bearer {_sb_key}",
                                    "Content-Type": "application/json",
                                    "Prefer": "return=minimal",
                                },
                                timeout=5,
                            )
                            if _patch_resp.status_code not in (200, 204):
                                app.logger.error(f"[pyramid] PATCH pyramid_count failed {_patch_resp.status_code}: {_patch_resp.text[:200]}")
                        except Exception as _e:
                            app.logger.error(f"[pyramid] PATCH pyramid_count exception: {_e}")
                            sentry_sdk.capture_exception(_e)
                    _pyr_order_id = ""
                    try:
                        _pyr_order_id = str(pyramid_result.get("sendStatus", {}).get("order_id", ""))
                    except Exception:
                        pass
                    return jsonify({
                        "status": "pyramided",
                        "direction": direction,
                        "existing_bet_id": existing_bet_info.get("existing_bet_id"),
                        "pyramid_add_size": pyramid_size,
                        "total_position_size": round(current_pos_size + pyramid_size, 4),
                        "current_pnl_pct": round(current_pnl_pct, 4),
                        "pyramid_reason": pyramid_reason,
                        "order_id": _pyr_order_id,
                        "confidence": confidence,
                        "symbol": symbol,
                    }), 200
                except Exception:
                    pass  # pyramid fallito → skip normale

            # Skip normale (pyramid non possibile o fallito)
            return jsonify({
                "status": "skipped",
                "reason": f"Posizione {pos['side']} già aperta nella stessa direzione (no stacking).",
                "symbol": symbol,
                "existing_position": pos,
                "existing_entry_price": existing_entry_price,
                "confidence": confidence,
                "direction": direction,
                "no_stack": True,
                **existing_bet_info,
            }), 200

        trade = get_trade_client()

        # se opposta => controlla PnL corrente prima di invertire (R-03)
        if pos and pos["side"] != desired_side:
            # Leggi il prezzo corrente PRIMA di fare qualsiasi ordine
            current_mark = _get_mark_price(symbol) or float(pos.get("price") or 0)
            entry_p = float(pos.get("price") or 0)

            # Calcola se la posizione esistente è in profitto
            in_profit = False
            if entry_p > 0 and current_mark > 0:
                in_profit = (
                    (current_mark > entry_p) if pos["side"] == "long"
                    else (current_mark < entry_p)
                )

            # Se in profitto: ignora il segnale inverso — non tagliare un vincitore
            if in_profit:
                app.logger.info(
                    f"[reverse-bet] Posizione {pos['side']} in profitto "
                    f"(entry={entry_p:.1f} mark={current_mark:.1f}) — segnale opposto ignorato"
                )
                return jsonify({
                    "status": "skipped",
                    "reason": "Posizione opposta in profitto — reverse ignorato per preservare guadagno",
                    "symbol": symbol,
                    "existing_side": pos["side"],
                    "current_mark": current_mark,
                    "entry_price": entry_p,
                    "confidence": confidence,
                }), 200

            # Se in perdita ma confidence < 0.75: non abbastanza segnale per invertire
            if confidence < 0.75:
                app.logger.info(
                    f"[reverse-bet] Posizione {pos['side']} in perdita ma conf={confidence:.2f} < 0.75 — skip reverse"
                )
                return jsonify({
                    "status": "skipped",
                    "reason": f"Reverse: posizione in perdita ma confidence ({confidence:.2f}) < 0.75 — non invertire",
                    "symbol": symbol,
                    "confidence": confidence,
                }), 200

            # Procedi: posizione in perdita E confidence >= 0.75 → inverti
            app.logger.info(
                f"[reverse-bet] Posizione {pos['side']} in perdita (entry={entry_p:.1f} mark={current_mark:.1f}) "
                f"e conf={confidence:.2f} >= 0.75 — procedo con inversione"
            )
            close_side = "sell" if pos["side"] == "long" else "buy"
            trade.create_order(
                orderType="mkt",
                symbol=symbol,
                side=close_side,
                size=pos["size"],
                reduceOnly=True,
            )
            wait_for_position(symbol, want_open=False, retries=15, sleep_s=0.35)
            exit_price_at_close = _get_mark_price(symbol) or current_mark
            _close_prev_bet_on_reverse(pos["side"], exit_price_at_close, pos["size"])
            time.sleep(2)  # buffer Kraken: attendi che il conto si assesti prima di aprire nuova posizione

        # apri nuova posizione
        order_side = "buy" if direction == "UP" else "sell"
        result = trade.create_order(
            orderType="mkt",
            symbol=symbol,
            side=order_side,
            size=size,
        )

        ok = result.get("result") == "success"
        send_status = result.get("sendStatus", {}) or {}
        send_status_type = send_status.get("status", "")
        order_id = send_status.get("order_id")

        # Kraken può restituire result="success" ma sendStatus.status="invalidSize"
        # (o altri errori) quando l'ordine non è effettivamente piazzato.
        FAILED_SEND_STATUSES = {
            "invalidSize", "invalidOrderType", "invalidSide",
            "unknownError", "insufficientAvailableFunds",
            "marketSuspended", "tooManyRequests",
        }
        if send_status_type in FAILED_SEND_STATUSES:
            ok = False

        confirmed_pos = wait_for_position(symbol, want_open=True, retries=15, sleep_s=0.35) if ok else None
        position_confirmed = confirmed_pos is not None

        # ── Piazza Stop-Loss reale su Kraken + calcola TP/RR ─────────────────
        sl_order_id = None
        sl_price    = None
        tp_price    = None
        rr_ratio    = None
        fill_price  = None
        if ok and confirmed_pos:
            try:
                # Execution event price (stesso source usato da n8n Save Entry Fill)
                _order_events = (result.get("sendStatus") or {}).get("orderEvents") or []
                _exec = next((e for e in _order_events if e.get("type") == "EXECUTION"), None)
                if _exec and _exec.get("price"):
                    fill_price = float(_exec["price"])

                sl_pct = float(data.get("sl_pct", 1.2))
                tp_pct = float(data.get("tp_pct", sl_pct * 2))  # default 2× SL
                entry_price = fill_price or float(confirmed_pos.get("price") or 0) or _get_mark_price(symbol)
                if entry_price > 0:
                    if not fill_price:
                        fill_price = entry_price
                    if direction == "UP":
                        sl_price = round(entry_price * (1 - sl_pct / 100), 1)
                        tp_price = round(entry_price * (1 + tp_pct / 100), 1)
                        sl_side  = "sell"
                    else:
                        sl_price = round(entry_price * (1 + sl_pct / 100), 1)
                        tp_price = round(entry_price * (1 - tp_pct / 100), 1)
                        sl_side  = "buy"
                    sl_dist  = abs(entry_price - sl_price)
                    tp_dist  = abs(entry_price - tp_price)
                    rr_ratio = round(tp_dist / sl_dist, 2) if sl_dist > 0 else None
                    sl_result = trade.create_order(
                        orderType="stp",
                        symbol=symbol,
                        side=sl_side,
                        size=size,
                        stopPrice=sl_price,
                        reduceOnly=True,
                    )
                    sl_status = sl_result.get("sendStatus", {}) or {}
                    if sl_status.get("status") not in FAILED_SEND_STATUSES:
                        sl_order_id = sl_status.get("order_id")
            except Exception:
                pass  # non bloccare il flusso principale

        return jsonify({
            "status": "placed" if ok else "failed",
            "direction": direction,
            "confidence": confidence,
            "symbol": symbol,
            "side": order_side,
            "size": size,
            "order_id": order_id,
            "send_status_type": send_status_type,
            "position_confirmed": position_confirmed,
            "position": confirmed_pos,
            "previous_position_existed": pos is not None,
            "fill_price":  fill_price,
            "sl_order_id": sl_order_id,
            "sl_price":    sl_price,
            "tp_price":    tp_price,
            "rr_ratio":    rr_ratio,
            "raw": result,
        }), (200 if ok else 400)

    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"status": "error", "error": "internal_error"}), 500

# ── BTC PRICE (Kraken Futures mark price) ────────────────────────────────────

@app.route("/btc-price", methods=["GET"])
def get_btc_price():
    try:
        trade = get_trade_client()
        result = trade.request(
            method="GET",
            uri="/derivatives/api/v3/tickers",
            auth=False
        )
        tickers = result.get("tickers", []) or []
        ticker = next(
            (t for t in tickers if (t.get("symbol") or "").upper() == "PF_XBTUSD"),
            None
        )
        if not ticker:
            return jsonify({"error": "ticker PF_XBTUSD not found"}), 404

        return jsonify({
            "symbol":     "PF_XBTUSD",
            "mark_price": float(ticker.get("markPrice") or 0),
            "last_price": float(ticker.get("last")      or 0),
            "bid":        float(ticker.get("bid")        or 0),
            "ask":        float(ticker.get("ask")        or 0),
            "funding_rate": ticker.get("fundingRate"),
        })
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500



# ── EXECUTION FEES ───────────────────────────────────────────────────────────

@app.route("/execution-fees", methods=["GET"])
def get_execution_fees():
    order_id = request.args.get("order_id")
    if not order_id:
        return jsonify({"error": "order_id required"}), 400

    try:
        trade = get_trade_client()
        result = trade.request(
            method="GET",
            uri="/derivatives/api/v3/fills",
            auth=True
        )
        fills = result.get("fills", []) or []

        order_fills = [f for f in fills if f.get("order_id") == order_id]

        total_fee = sum(
            float(f.get("fee", 0) or 0) or
            (float(f.get("size", 0)) * float(f.get("price", 0)) * TAKER_FEE)
            for f in order_fills
        )

        fee_currency = order_fills[0].get("fee_currency", "USD") if order_fills else "USD"

        return jsonify({
            "order_id":     order_id,
            "total_fee":    round(total_fee, 8),
            "fee_currency": fee_currency,
            "fills_found":  len(order_fills),
            "fills":        order_fills,
        })
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500

# ── ACCOUNT SUMMARY (tutto in uno) ───────────────────────────────────────────

@app.route("/account-summary", methods=["GET"])
def account_summary():
    err = _check_read_key()
    if err:
        return err
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    try:
        _TIMEOUT = 8  # secondi per singola call Kraken

        # ── Fetch parallelo: 5 chiamate Kraken indipendenti ──────────────────
        def _fetch_wallets():
            try:
                return get_user_client().get_wallets()
            except Exception:
                return {}

        def _fetch_tickers():
            try:
                return get_trade_client().request(
                    method="GET", uri="/derivatives/api/v3/tickers",
                    auth=False, timeout=_TIMEOUT
                ).get("tickers", [])
            except Exception:
                return []

        def _fetch_position():
            try:
                result = get_trade_client().request(
                    method="GET", uri="/derivatives/api/v3/openpositions",
                    auth=True, timeout=_TIMEOUT
                )
                for p in result.get("openPositions", []) or []:
                    if (p.get("symbol", "") or "").upper() == symbol.upper():
                        size = float(p.get("size", 0) or 0)
                        if size == 0:
                            return None
                        side = (p.get("side", "") or "").lower()
                        if side not in ("long", "short"):
                            side = "long" if size > 0 else "short"
                        return {"side": side, "size": abs(size), "price": float(p.get("price", 0) or 0)}
                return None
            except Exception:
                return None

        def _fetch_openorders():
            try:
                return get_trade_client().request(
                    method="GET", uri="/derivatives/api/v3/openorders",
                    auth=True, timeout=_TIMEOUT
                ).get("openOrders", []) or []
            except Exception:
                return []

        def _fetch_fills():
            try:
                return get_trade_client().request(
                    method="GET", uri="/derivatives/api/v3/fills",
                    auth=True, timeout=_TIMEOUT
                ).get("fills", []) or []
            except Exception:
                return []

        with ThreadPoolExecutor(max_workers=5) as pool:
            f_w = pool.submit(_fetch_wallets)
            f_t = pool.submit(_fetch_tickers)
            f_p = pool.submit(_fetch_position)
            f_o = pool.submit(_fetch_openorders)
            f_f = pool.submit(_fetch_fills)

            def _safe(future, default):
                try:
                    return future.result(timeout=_TIMEOUT + 2)
                except Exception:
                    return default

            wallets_raw = _safe(f_w, {})
            all_tickers = _safe(f_t, [])
            pos         = _safe(f_p, None)
            orders_raw  = _safe(f_o, [])
            fills_raw   = _safe(f_f, [])

        # ── 1. WALLET ────────────────────────────────────────────────────────
        flex = wallets_raw.get("accounts", {}).get("flex", {})

        usdc_available  = flex.get("currencies", {}).get("USDC", {}).get("available")
        usd_available   = flex.get("currencies", {}).get("USD",  {}).get("available")
        margin_equity   = flex.get("marginEquity")
        available_margin= flex.get("availableMargin")
        portfolio_value = flex.get("portfolioValue")
        collateral      = flex.get("collateralValue")
        pnl_unrealized  = flex.get("totalUnrealized")       # P&L mark-to-market aggregato
        funding_unrealized = flex.get("unrealizedFunding")  # funding maturato non ancora pagato
        initial_margin  = flex.get("initialMargin")
        maint_margin    = flex.get("maintenanceMargin")

        # margin usage %
        margin_usage_pct = None
        if portfolio_value and portfolio_value > 0 and initial_margin is not None:
            margin_usage_pct = round((initial_margin / portfolio_value) * 100, 2)

        # ── 2. POSIZIONE APERTA ──────────────────────────────────────────────
        # P&L della posizione se aperta: (mark - entry) * size * direction
        position_pnl = None
        position_pnl_pct = None
        if pos:
            try:
                ticker = next(
                    (t for t in all_tickers if (t.get("symbol") or "").upper() == symbol.upper()),
                    None
                )
                if ticker and pos["price"] > 0:
                    mark = float(ticker.get("markPrice") or 0)
                    direction = 1 if pos["side"] == "long" else -1
                    position_pnl = round((mark - pos["price"]) * direction * pos["size"], 6)
                    position_pnl_pct = round((position_pnl / (pos["price"] * pos["size"])) * 100, 4)
            except Exception:
                pass

        # ── 3. PREZZO BTC ────────────────────────────────────────────────────
        btc_data = {}
        try:
            ticker_btc = next(
                (t for t in all_tickers if (t.get("symbol") or "").upper() == "PF_XBTUSD"),
                None
            )
            if ticker_btc:
                btc_data = {
                    "mark_price":   float(ticker_btc.get("markPrice") or 0),
                    "last_price":   float(ticker_btc.get("last")      or 0),
                    "bid":          float(ticker_btc.get("bid")        or 0),
                    "ask":          float(ticker_btc.get("ask")        or 0),
                    "funding_rate": ticker_btc.get("fundingRate"),         # rate attuale (es. 0.0001)
                    "funding_rate_pct": round(float(ticker_btc.get("fundingRate") or 0) * 100, 6),
                    "open_interest": ticker_btc.get("openInterest"),
                    "volume_24h":   ticker_btc.get("vol24h"),
                }
        except Exception:
            pass

        # ── 4. ORDINI APERTI ─────────────────────────────────────────────────
        open_orders = [
            {
                "order_id":   o.get("order_id"),
                "symbol":     o.get("symbol"),
                "side":       o.get("side"),
                "type":       o.get("orderType"),
                "size":       o.get("size"),
                "limit_price": o.get("limitPrice"),
                "stop_price": o.get("stopPrice"),
                "filled":     o.get("filled"),
                "reduce_only": o.get("reduceOnly"),
                "timestamp":  o.get("timestamp"),
            }
            for o in orders_raw
            if (o.get("symbol") or "").upper() == symbol.upper()
        ]

        # ── 5. ULTIMI 5 FILL (P&L realizzato recente) ────────────────────────
        recent_fills = []
        realized_pnl_recent = 0.0
        symbol_fills = [
            f for f in fills_raw
            if (f.get("symbol") or "").upper() == symbol.upper()
        ][:5]
        for f in symbol_fills:
            # Kraken fills don't return 'fee' or 'pnl' fields — calculate fee manually
            size_f  = float(f.get("size",  0) or 0)
            price_f = float(f.get("price", 0) or 0)
            fee_raw = float(f.get("fee",   0) or 0)
            fee = fee_raw if fee_raw > 0 else round(size_f * price_f * TAKER_FEE, 6)
            realized_pnl_recent -= fee  # fees are a cost (negative contribution)
            recent_fills.append({
                "order_id":  f.get("order_id"),
                "side":      f.get("side"),
                "size":      f.get("size"),
                "price":     f.get("price"),
                "pnl":       None,   # not available per-fill from Kraken API
                "fee":       fee,
                "timestamp": f.get("fillTime"),
            })

        # ── 6. OPEN BETS (Supabase) ──────────────────────────────────────────────
        open_bets = []
        try:
            supabase_url, supabase_key = _sb_config()
            supabase_headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
            r_bets = requests.get(
                f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=id,created_at,direction,confidence,bet_size"
                "&bet_taken=eq.true&correct=is.null&order=id.desc",
                headers=supabase_headers,
                timeout=3
            )
            if r_bets.ok:
                open_bets = r_bets.json() or []
        except Exception:
            pass

        # ── RISPOSTA FINALE ──────────────────────────────────────────────────
        return jsonify({
            "status": "ok",
            "symbol": symbol,
            "timestamp": get_kraken_servertime(),

            "wallet": {
                "usdc_available":    usdc_available,
                "usd_available":     usd_available,
                "margin_equity":     margin_equity,
                "available_margin":  available_margin,
                "portfolio_value":   portfolio_value,
                "collateral":        collateral,
                "initial_margin":    initial_margin,
                "maintenance_margin": maint_margin,
                "margin_usage_pct":  margin_usage_pct,
                "pnl_unrealized":    pnl_unrealized,
                "funding_unrealized": funding_unrealized,
            },

            "position": {
                "open":         pos is not None,
                "side":         pos["side"]  if pos else None,
                "size":         pos["size"]  if pos else None,
                "entry_price":  pos["price"] if pos else None,
                "pnl":          position_pnl,
                "pnl_pct":      position_pnl_pct,
            },

            "btc": btc_data,

            "open_orders": {
                "count":  len(open_orders),
                "orders": open_orders,
            },

            "recent_activity": {
                "fills_count":          len(recent_fills),
                "realized_pnl_recent":  round(realized_pnl_recent, 6),
                "fills":                recent_fills,
            },

            "open_bets": open_bets,
        })

    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"status": "error", "error": "internal_error"}), 500

# ── SIGNALS PROXY (Supabase) ─────────────────────────────────────────────────

@app.route("/signals", methods=["GET"])
def get_signals():
    err = _check_read_key()
    if err:
        return err
    try:
        try:
            limit = max(1, min(int(request.args.get("limit", 500)), 2000))
        except (ValueError, TypeError):
            limit = 500

        try:
            days = max(0, min(int(request.args.get("days", 0)), 365))
        except (ValueError, TypeError):
            days = 0

        supabase_url, supabase_key = _sb_config()

        if not supabase_url or not supabase_key:
            return jsonify({"error": "Supabase credentials not configured"}), 500

        include_history = request.args.get("include_history", "false").lower() == "true"

        url = f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?select=*&order=id.desc&limit={limit}"
        if days > 0:
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            url += f"&created_at=gte.{since}"

        sb_headers = {
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}",
            "Prefer": "count=exact"
        }
        res = requests.get(url, headers=sb_headers, timeout=10)

        if not res.ok:
            return jsonify({"error": f"Supabase HTTP {res.status_code}"}), 502

        # Parse total count from Content-Range header (e.g. "0-499/1243")
        total_count = None
        cr = res.headers.get("Content-Range", "")
        if "/" in cr:
            try:
                total_count = int(cr.split("/")[1])
            except (ValueError, IndexError):
                pass

        data = res.json()
        if total_count is None:
            total_count = len(data) if isinstance(data, list) else 0

        # Merge pre-day0 historical bets if requested
        if include_history and SUPABASE_TABLE == "btc_predictions":
            hist_url = (f"{supabase_url}/rest/v1/btc_predictions_pre_day0"
                        f"?select=*&bet_taken=eq.true&order=id.desc&limit=2000")
            hist_res = requests.get(hist_url, headers=sb_headers, timeout=10)
            if hist_res.ok:
                hist_data = hist_res.json()
                if isinstance(hist_data, list):
                    for row in hist_data:
                        row["_source"] = "pre_day0"
                    data = data + hist_data
                    data.sort(key=lambda r: r.get("id", 0), reverse=True)
                    total_count += len(hist_data)

        return jsonify({
            "data": data,
            "total_count": total_count,
            "fetched": len(data),
            "has_more": len(data) >= limit,
        })

    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500

# ── PERFORMANCE STATS ────────────────────────────────────────────────────────

@app.route("/performance-stats", methods=["GET"])
def performance_stats():
    """
    Calcola statistiche storiche live da Supabase e restituisce un testo
    compatto da iniettare nel prompt di Claude come contesto di calibrazione.
    """
    try:
        supabase_url, supabase_key = _sb_config()
        if not supabase_url or not supabase_key:
            return jsonify({"perf_stats_text": "n/a (no Supabase config)"})

        # Fetch ultimi 50 bet risolti
        url = (
            f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=direction,confidence,correct,pnl_usd,created_at"
            "&bet_taken=eq.true&correct=not.is.null"
            "&order=id.desc&limit=50"
        )
        res = requests.get(url, headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }, timeout=5)
        if not res.ok:
            return jsonify({"perf_stats_text": "n/a (Supabase error)"}), 200
        rows = res.json()

        if not rows or len(rows) < 5:
            return jsonify({"perf_stats_text": "n/a (insufficient history)"})

        from datetime import datetime, timezone

        current_hour = datetime.now(timezone.utc).hour

        # ── Recent WR (last 10) ──────────────────────────────────────────────
        last10 = rows[:10]
        w10 = sum(1 for r in last10 if r.get("correct") is True)
        l10 = len(last10) - w10
        wr10 = round(w10 / len(last10) * 100)

        # ── Current streak ───────────────────────────────────────────────────
        streak, streak_val = 0, None
        for r in rows:
            v = r.get("correct")
            if streak_val is None:
                streak_val, streak = v, 1
            elif v == streak_val:
                streak += 1
            else:
                break
        streak_label = f"{streak} {'WIN' if streak_val else 'LOSS'}"

        # ── Last 5 PnL ───────────────────────────────────────────────────────
        pnl5 = sum(float(r.get("pnl_usd") or 0) for r in rows[:5])
        pnl5_str = f"+${pnl5:.2f}" if pnl5 >= 0 else f"-${abs(pnl5):.2f}"

        # ── Hour WR (current UTC hour) ───────────────────────────────────────
        hour_rows = []
        for r in rows:
            try:
                h = int((r.get("created_at") or "T00:")[11:13])
                if h == current_hour:
                    hour_rows.append(r)
            except Exception:
                pass
        if len(hour_rows) >= 3:
            hw = sum(1 for r in hour_rows if r.get("correct") is True)
            hour_wr = f"WR {round(hw/len(hour_rows)*100)}% ({len(hour_rows)} bets)"
        else:
            hour_wr = "insufficient data"

        # ── Direction WR ─────────────────────────────────────────────────────
        up_rows   = [r for r in rows if r.get("direction") == "UP"]
        down_rows = [r for r in rows if r.get("direction") == "DOWN"]
        def _wr(lst):
            if not lst:
                return "n/a"
            w = sum(1 for r in lst if r.get("correct") is True)
            return f"{round(w/len(lst)*100)}% ({len(lst)})"
        dir_stats = f"UP→{_wr(up_rows)} | DOWN→{_wr(down_rows)}"

        # ── Confidence bucket WR ─────────────────────────────────────────────
        def _bucket_wr(lo, hi):
            b = [r for r in rows if lo <= float(r.get("confidence") or 0) < hi]
            if len(b) < 3:
                return "n/a"
            w = sum(1 for r in b if r.get("correct") is True)
            return f"{round(w/len(b)*100)}%({len(b)})"
        conf_stats = (
            f"[<0.65]→{_bucket_wr(0.50,0.65)} "
            f"[0.65-0.70]→{_bucket_wr(0.65,0.70)} "
            f"[≥0.70]→{_bucket_wr(0.70,1.01)}"
        )

        stats_text = (
            f"Last 10 bets: {w10}W/{l10}L ({wr10}%) | Streak: {streak_label} | Last5 PnL: {pnl5_str}\n"
            f"Hour {current_hour:02d}h UTC: {hour_wr}\n"
            f"Direction WR: {dir_stats}\n"
            f"Confidence calibration: {conf_stats}"
        )

        # ── Append error patterns snippet (da analyze_errors.py) ─────────────
        try:
            ep_path = os.path.join(os.path.dirname(__file__), "datasets", "error_patterns.json")
            if os.path.exists(ep_path):
                import time as _time
                age_days = (_time.time() - os.path.getmtime(ep_path)) / 86400
                if age_days < 8:  # ignora se più vecchio di 8 giorni
                    with open(ep_path, "r") as _f:
                        ep = json.load(_f)
                    snippet = ep.get("prompt_snippet", "")
                    if snippet:
                        stats_text += "\n\n" + snippet
        except Exception:
            pass

        return jsonify({"perf_stats_text": stats_text})

    except Exception as e:
        app.logger.error("perf_stats error: %s", e)
        return jsonify({"perf_stats_text": "n/a"})


# ── XGB PREDICT ──────────────────────────────────────────────────────────────

@app.route("/predict-xgb", methods=["GET"])
def predict_xgb():
    """
    Predice la direzione BTC con XGBoost e confronta con Claude.
    Params: claude_direction, confidence, fear_greed_value, rsi14,
            technical_score, hour_utc, ema_trend, technical_bias,
            signal_technical, signal_sentiment, signal_fear_greed, signal_volume
    Returns: { xgb_direction, xgb_prob_up, xgb_prob_down, claude_direction, agree }
    """
    err = _check_api_key()
    if err:
        return err
    claude_dir = request.args.get("claude_direction", "")

    # Fail-open: se modello non disponibile, non bloccare il trade
    if _XGB_MODEL is None:
        return jsonify({"xgb_direction": None, "agree": True, "reason": "model_not_loaded"})

    try:
        ema_trend    = request.args.get("ema_trend", "").lower()
        tech_bias    = request.args.get("technical_bias", "").lower()
        sig_tech     = request.args.get("signal_technical", "").lower()
        sig_sent     = request.args.get("signal_sentiment", "").lower()
        sig_fg       = request.args.get("signal_fear_greed", "").lower()
        sig_vol      = request.args.get("signal_volume", "").lower()

        from datetime import datetime as _dt_xgb2
        _h2 = _safe_int(request.args.get("hour_utc", 12), default=12, min_v=0, max_v=23)
        _dow2 = _dt_xgb2.utcnow().weekday()  # 0=Mon..6=Sun
        _session2 = 0 if _h2 < 8 else (1 if _h2 < 14 else 2)  # 0=Asia 1=London 2=NY
        _fg2 = _safe_float(request.args.get("fear_greed_value", 50), default=50.0, min_v=0.0, max_v=100.0)
        features = [[
            _safe_float(request.args.get("confidence", 0.62), default=0.62, min_v=0.0, max_v=1.0),
            _fg2,
            _safe_float(request.args.get("rsi14", 50), default=50.0, min_v=0.0, max_v=100.0),
            _safe_float(request.args.get("technical_score", 0), default=0.0, min_v=-10.0, max_v=10.0),
            math.sin(2 * math.pi * _h2 / 24),              # hour_sin
            math.cos(2 * math.pi * _h2 / 24),              # hour_cos
            float(_BIAS_MAP.get(tech_bias.strip(), 0)),     # technical_bias_score
            1.0 if _fg2 < 45 else 0.0,                     # signal_fg_fear
            math.sin(2 * math.pi * _dow2 / 7),             # dow_sin
            math.cos(2 * math.pi * _dow2 / 7),             # dow_cos
            float(_session2),                               # session
        ]]

        prob = _XGB_MODEL.predict_proba(features)[0]  # [P(DOWN), P(UP)]
        xgb_dir = "UP" if prob[1] > prob[0] else "DOWN"
        agree = (xgb_dir == claude_dir) or (claude_dir in ("NO_BET", ""))

        return jsonify({
            "xgb_direction": xgb_dir,
            "xgb_prob_up":   round(float(prob[1]), 3),
            "xgb_prob_down": round(float(prob[0]), 3),
            "claude_direction": claude_dir,
            "agree": agree,
        })

    except Exception as e:
        app.logger.error("predict_xgb error: %s", e)
        return jsonify({"xgb_direction": None, "agree": True, "reason": "internal_error"})


# ── BET SIZING ───────────────────────────────────────────────────────────────

@app.route("/bet-sizing", methods=["GET"])
def bet_sizing():
    base_size  = _safe_float(request.args.get("base_size", 0.002),  default=0.002,  min_v=0.0001, max_v=0.1)
    confidence = _safe_float(request.args.get("confidence", 0.75), default=0.75,  min_v=0.0,    max_v=1.0)

    # Parametri aggiuntivi per XGBoost correctness model (opzionali, con default neutri)
    fear_greed = _safe_float(request.args.get("fear_greed_value", 50), default=50.0, min_v=0.0,   max_v=100.0)
    rsi14      = _safe_float(request.args.get("rsi14", 50),            default=50.0, min_v=0.0,   max_v=100.0)
    tech_score = _safe_float(request.args.get("technical_score", 0),   default=0.0,  min_v=-10.0, max_v=10.0)
    hour_utc   = _safe_int(request.args.get("hour_utc", time.gmtime().tm_hour), default=12, min_v=0, max_v=23)
    ema_trend   = request.args.get("ema_trend", "").lower()
    tech_bias   = request.args.get("technical_bias", "").lower()
    sig_tech    = request.args.get("signal_technical", "").lower()
    sig_sent    = request.args.get("signal_sentiment", "").lower()
    sig_fg      = request.args.get("signal_fear_greed", "").lower()
    sig_vol     = request.args.get("signal_volume", "").lower()

    ema_trend_up       = 1 if ("bullish" in ema_trend or "bull" in ema_trend or ema_trend == "up") else 0
    tech_bias_score    = float(_BIAS_MAP.get(tech_bias.strip(), 0))
    sig_tech_buy       = 1 if sig_tech in ("buy", "bullish") else 0
    sig_sent_pos       = 1 if sig_sent in ("positive", "pos", "buy", "bullish") else 0
    sig_fg_fear        = 1.0 if fear_greed < 45 else 0.0
    sig_vol_high       = 1 if "high" in sig_vol else 0

    try:
        supabase_url, supabase_key = _sb_config()

        url = f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?select=correct,pnl_usd&bet_taken=eq.true&correct=not.is.null&order=id.desc&limit=10"
        res = requests.get(url, headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }, timeout=5)

        trades = res.json()
        if not trades or len(trades) < 3:
            return jsonify({"size": base_size, "reason": "insufficient_history", "multiplier": 1.0})

        results = [t.get("correct") for t in trades if t.get("correct") is not None]
        pnls = [float(t.get("pnl_usd") or 0) for t in trades]

        # streak
        streak = 0
        streak_type = None
        for r in results:
            if streak_type is None:
                streak_type = r
                streak = 1
            elif r == streak_type:
                streak += 1
            else:
                break

        recent_pnl = sum(pnls[:5])

        # asimmetria win/loss
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0
        profit_factor = round(avg_win / avg_loss, 3) if avg_loss > 0 else 1.0

        # logica moltiplicatore
        multiplier = 1.0
        reason = "base"

        if recent_pnl < -0.15:
            multiplier = 0.25
            reason = "drawdown_protection"
        elif streak_type == False and streak >= 2:
            multiplier = 0.5
            reason = f"loss_streak_{streak}"
        elif streak_type == True and streak >= 3:
            if confidence >= 0.75:
                multiplier = 1.5
                reason = f"win_streak_{streak}_high_conf"
            else:
                multiplier = 1.2
                reason = f"win_streak_{streak}_low_conf"

        # asymmetry penalty: perdite medie >1.5× i guadagni medi
        if profit_factor < 0.67 and reason == "base":
            multiplier *= 0.75
            reason = "asymmetry_penalty"

        # confidence scaling: 0.75→1.00x | 0.85→1.20x (pivot = nuova soglia 0.75)
        conf_mult = 1.0 + (confidence - 0.75) * (0.2 / 0.10)
        conf_mult = round(max(0.8, min(1.2, conf_mult)), 2)

        final_size = round(base_size * multiplier * conf_mult, 6)
        final_size = max(0.001, min(0.005, final_size))

        # P1.1 — XGBoost correctness penalty
        corr_prob = None
        corr_multiplier = 1.0
        if _xgb_correctness is not None:
            try:
                import math as _math3
                from datetime import datetime as _dt_xgb3
                _dow3 = _dt_xgb3.utcnow().weekday()  # 0=Mon..6=Sun
                _session3 = 0 if hour_utc < 8 else (1 if hour_utc < 14 else 2)
                feat_row = [[
                    confidence, fear_greed,
                    rsi14, tech_score,
                    _math3.sin(2 * _math3.pi * hour_utc / 24),  # hour_sin
                    _math3.cos(2 * _math3.pi * hour_utc / 24),  # hour_cos
                    tech_bias_score,                             # technical_bias_score
                    sig_fg_fear,                                 # signal_fg_fear
                    _math3.sin(2 * _math3.pi * _dow3 / 7),      # dow_sin
                    _math3.cos(2 * _math3.pi * _dow3 / 7),      # dow_cos
                    float(_session3),                            # session
                ]]
                corr_prob = float(_xgb_correctness.predict_proba(feat_row)[0][1])  # P(CORRECT)
                # Se P(CORRECT) < 0.45: size -20%, se > 0.55: size +10%, altrimenti invariata
                if corr_prob < 0.45:
                    corr_multiplier = 0.80
                elif corr_prob > 0.55:
                    corr_multiplier = 1.10
                else:
                    corr_multiplier = 1.0
            except Exception:
                corr_multiplier = 1.0

        final_size = round(final_size * corr_multiplier, 4)  # Kraken PF_XBTUSD: max 4 decimali
        final_size = max(0.001, min(0.005, final_size))

        # P1.2 — Confidence calibration
        calibrated_wr = get_calibrated_wr(confidence)

        return jsonify({
            "size": final_size,
            "multiplier": multiplier,
            "conf_multiplier": conf_mult,
            "reason": reason,
            "streak": streak,
            "streak_type": "win" if streak_type else "loss",
            "recent_pnl_5": round(recent_pnl, 6),
            "confidence_used": confidence,
            "profit_factor": profit_factor,
            "xgb_correctness_prob": round(corr_prob, 4) if corr_prob is not None else None,
            "xgb_multiplier": corr_multiplier,
            "calibrated_wr_estimate": calibrated_wr,
            "calibration_note": "historical WR for this confidence bucket",
        })

    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"size": base_size, "reason": "error", "error": "internal_error"})

# ── N8N STATUS (proxy) ───────────────────────────────────────────────────────

# ID fissi dei workflow BTC — evita paginazione su 100+ workflow nell'account
@app.route("/rescue-orphaned", methods=["POST"])
def rescue_orphaned():
    """
    Controlla bet orfane (bet_taken=true, correct=null) e ri-triggera wf02 per ognuna.
    Chiamare periodicamente da launchd ogni 5 minuti.
    """
    err = _check_api_key()
    if err:
        return err
    _rl_key = f"rescue:{request.headers.get('X-Api-Key', request.remote_addr)}"
    if not _check_rate_limit(_rl_key, max_calls=20):
        return jsonify({"error": "rate_limited"}), 429
    n8n_key = os.environ.get("N8N_API_KEY", "")
    n8n_url = os.environ.get("N8N_URL", "https://n8n.srv1432354.hstgr.cloud")
    supabase_url, supabase_key = _sb_config()

    if not n8n_key:
        return jsonify({"status": "error", "error": "N8N_API_KEY not configured"}), 503

    # 1. Cerca bet orfane in Supabase
    try:
        r = requests.get(
            f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,direction,created_at,entry_fill_price"
            "&bet_taken=eq.true&correct=is.null&entry_fill_price=not.is.null&order=created_at.asc",
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
            },
            timeout=6,
        )
        orphaned = r.json() if r.ok else []
    except Exception as e:
        return jsonify({"status": "error", "error": f"Supabase: {e}"}), 503

    if not orphaned:
        return jsonify({"status": "ok", "rescued": 0, "message": "No orphaned bets"})

    # 2. Per ogni bet orfana, controlla se wf02 è già attivo per quella bet
    #    (esecuzioni in waiting nelle ultime 40 minuti)
    WF02_ID = os.environ.get("WF02_ID", "NnjfpzgdIyleMVBO")
    rescued = []
    skipped = []

    try:
        active_r = requests.get(
            f"{n8n_url}/api/v1/executions?workflowId={WF02_ID}&status=waiting&limit=20",
            headers={"X-N8N-API-KEY": n8n_key},
            timeout=5,
        )
        active_execs = active_r.json().get("data", []) if active_r.ok else []
        # IDs delle bet già monitorate (da workflowData.pinData se disponibile)
        active_ids = set()
        for ex in active_execs:
            # startedAt entro 40 minuti
            started = ex.get("startedAt", "")
            if started:
                try:
                    age_min = (_dt.datetime.utcnow() -
                               _dt.datetime.fromisoformat(started.replace("Z",""))).total_seconds() / 60
                    if age_min < 40:
                        active_ids.add(ex.get("id"))  # execution id, non bet id
                except Exception:
                    pass
    except Exception:
        active_execs = []
        active_ids = set()

    # 3. Triggera wf02 via rescue webhook per bet orfane
    #    (wf02 ha ora un Webhook Rescue Trigger su /webhook/rescue-wf02)
    #    Per bet stale (>MAX_BET_DURATION_HOURS), risolve direttamente senza wf02.
    max_concurrent = 5
    triggered_count = 0
    RESCUE_WEBHOOK_URL = f"{n8n_url}/webhook/rescue-wf02"
    MAX_BET_HOURS = float(os.environ.get("MAX_BET_DURATION_HOURS", "4"))
    for bet in orphaned:
        bet_id = bet.get("id")

        # ── Stale bet path: risoluzione diretta senza wf02 ──────────────────
        bet_created = bet.get("created_at", "")
        try:
            created_dt = _dt.datetime.fromisoformat(
                bet_created.replace("Z", "").split("+")[0]
            )
            age_hours = (_dt.datetime.utcnow() - created_dt).total_seconds() / 3600
        except Exception:
            age_hours = 0

        if age_hours >= MAX_BET_HOURS:
            try:
                # Chiudi posizione Kraken se ancora aperta
                pos = get_open_position(DEFAULT_SYMBOL)
                if pos:
                    trade = get_trade_client()
                    close_side = "sell" if pos["side"] == "long" else "buy"
                    trade.create_order(
                        orderType="mkt",
                        symbol=DEFAULT_SYMBOL,
                        side=close_side,
                        size=pos["size"],
                        reduceOnly=True,
                    )
                # Prezzo attuale da Binance
                pr = requests.get(
                    "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                    timeout=4,
                )
                exit_price = float(pr.json()["price"]) if pr.ok else float(bet.get("entry_fill_price") or 0)
                entry = float(bet.get("entry_fill_price") or 0)
                direction = bet.get("direction", "UP")
                gross_delta = exit_price - entry
                if direction == "DOWN":
                    gross_delta = -gross_delta
                correct = gross_delta > 0
                pnl = round((exit_price - entry) / entry * 100, 4) if entry else 0
                # Aggiorna Supabase — &correct=is.null previene doppia risoluzione (S-17)
                upd = requests.patch(
                    f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}&correct=is.null",
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal",
                    },
                    json={"exit_fill_price": exit_price, "correct": correct, "pnl_pct": pnl},
                    timeout=6,
                )
                if upd.ok:
                    app.logger.warning(
                        f"[rescue_orphaned] STALE bet #{bet_id} auto-resolved "
                        f"age={age_hours:.1f}h exit={exit_price} correct={correct}"
                    )
                    rescued.append(bet_id)
                    continue
            except Exception as e:
                app.logger.error(f"[rescue_orphaned] stale resolve error bet#{bet_id}: {e}")
            skipped.append(bet_id)
            continue

        # ── Normal path: trigger wf02 webhook ───────────────────────────────
        if len(active_execs) + triggered_count >= max_concurrent:
            skipped.append(bet_id)
            continue
        try:
            trig_r = requests.post(
                RESCUE_WEBHOOK_URL,
                json={"id": bet_id},
                timeout=6,
            )
            if trig_r.status_code < 400:
                rescued.append(bet_id)
                triggered_count += 1
            else:
                skipped.append(bet_id)
        except Exception:
            skipped.append(bet_id)

    app.logger.info(f"[rescue_orphaned] orphaned={len(orphaned)} rescued={rescued} skipped={skipped}")
    return jsonify({
        "status": "ok",
        "orphaned": len(orphaned),
        "rescued": rescued,
        "skipped": skipped,
        "active_wf02_execs": len(active_execs),
    })


@app.route("/ghost-evaluate", methods=["POST"])
def ghost_evaluate():
    """
    Valuta l'outcome fantasma dei segnali SKIP/ALERT per training data XGBoost.
    Per ogni segnale non ancora valutato (ghost_evaluated_at IS NULL), creato almeno
    30 minuti fa: fetch il prezzo Binance a T+30min e valuta se direction era corretta.
    Funziona autonomamente — non dipende da posizioni aperte.
    """
    err = _check_api_key()
    if err:
        return err

    supabase_url, supabase_key = _sb_config()
    if not supabase_url or not supabase_key:
        return jsonify({"status": "error", "error": "Supabase not configured"}), 503

    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff_recent = (now - _dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_old = (now - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        resp = requests.get(
            f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,direction,signal_price,created_at"
            "&bet_taken=eq.false"
            "&ghost_evaluated_at=is.null"
            "&signal_price=not.is.null"
            f"&created_at=lte.{cutoff_recent}"
            f"&created_at=gte.{cutoff_old}"
            "&order=created_at.asc"
            "&limit=50",
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
            },
            timeout=8,
        )
        candidates = resp.json() if resp.ok else []
        if not resp.ok:
            app.logger.warning(
                f"[ghost_evaluate] Supabase HTTP {resp.status_code}: {resp.text[:200]}"
            )
    except Exception:
        app.logger.exception("[ghost_evaluate] Supabase fetch failed")
        return jsonify({"status": "error", "error": "supabase_fetch_failed"}), 503

    if not candidates:
        return jsonify({
            "status": "ok",
            "evaluated": 0,
            "message": "No pending ghost signals",
        })

    evaluated = []
    errors = []
    ghost_ts = now.isoformat()
    batch_limit = min(len(candidates), 10)  # process max 10 per call to avoid rate limits

    for idx, row in enumerate(candidates[:batch_limit]):
        row_id = row.get("id")
        direction = (row.get("direction") or "").upper()
        signal_price = row.get("signal_price")
        created_at = row.get("created_at")
        if not row_id or direction not in ("UP", "DOWN") or signal_price is None:
            continue
        try:
            sp = float(signal_price)
        except (TypeError, ValueError):
            continue

        if idx > 0:
            time.sleep(0.5)  # rate limit protection

        exit_price = _fetch_ghost_exit_price(created_at)
        if exit_price is None:
            errors.append({"id": row_id, "error": "binance_price_unavailable"})
            continue

        ghost_correct = (exit_price > sp) if direction == "UP" else (exit_price < sp)
        pnl_pct = ((exit_price - sp) / sp) if direction == "UP" else ((sp - exit_price) / sp)

        try:
            upd = requests.patch(
                f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{row_id}",
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={
                    "ghost_exit_price": exit_price,
                    "ghost_correct": ghost_correct,
                    "ghost_evaluated_at": ghost_ts,
                    "correct": ghost_correct,
                    "btc_price_exit": exit_price,
                    "pnl_pct": round(pnl_pct if ghost_correct else -abs(pnl_pct), 6),
                    "actual_direction": direction if ghost_correct else ("DOWN" if direction == "UP" else "UP"),
                },
                timeout=5,
            )
            if upd.ok:
                evaluated.append({"id": row_id, "ghost_correct": ghost_correct, "exit_price": exit_price})
            else:
                errors.append({"id": row_id, "error": upd.text[:100]})
        except Exception:
            app.logger.exception(f"Ghost evaluate error row {row_id}")
            errors.append({"id": row_id, "error": "evaluate_error"})

    app.logger.info(
        f"[ghost_evaluate] evaluated={len(evaluated)} errors={len(errors)}"
    )
    return jsonify({
        "status": "ok",
        "evaluated": len(evaluated),
        "errors": len(errors),
        "remaining": len(candidates) - batch_limit,
        "results": evaluated,
        "error_details": errors[:5],
    })


def _fetch_ghost_exit_price(created_at_str):
    """
    Fetch close price at T+30min from signal creation time.
    Tries Binance 1m klines first, falls back to CryptoCompare histominute.
    Returns float price or None if unavailable.
    """
    try:
        ts = _dt.datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
        target = ts + _dt.timedelta(minutes=30)
        target_ms = int(target.timestamp() * 1000)
        target_unix = int(target.timestamp())
    except Exception as e:
        app.logger.warning(f"[ghost] parse error: {e}")
        return None

    # Try Binance first
    try:
        r = requests.get(
            f"https://api.binance.com/api/v3/klines"
            f"?symbol=BTCUSDT&interval=1m&startTime={target_ms}&limit=1",
            timeout=8,
        )
        if r.ok:
            klines = r.json()
            if klines and len(klines) > 0:
                return float(klines[0][4])
            app.logger.warning(f"[ghost] Binance klines empty ts={target_ms}")
        else:
            app.logger.warning(f"[ghost] Binance HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        app.logger.warning(f"[ghost] Binance exception: {e}")

    # Fallback: CryptoCompare histominute (no geo-restriction, precise timestamp)
    try:
        r2 = requests.get(
            f"https://min-api.cryptocompare.com/data/v2/histominute"
            f"?fsym=BTC&tsym=USD&limit=1&toTs={target_unix}",
            timeout=8,
        )
        if r2.ok:
            data = r2.json()
            if data.get("Response") == "Success":
                candles = data.get("Data", {}).get("Data", [])
                if candles:
                    return float(candles[-1]["close"])
            app.logger.warning(f"[ghost] CryptoCompare empty ts={target_unix}")
        else:
            app.logger.warning(f"[ghost] CryptoCompare HTTP {r2.status_code}")
    except Exception as e:
        app.logger.warning(f"[ghost] CryptoCompare exception: {e}")

    return None


@app.route("/admin/backfill-signal-price", methods=["POST"])
def admin_backfill_signal_price():
    """One-time: backfill signal_price=btc_price_entry for SKIP records where signal_price IS NULL."""
    err = _check_api_key()
    if err:
        return err
    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return jsonify({"error": "no supabase config"}), 500
    headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}", "Content-Type": "application/json", "Prefer": "return=minimal"}
    r = requests.get(
        f"{sb_url}/rest/v1/{SUPABASE_TABLE}?classification=eq.SKIP&signal_price=is.null&btc_price_entry=not.is.null&select=id,btc_price_entry&order=id.asc",
        headers=headers, timeout=10
    )
    if not r.ok:
        return jsonify({"error": r.text[:200]}), 500
    rows = r.json()
    ok, err_list = 0, []
    for row in rows:
        upd = requests.patch(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{row['id']}",
            json={"signal_price": row["btc_price_entry"]},
            headers=headers, timeout=5
        )
        if upd.status_code in (200, 204):
            ok += 1
        else:
            err_list.append({"id": row["id"], "status": upd.status_code})
    return jsonify({"patched": ok, "errors": err_list, "total": len(rows)})


@app.route("/reload-calibration", methods=["POST"])
def reload_calibration():
    """
    Aggiorna CONF_CALIBRATION e DEAD_HOURS_UTC da dati Supabase live.
    Chiamato da launchd dopo ogni retrain XGBoost (POST su Railway URL).
    """
    err = _check_api_key()
    if err:
        return err
    cal_result  = refresh_calibration()
    dead_result = refresh_dead_hours()
    return jsonify({
        "calibration":      cal_result,
        "dead_hours":       dead_result,
        "conf_calibration": {f"{k[0]:.2f}-{k[1]:.2f}": v for k, v in CONF_CALIBRATION.items()},
        "dead_hours_utc":   sorted(DEAD_HOURS_UTC),
    })


_force_retrain_last: float = 0.0

@app.route("/force-retrain", methods=["POST"])
def force_retrain():
    """
    Public endpoint — anyone can trigger calibration refresh when bets >= 30.
    Rate limited: 1 request per hour. Does NOT run XGBoost training.
    Only refreshes in-memory confidence thresholds + dead hours from live Supabase data.
    """
    global _force_retrain_last
    import time as _time
    now = _time.time()
    cooldown = 3600  # 1 hour
    if now - _force_retrain_last < cooldown:
        remaining = int(cooldown - (now - _force_retrain_last))
        return jsonify({
            "status": "cooldown",
            "message": f"Rate limited. Try again in {remaining // 60}m {remaining % 60}s.",
            "next_allowed_in_seconds": remaining,
        }), 429
    _force_retrain_last = now
    cal_result  = refresh_calibration()
    dead_result = refresh_dead_hours()
    return jsonify({
        "status": "ok",
        "message": "Calibration thresholds refreshed from live data.",
        "dead_hours_utc": sorted(DEAD_HOURS_UTC),
        "note": "Full XGBoost retrain runs automatically every Sunday 03:00 UTC via launchd.",
    })


@app.route("/costs", methods=["GET"])
def costs():
    """
    Breakdown costi reali + stimati delle piattaforme usate dal bot.
    Cache 10 minuti sulla parte n8n executions.
    """
    global _costs_cache
    sb_url, sb_key = _sb_config()
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}

    # ── 1. Kraken fees (reali da Supabase) ───────────────────────────────────
    kraken_fees_total = 0.0
    trade_count = 0
    try:
        url = (f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
               f"?select=fees_total&bet_taken=eq.true&correct=not.is.null&limit=10000")
        res = requests.get(url, headers=sb_headers, timeout=5)
        rows = res.json() if res.ok else []
        fees_list = [float(r["fees_total"]) for r in rows if r.get("fees_total") is not None]
        kraken_fees_total = round(sum(fees_list), 4)
        trade_count = len(rows)
    except Exception:
        pass

    avg_per_trade = round(kraken_fees_total / trade_count, 6) if trade_count > 0 else 0.0

    # ── 1b. Entry slippage stats (da Supabase) ───────────────────────────────
    slip_total = 0.0
    slip_avg   = 0.0
    slip_count = 0
    try:
        url = (f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
               f"?select=entry_slippage,bet_size"
               f"&bet_taken=eq.true&correct=not.is.null&entry_slippage=not.is.null&limit=10000")
        res = requests.get(url, headers=sb_headers, timeout=5)
        slip_rows = res.json() if res.ok else []
        slip_vals = [float(r["entry_slippage"]) for r in slip_rows if r.get("entry_slippage") is not None]
        slip_count = len(slip_vals)
        slip_total = round(sum(slip_vals), 4) if slip_vals else 0.0
        slip_avg   = round(slip_total / slip_count, 4) if slip_count > 0 else 0.0
    except Exception:
        pass

    # ── 2. Supabase row count (reale) ─────────────────────────────────────────
    row_count = 0
    try:
        url = f"{sb_url}/rest/v1/{SUPABASE_TABLE}?select=id"
        res = requests.get(url, headers={**sb_headers, "Prefer": "count=exact"}, timeout=5)
        cr = res.headers.get("Content-Range", "")
        if "/" in cr:
            row_count = int(cr.split("/")[1])
    except Exception:
        pass

    # ── 3. n8n executions (cached 10min) ─────────────────────────────────────
    now = time.time()
    use_cache = _costs_cache["data"] is not None and (now - _costs_cache["ts"]) < 600
    n8n_exec_est = 0
    cached = False
    if use_cache:
        n8n_exec_est = _costs_cache["data"].get("n8n_exec_est", 0)
        cached = True
    else:
        try:
            n8n_key = os.environ.get("N8N_API_KEY", "")
            n8n_url_base = os.environ.get("N8N_URL", "https://n8n.srv1432354.hstgr.cloud")
            if n8n_key:
                r = requests.get(
                    f"{n8n_url_base}/api/v1/executions?workflowId=OMgFa9Min4qXRnhq&limit=100",
                    headers={"X-N8N-API-KEY": n8n_key},
                    timeout=8,
                )
                if r.ok:
                    count_wf01 = len(r.json().get("data", []))
                    n8n_exec_est = count_wf01 * 6  # stima ×6 workflow attivi
        except Exception:
            pass
        _costs_cache["data"] = {"n8n_exec_est": n8n_exec_est}
        _costs_cache["ts"] = now

    _n8n_limit_raw = os.environ.get("N8N_EXECUTION_LIMIT", "999999")
    try:
        n8n_limit = int(_n8n_limit_raw)
    except (ValueError, TypeError):
        n8n_limit = 999999  # "infinite" or invalid → treat as unlimited
    n8n_pct = round(n8n_exec_est / n8n_limit * 100, 1) if n8n_limit > 0 else 0.0
    n8n_cost = 0.0  # self-hosted su VPS Hostinger

    # ── 4. Claude API (reale: conta predizioni questo mese da Supabase) ─────────
    import datetime as _dt
    claude_model = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")
    _MODEL_PRICING = {
        "claude-haiku-4-5":           {"input": 0.80,  "output": 4.00},
        "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
        "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
        "claude-sonnet-4-6-20251001": {"input": 3.00,  "output": 15.00},
        "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
    }
    pricing = _MODEL_PRICING.get(claude_model, {"input": 3.00, "output": 15.00})
    avg_in  = int(os.environ.get("CLAUDE_AVG_INPUT_TOKENS",  "4000"))
    avg_out = int(os.environ.get("CLAUDE_AVG_OUTPUT_TOKENS", "800"))

    monthly_calls = 0
    month_start = _dt.date.today().replace(day=1).isoformat()
    try:
        url = (f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
               f"?select=id&created_at=gte.{month_start}T00:00:00")
        res = requests.get(url, headers={**sb_headers, "Prefer": "count=exact"}, timeout=5)
        cr = res.headers.get("Content-Range", "")
        if "/" in cr:
            monthly_calls = int(cr.split("/")[1])
    except Exception:
        monthly_calls = 0

    cost_per_call = (avg_in * pricing["input"] + avg_out * pricing["output"]) / 1_000_000
    claude_api_estimated = round(monthly_calls * cost_per_call, 4)
    # CLAUDE_API_MONTHLY_USD overrides estimate with real Anthropic invoice value
    _claude_api_override = os.environ.get("CLAUDE_API_MONTHLY_USD", "")
    if _claude_api_override:
        claude_api_cost   = float(_claude_api_override)
        claude_api_source = "manual"
    else:
        claude_api_cost   = claude_api_estimated
        claude_api_source = "estimated"

    # ── 5. Claude Code (IDE sessions) ─────────────────────────────────────────
    # Priority: CLAUDE_CODE_MONTHLY_USD (manual) > derived from ANTHROPIC_TOTAL_SPEND_USD
    _anthropic_total = os.environ.get("ANTHROPIC_TOTAL_SPEND_USD", "")
    _claude_code_manual = os.environ.get("CLAUDE_CODE_MONTHLY_USD", "")
    if _claude_code_manual:
        claude_code_cost   = float(_claude_code_manual)
        claude_code_source = "manual"
    elif _anthropic_total:
        # Total Anthropic spend − Claude API bot calls = Claude Code IDE sessions
        claude_code_cost   = round(max(0.0, float(_anthropic_total) - claude_api_cost), 2)
        claude_code_source = "derived"
    else:
        claude_code_cost   = 0.0
        claude_code_source = "set ANTHROPIC_TOTAL_SPEND_USD or CLAUDE_CODE_MONTHLY_USD"

    # ── 6. Railway (Hobby $5/mo, auto-renew monthly) ──────────────────────────
    railway_plan = os.environ.get("RAILWAY_PLAN", "hobby").lower()
    railway_cost = 5.0 if railway_plan == "hobby" else 0.0

    # ── 7. Hostinger VPS (n8n self-hosted) ───────────────────────────────────
    hostinger_vps_eur = float(os.environ.get("HOSTINGER_VPS_MONTHLY_EUR", "4.99"))
    hostinger_vps_usd = round(hostinger_vps_eur * 1.08, 2)  # EUR→USD approx

    # ── 8. Dominio btcpredictor.io (.io renewal incl. taxes) ─────────────────
    # Renewal 2027-02-25: €63.99 + €14.08 taxes = €78.07/yr
    domain_yearly_eur = float(os.environ.get("DOMAIN_YEARLY_EUR", "78.07"))
    domain_monthly_usd = round(domain_yearly_eur / 12 * 1.08, 2)

    # ── 9. Hostinger Business Email (signal@btcpredictor.io) ─────────────────
    # Trial expiry 2026-03-27: €5.40 + €1.19 taxes = €6.59/yr
    email_yearly_eur = float(os.environ.get("HOSTINGER_EMAIL_YEARLY_EUR", "6.59"))
    email_monthly_usd = round(email_yearly_eur / 12 * 1.08, 2)

    polygon_gas_usd = float(os.environ.get("POLYGON_GAS_MONTHLY_USD", "0.0"))

    # slip_total is in price-level points (not USD) — excluded from USD total
    total = round(
        kraken_fees_total + n8n_cost + claude_api_cost + claude_code_cost
        + railway_cost + hostinger_vps_usd + domain_monthly_usd + email_monthly_usd
        + polygon_gas_usd,
        4
    )

    return jsonify({
        "kraken_fees": {
            "total_usd": kraken_fees_total,
            "trade_count": trade_count,
            "avg_per_trade": avg_per_trade,
        },
        "slippage_stats": {
            "total_pts": slip_total,
            "avg_pts":   slip_avg,
            "count":     slip_count,
        },
        "supabase": {
            "row_count": row_count,
            "plan": "free",
            "cost_usd": 0.0,
        },
        "n8n": {
            "plan": "self-hosted (VPS)",
            "executions_est": n8n_exec_est,
            "limit": "unlimited",
            "pct_used": n8n_pct,
            "cost_usd": 0.0,
        },
        "hostinger_vps": {
            "plan": "KVM1",
            "monthly_eur": hostinger_vps_eur,
            "cost_usd": hostinger_vps_usd,
            "note": "n8n self-hosted + email signal@btcpredictor.io",
        },
        "domain": {
            "name": "btcpredictor.io",
            "yearly_eur": domain_yearly_eur,
            "cost_usd": domain_monthly_usd,
            "expires": "2027-02-25",
        },
        "hostinger_email": {
            "plan": "Starter Business Email",
            "yearly_eur": email_yearly_eur,
            "cost_usd": email_monthly_usd,
            "expires": "2026-03-27",
            "note": "signal@btcpredictor.io · trial expiring soon",
        },
        "claude_api": {
            "model": claude_model,
            "monthly_calls": monthly_calls,
            "avg_input_tokens": avg_in,
            "avg_output_tokens": avg_out,
            "cost_usd": claude_api_cost,
            "cost_estimated": claude_api_estimated,
            "source": claude_api_source,
            "pricing": pricing,
        },
        "claude_code": {
            "cost_usd": claude_code_cost,
            "source": claude_code_source,
        },
        "railway": {
            "plan": railway_plan,
            "cost_usd": railway_cost,
        },
        "polygon_gas": {
            "cost_usd": polygon_gas_usd,
            "source": "env:POLYGON_GAS_MONTHLY_USD",
        },
        "total_usd": total,
        "cached": cached,
    })


@app.route("/equity-history", methods=["GET"])
def equity_history():
    err = _check_read_key()
    if err:
        return err
    sb_url, sb_key = _sb_config()
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    capital_base = float(os.environ.get("CAPITAL_USD") or os.environ.get("CAPITAL", "100"))

    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,created_at,pnl_usd"
            "&bet_taken=eq.true&correct=not.is.null&pnl_usd=not.is.null"
            "&order=id.asc",
            headers=sb_headers,
            timeout=6,
        )
        rows = r.json() if r.ok else []
    except Exception as e:
        return jsonify({"error": f"Supabase: {e}"}), 500

    history = []
    cumulative_pnl = 0.0
    for row in rows:
        pnl = float(row.get("pnl_usd") or 0)
        cumulative_pnl += pnl
        equity = round(capital_base + cumulative_pnl, 6)
        history.append({
            "id": row.get("id"),
            "created_at": row.get("created_at"),
            "pnl_usd": pnl,
            "equity": equity,
        })

    final_equity = round(capital_base + cumulative_pnl, 6) if history else capital_base
    return jsonify({
        "capital_base": capital_base,
        "history": history,
        "count": len(history),
        "final_equity": final_equity,
    })


@app.route("/risk-metrics", methods=["GET"])
def risk_metrics():
    err = _check_read_key()
    if err:
        return err
    sb_url, sb_key = _sb_config()
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}

    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,correct,pnl_usd,direction,created_at"
            "&bet_taken=eq.true&correct=not.is.null"
            "&order=id.asc",
            headers=sb_headers,
            timeout=6,
        )
        rows = r.json() if r.ok else []
    except Exception as e:
        return jsonify({"error": f"Supabase: {e}"}), 500

    total_trades = len(rows)
    wins = [r for r in rows if r.get("correct") is True]
    losses = [r for r in rows if r.get("correct") is False]
    win_rate = round(len(wins) / total_trades * 100, 1) if total_trades > 0 else 0.0

    pnl_wins = [float(r.get("pnl_usd") or 0) for r in wins]
    pnl_losses = [float(r.get("pnl_usd") or 0) for r in losses]
    total_pnl = round(sum(pnl_wins) + sum(pnl_losses), 6)
    avg_win = round(sum(pnl_wins) / len(pnl_wins), 6) if pnl_wins else 0.0
    avg_loss = round(sum(pnl_losses) / len(pnl_losses), 6) if pnl_losses else 0.0

    sum_wins = sum(pnl_wins)
    sum_losses = sum(pnl_losses)
    profit_factor = round(abs(sum_wins) / abs(sum_losses), 3) if sum_losses != 0 else None

    current_streak = {"result": None, "count": 0}
    if rows:
        last_result = rows[-1].get("correct")
        streak_label = "WIN" if last_result else "LOSS"
        count = 0
        for row in reversed(rows):
            if row.get("correct") == last_result:
                count += 1
            else:
                break
        current_streak = {"result": streak_label, "count": count}

    capital_base = float(os.environ.get("CAPITAL_USD") or os.environ.get("CAPITAL", "100"))
    max_drawdown_usd = 0.0
    if rows:
        peak = capital_base
        equity = capital_base
        for row in rows:
            equity += float(row.get("pnl_usd") or 0)
            if equity > peak:
                peak = equity
            dd = equity - peak
            if dd < max_drawdown_usd:
                max_drawdown_usd = dd
    max_drawdown_usd = round(max_drawdown_usd, 6)

    last_10 = rows[-10:] if len(rows) >= 10 else rows
    last_10_wins = sum(1 for r in last_10 if r.get("correct") is True)
    last_10_wr = int(last_10_wins / len(last_10) * 100) if last_10 else 0
    last_10_pnl = round(sum(float(r.get("pnl_usd") or 0) for r in last_10), 6)

    return jsonify({
        "total_trades": total_trades,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "current_streak": current_streak,
        "max_drawdown_usd": max_drawdown_usd,
        "last_10_wr": last_10_wr,
        "last_10_pnl": last_10_pnl,
    })


@app.route("/wf-status", methods=["GET"])
def wf_status():
    err = _check_api_key()
    if err:
        return err
    sb_url, sb_key = _sb_config()
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    n8n_key = os.environ.get("N8N_API_KEY", "")
    n8n_url_base = os.environ.get("N8N_URL", "https://n8n.srv1432354.hstgr.cloud")

    wf02_active = False
    wf02_last_execution = None
    if n8n_key:
        try:
            r = requests.get(
                f"{n8n_url_base}/api/v1/executions?workflowId=NnjfpzgdIyleMVBO&limit=5",
                headers={"X-N8N-API-KEY": n8n_key},
                timeout=8,
            )
            if r.ok:
                executions = r.json().get("data", [])
                if executions:
                    last = executions[0]
                    wf02_last_execution = {
                        "id": str(last.get("id", "")),
                        "status": last.get("status"),
                        "started_at": last.get("startedAt"),
                    }
                    wf02_active = any(
                        e.get("status") in ("running", "waiting") for e in executions
                    )
        except Exception:
            pass

    open_bets_supabase = 0
    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id&bet_taken=eq.true&correct=is.null",
            headers={**sb_headers, "Prefer": "count=exact"},
            timeout=5,
        )
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            open_bets_supabase = int(cr.split("/")[1])
    except Exception:
        pass

    alert = None
    if open_bets_supabase > 0 and not wf02_active:
        alert = "bet open but wf02 not monitoring"

    return jsonify({
        "wf02_active": wf02_active,
        "wf02_last_execution": wf02_last_execution,
        "open_bets_supabase": open_bets_supabase,
        "alert": alert,
    })


@app.route("/check-status", methods=["GET"])
def check_status():
    """
    Public health check for the dashboard alert banner.
    Returns: alert message (or null), open bet count, wf02 active flag.
    No auth required — only returns high-level system status.
    """
    sb_url, sb_key = _sb_config()
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    n8n_key = os.environ.get("N8N_API_KEY", "")
    n8n_url_base = os.environ.get("N8N_URL", "https://n8n.srv1432354.hstgr.cloud")

    from datetime import datetime, timezone
    # wf02 ID: NnjfpzgdIyleMVBO (02_BTC_Trade_Checker — VPS Hostinger)
    # Active = currently running/waiting OR had a successful execution in the last 25 min
    # (wf08 triggers wf02 every 10 min — 25 min gives 2.5x buffer)
    wf02_active = False
    if n8n_key:
        try:
            r = requests.get(
                f"{n8n_url_base}/api/v1/executions?workflowId=NnjfpzgdIyleMVBO&limit=5",
                headers={"X-N8N-API-KEY": n8n_key},
                timeout=6,
            )
            if r.ok:
                executions = r.json().get("data", [])
                now_utc = datetime.now(timezone.utc)
                for e in executions:
                    if e.get("status") in ("running", "waiting"):
                        wf02_active = True
                        break
                    ts = e.get("stoppedAt") or e.get("finishedAt") or e.get("startedAt")
                    if ts:
                        try:
                            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if (now_utc - t).total_seconds() < 1500:  # 25 min
                                wf02_active = True
                                break
                        except Exception:
                            pass
        except Exception:
            pass

    open_bets_supabase = 0
    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id&bet_taken=eq.true&correct=is.null",
            headers={**sb_headers, "Prefer": "count=exact"},
            timeout=5,
        )
        cr = r.headers.get("Content-Range", "")
        if "/" in cr:
            open_bets_supabase = int(cr.split("/")[1])
    except Exception:
        pass

    alert = None
    if open_bets_supabase > 0 and not wf02_active:
        alert = f"{open_bets_supabase} bet open but wf02 not monitoring"

    return jsonify({
        "wf02_active": wf02_active,
        "open_bets_supabase": open_bets_supabase,
        "alert": alert,
    })


@app.route("/orphaned-bets", methods=["GET"])
def orphaned_bets():
    err = _check_api_key()
    if err:
        return err
    sb_url, sb_key = _sb_config()
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}

    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,created_at,direction,btc_price_entry,bet_size"
            "&bet_taken=eq.true&correct=is.null&entry_fill_price=not.is.null&order=id.desc&limit=20",
            headers=sb_headers,
            timeout=6,
        )
        rows = r.json() if r.ok else []
    except Exception as e:
        return jsonify({"error": f"Supabase: {e}"}), 500

    now = _dt.datetime.utcnow()
    result = []
    for row in rows:
        minutes_open = 0
        try:
            created = _dt.datetime.fromisoformat(row["created_at"].replace("Z", ""))
            minutes_open = int((now - created).total_seconds() / 60)
        except Exception:
            pass
        result.append({
            "id": row.get("id"),
            "created_at": row.get("created_at"),
            "direction": row.get("direction"),
            "btc_price_entry": row.get("btc_price_entry"),
            "bet_size": row.get("bet_size"),
            "minutes_open": minutes_open,
        })

    return jsonify({"orphaned": result, "count": len(result)})


@app.route("/backfill-bet/<int:bet_id>", methods=["POST"])
def backfill_bet(bet_id):
    err = _check_api_key()
    if err:
        return err
    sb_url, sb_key = _sb_config()
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}

    body = request.get_json(silent=True) or {}
    exit_price = body.get("exit_price")
    if exit_price is None:
        return jsonify({"error": "exit_price is required"}), 400

    try:
        exit_price = float(exit_price)
    except (TypeError, ValueError):
        return jsonify({"error": "exit_price must be a number"}), 400

    # 1. Fetch bet from Supabase
    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            f"?id=eq.{bet_id}&select=id,direction,btc_price_entry,bet_size,correct",
            headers=sb_headers,
            timeout=6,
        )
        rows = r.json() if r.ok else []
    except Exception as e:
        return jsonify({"error": f"Supabase: {e}"}), 500

    if not rows:
        return jsonify({"error": "bet not found"}), 404

    bet = rows[0]

    if bet.get("correct") is not None:
        return jsonify({"error": "bet already closed"}), 400

    entry_price = float(bet["btc_price_entry"])
    bet_size = float(bet["bet_size"])
    direction = bet["direction"]

    # 2. Calculate fields
    actual_direction = "UP" if exit_price > entry_price else "DOWN"

    if direction == "UP":
        pnl_gross = (exit_price - entry_price) * bet_size
    else:
        pnl_gross = (entry_price - exit_price) * bet_size

    fee_est = bet_size * (entry_price + exit_price) * TAKER_FEE  # entry + exit taker fee
    pnl_usd = pnl_gross - fee_est
    pnl_pct = round(pnl_gross / (entry_price * bet_size) * 100, 4) if entry_price * bet_size != 0 else 0.0

    correct = body.get("correct")
    if correct is None:
        correct = direction == actual_direction
    else:
        correct = bool(correct)

    # 3. PATCH Supabase
    patch_data = {
        "btc_price_exit": exit_price,
        "actual_direction": actual_direction,
        "pnl_usd": round(pnl_usd, 4),
        "pnl_pct": pnl_pct,
        "fees_total": round(fee_est, 6),
        "correct": correct,
        "close_reason": "manual_backfill",
    }
    try:
        pr = requests.patch(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}",
            headers={**sb_headers, "Content-Type": "application/json", "Prefer": "return=minimal"},
            json=patch_data,
            timeout=6,
        )
        if not pr.ok:
            return jsonify({"error": f"Supabase PATCH failed: {pr.status_code} {pr.text}"}), 500
    except Exception as e:
        return jsonify({"error": f"Supabase: {e}"}), 500

    return jsonify({
        "ok": True,
        "bet_id": bet_id,
        "pnl_usd": round(pnl_usd, 4),
        "pnl_pct": pnl_pct,
        "correct": correct,
    })


@app.route("/n8n-status", methods=["GET"])
def n8n_status():
    """
    Proxy verso n8n API — richiede N8N_API_KEY env var su Railway.
    Fetch per ID diretto (non per tag, che vengono azzerati dall'API n8n ad ogni update).
    """
    n8n_key = os.environ.get("N8N_API_KEY", "")
    n8n_url = os.environ.get("N8N_URL", "https://n8n.srv1432354.hstgr.cloud")
    if not n8n_key:
        return jsonify({"status": "error", "error": "N8N_API_KEY not configured on Railway"}), 200

    # IDs VPS Hostinger (migrati 2026-02-26)
    BTC_WORKFLOW_IDS = [
        "Yg0o2MaBZBHYq7Wc",  # 00_Error_Notifier
        "E2LdFbQHKfMTVPOI",  # 01A_BTC_AI_Inputs
        "OMgFa9Min4qXRnhq",  # 01B_BTC_Prediction_Bot
        "NnjfpzgdIyleMVBO",  # 02_BTC_Trade_Checker
        "K4pzVU0SCc7apPKh",  # 03_BTC_Wallet_Checker
        "my8xac5Vs2q3wN4G",  # 04_BTC_Talker
        "3YSec3NytjxfbG08",  # 05_BTC_Prediction_Verifier
        "O1JlHp7tgVFBfrwm",  # 06_BTC_System_Watchdog
        "nzMMmMC6Q9eysUBP",  # 07_BTC_Commander
        "Fjk7M3cOEcL1aAVf",  # 08_BTC_Position_Monitor
        "EQ5AuKbbM9DNWWXw",  # 09A_BTC_Social_Media_Manager
        "l1t7NAtR9BiF80Bi",  # 09B_BTC_Social_Publisher
        "eWGpJa3dsw6XxnC4",  # 10_GA4_Daily_Report
        "mKC0Y4YDjUf3I2dp",  # 11_BTC_Channel_Content
        "SR2gtlT3xnTZVIOx",  # 12_Email_Handler
        "Te09gFLnfVhC7ugt",  # 10_Sentry_Alert_Handler
        "wT8XdaLs0HHlXZjX",  # 10_BTC_Compliance_Reminder
    ]

    headers = {"X-N8N-API-KEY": n8n_key}

    def _fetch_workflow(wf_id):
        try:
            wf_r = requests.get(
                f"{n8n_url}/api/v1/workflows/{wf_id}",
                headers=headers, timeout=5
            )
            if not wf_r.ok:
                return None
            wf = wf_r.json()
            wf_data = {
                "id":     wf_id,
                "name":   wf.get("name", wf_id),
                "active": wf.get("active", False),
            }
            # Ultime 5 executions per stats e sparkline
            ex_r = requests.get(
                f"{n8n_url}/api/v1/executions?workflowId={wf_id}&limit=5",
                headers=headers, timeout=4
            )
            if ex_r.ok:
                executions = ex_r.json().get("data", [])
                if executions:
                    last = executions[0]
                    wf_data["last_execution"] = {
                        "id":         last.get("id"),
                        "status":     last.get("status"),
                        "started_at": last.get("startedAt"),
                        "stopped_at": last.get("stoppedAt"),
                    }
                    history = [ex.get("status", "unknown") for ex in executions]
                    successes = sum(1 for s in history if s == "success")
                    wf_data["exec_history"]  = history
                    wf_data["success_rate"]  = round(successes / len(history) * 100) if history else None
            return wf_data
        except Exception:
            return None

    try:
        # Parallel fetch — all 12 workflows in ~5s instead of ~60s sequential
        result = []
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = {pool.submit(_fetch_workflow, wf_id): wf_id for wf_id in BTC_WORKFLOW_IDS}
            for future in as_completed(futures, timeout=10):
                data = future.result()
                if data:
                    result.append(data)
        # Sort back to original order
        id_order = {wf_id: i for i, wf_id in enumerate(BTC_WORKFLOW_IDS)}
        result.sort(key=lambda w: id_order.get(w["id"], 99))
        return jsonify({"status": "ok", "workflows": result, "ts": int(time.time())})

    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"status": "error", "error": "internal_error"[:120]}), 200




# ── ERROR PATTERNS ───────────────────────────────────────────────────────────

@app.route("/error-patterns", methods=["GET"])
def error_patterns():
    """Return last error pattern analysis (generated by analyze_errors.py)."""
    ep_path = os.path.join(os.path.dirname(__file__), "datasets", "error_patterns.json")
    if not os.path.exists(ep_path):
        return jsonify({"error": "No error_patterns.json found. Run analyze_errors.py first."}), 404
    try:
        with open(ep_path, "r") as f:
            data = json.load(f)
        return jsonify(data)
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500


# ── BACKTEST REPORT ──────────────────────────────────────────────────────────

@app.route("/backtest-report", methods=["GET"])
def backtest_report():
    """Return last walk-forward backtest report and XGBoost training report."""
    import os as _os
    base = _os.path.dirname(__file__)
    report_path = _os.path.join(base, "datasets", "backtest_report.txt")
    xgb_path = _os.path.join(base, "datasets", "xgb_report.txt")
    if not _os.path.exists(report_path):
        return jsonify({"error": "No backtest report found. Run backtest.py first."}), 404
    try:
        with open(report_path, "r") as f:
            content = f.read()
        lines = content.strip().split("\n")
        xgb_content = None
        if _os.path.exists(xgb_path):
            with open(xgb_path, "r") as f:
                xgb_content = f.read()
        return jsonify({
            "report": content,
            "backtest_report": content,
            "xgb_report": xgb_content,
            "lines": len(lines),
            "ok": True,
        })
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500


@app.route("/backtest-data", methods=["GET"])
def backtest_data():
    """Return structured backtest JSON for dashboard charts."""
    import os as _os
    data_path = _os.path.join(_os.path.dirname(__file__), "datasets", "backtest_data.json")
    if not _os.path.exists(data_path):
        return jsonify({"error": "No backtest data found. Run backtest.py first."}), 404
    try:
        with open(data_path, "r") as f:
            content = json.load(f)
        return jsonify(content)
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500


# ── PUBLIC CONTRIBUTIONS ──────────────────────────────────────────────────────

_CONTRIBUTION_ROLES = {
    "trader":    "Trader",
    "developer": "Developer",
    "crypto":    "Crypto Expert",
    "visionary": "Visionario",
    "friend":    "Amico / Parente",
    "other":     "Altro",
}
_CONTRIBUTION_MAX_CHARS = 500
_CONTRIBUTION_RATE = {}   # ip → last_submit timestamp (in-memory, ephemeral)
_CONTRIBUTION_COOLDOWN = 300  # 5 min between submissions per IP

# ── reCAPTCHA v3 ────────────────────────────────────────────────
_RECAPTCHA_SECRET = os.environ.get("RECAPTCHA_SECRET_KEY", "")
_RECAPTCHA_THRESHOLD = 0.5  # score 0.0 (bot) → 1.0 (human)


def _verify_recaptcha(token: str, action: str = "") -> bool:
    """Verify reCAPTCHA v3 token server-side. Returns True if valid."""
    if not _RECAPTCHA_SECRET:
        return True  # fail open if not configured
    if not token:
        return False
    try:
        r = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={"secret": _RECAPTCHA_SECRET, "response": token},
            timeout=5,
        )
        result = r.json()
        if not result.get("success"):
            return False
        if result.get("score", 0) < _RECAPTCHA_THRESHOLD:
            app.logger.warning("recaptcha low score=%.2f action=%s", result.get("score"), action)
            return False
        if action and result.get("action") != action:
            return False
        return True
    except Exception as exc:
        app.logger.error("recaptcha verify error: %s", exc)
        return True  # fail open on network error


@app.route("/submit-contribution", methods=["POST"])
def submit_contribution():
    """
    Public endpoint — zero personal data stored.
    Accepts: role (dropdown), insight (text), consent (bool).
    No name, no email, no IP stored in DB.
    """
    # ── Rate limit by IP (in-memory, not stored) ──
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0].strip()
    now = time.time()
    # Purge stale entries to prevent unbounded growth (memory leak A-08)
    for _k in [k for k, v in list(_CONTRIBUTION_RATE.items()) if now - v >= _CONTRIBUTION_COOLDOWN]:
        _CONTRIBUTION_RATE.pop(_k, None)
    last = _CONTRIBUTION_RATE.get(ip, 0)
    if now - last < _CONTRIBUTION_COOLDOWN:
        remaining = int(_CONTRIBUTION_COOLDOWN - (now - last))
        return jsonify({"ok": False, "error": "rate_limited",
                        "message": f"Aspetta {remaining // 60}m {remaining % 60}s prima di inviare un altro contributo."}), 429
    _CONTRIBUTION_RATE[ip] = now

    data = request.get_json(silent=True) or {}

    # ── reCAPTCHA v3 ──
    if not _verify_recaptcha(data.get("recaptcha_token", ""), "submit_contribution"):
        return jsonify({"ok": False, "error": "Verifica anti-bot fallita. Ricarica la pagina."}), 400

    role    = str(data.get("role", "other"))[:20].strip()
    insight = str(data.get("insight", ""))[:_CONTRIBUTION_MAX_CHARS].strip()
    consent = bool(data.get("consent", False))

    if not insight or len(insight) < 10:
        return jsonify({"ok": False, "error": "insight troppo corto (min 10 caratteri)"}), 400
    if role not in _CONTRIBUTION_ROLES:
        role = "other"
    if not consent:
        return jsonify({"ok": False, "error": "consenso obbligatorio"}), 400

    # ── Save to Supabase (zero personal data) ──
    supabase_url, supabase_key = _sb_config()
    if not supabase_url or not supabase_key:
        return jsonify({"ok": False, "error": "DB non configurato"}), 500

    payload = {"role": role, "insight": insight, "consent_given": True, "approved": False}
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    try:
        r = requests.post(
            f"{supabase_url}/rest/v1/contributions",
            json=payload, headers=headers, timeout=8,
        )
        if r.status_code not in (200, 201):
            return jsonify({"ok": False, "error": "Errore salvataggio"}), 500
        saved = r.json()
        contrib_id = saved[0]["id"] if saved else "?"
    except Exception as e:
        return jsonify({"ok": False, "error": "Errore DB"}), 500

    # ── Build approve/reject URLs (token HMAC, non espone BOT_API_KEY) ──
    base_url     = os.environ.get("RAILWAY_URL", "https://btcpredictor.io")
    approve_url  = f"{base_url}/approve-contribution/{contrib_id}?token={_make_contribution_token(contrib_id, 'approve')}"
    reject_url   = f"{base_url}/reject-contribution/{contrib_id}?token={_make_contribution_token(contrib_id, 'reject')}"
    owner_email  = os.environ.get("OWNER_EMAIL", "")

    # ── Telegram notification (best-effort) ──
    telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_owner = os.environ.get("TELEGRAM_OWNER_ID", "")
    if telegram_token and telegram_owner:
        try:
            msg = (
                f"📥 *Nuovo contributo \\#{contrib_id}*\n\n"
                f"*Ruolo*: {_CONTRIBUTION_ROLES.get(role, role)}\n\n"
                f"*Insight*:\n_{insight[:300]}_\n\n"
                f"[✅ Approva]({approve_url}) · [❌ Rifiuta]({reject_url})"
            )
            requests.post(
                f"https://api.telegram.org/bot{telegram_token}/sendMessage",
                json={"chat_id": telegram_owner, "text": msg, "parse_mode": "MarkdownV2",
                      "disable_web_page_preview": True},
                timeout=5,
            )
        except Exception:
            pass

    # ── n8n webhook → email review (best-effort) ──
    n8n_webhook = os.environ.get("N8N_CONTRIBUTION_WEBHOOK",
                                 "https://n8n.srv1432354.hstgr.cloud/webhook/contribution-review")
    try:
        requests.post(
            n8n_webhook,
            json={
                "id":          contrib_id,
                "role":        _CONTRIBUTION_ROLES.get(role, role),
                "insight":     insight,
                "approve_url": approve_url,
                "reject_url":  reject_url,
                "owner_email": owner_email,
            },
            timeout=6,
        )
    except Exception:
        pass  # email is best-effort; Telegram already notified

    return jsonify({"ok": True, "message": "Contributo ricevuto — verrà pubblicato dopo revisione. Grazie!"})


@app.route("/public-contributions", methods=["GET"])
def public_contributions():
    """Return approved contributions — role + insight + month/year only. Zero personal data."""
    supabase_url, supabase_key = _sb_config()
    if not supabase_url or not supabase_key:
        return jsonify([])
    try:
        r = requests.get(
            f"{supabase_url}/rest/v1/contributions"
            "?select=id,role,insight,created_at"
            "&approved=eq.true"
            "&order=created_at.desc"
            "&limit=50",
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=8,
        )
        if not r.ok:
            return jsonify([])
        rows = r.json() or []
        # Strip timestamp to month/year only — no fingerprinting
        for row in rows:
            if row.get("created_at"):
                try:
                    from datetime import datetime as _dt
                    dt = _dt.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                    row["date_label"] = dt.strftime("%B %Y")
                except Exception:
                    row["date_label"] = "2026"
                del row["created_at"]
        return jsonify(rows)
    except Exception:
        return jsonify([])


@app.route("/approve-contribution/<int:contrib_id>", methods=["GET"])
def approve_contribution(contrib_id):
    """Owner-only: approve a contribution. Called via link in Telegram."""
    token = request.args.get("token", "")
    if not _valid_contribution_token(token, contrib_id, "approve"):
        return jsonify({"error": "Unauthorized"}), 401
    supabase_url, supabase_key = _sb_config()
    try:
        r = requests.patch(
            f"{supabase_url}/rest/v1/contributions?id=eq.{contrib_id}",
            json={"approved": True},
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}",
                     "Content-Type": "application/json"},
            timeout=8,
        )
        if r.ok:
            return jsonify({"ok": True, "message": f"Contributo #{contrib_id} approvato e pubblicato."})
        return jsonify({"ok": False, "error": "Errore approvazione"}), 500
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"ok": False, "error": "internal_error"}), 500


@app.route("/reject-contribution/<int:contrib_id>", methods=["GET"])
def reject_contribution(contrib_id):
    """Owner-only: reject (delete) a contribution. Called via link in email."""
    token = request.args.get("token", "")
    if not _valid_contribution_token(token, contrib_id, "reject"):
        return jsonify({"error": "Unauthorized"}), 401
    supabase_url, supabase_key = _sb_config()
    try:
        r = requests.delete(
            f"{supabase_url}/rest/v1/contributions?id=eq.{contrib_id}",
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=8,
        )
        if r.ok:
            return jsonify({"ok": True, "message": f"Contributo #{contrib_id} rifiutato e rimosso."})
        return jsonify({"ok": False, "error": "Errore rifiuto"}), 500
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"ok": False, "error": "internal_error"}), 500


# ── BACKTEST ───────────────────────────────────────────────────────────────────

_last_backtest_run = 0.0
_BACKTEST_COOLDOWN = 3600  # seconds (1 hour)


@app.route("/run-backtest", methods=["POST"])
def run_backtest():
    """Trigger walk-forward backtest (rate-limited: once per hour)."""
    global _last_backtest_run
    now = time.time()
    elapsed = now - _last_backtest_run
    if elapsed < _BACKTEST_COOLDOWN:
        remaining = int(_BACKTEST_COOLDOWN - elapsed)
        return jsonify({
            "ok": False,
            "error": "rate_limited",
            "message": f"Backtest già in esecuzione o completato di recente. Riprova tra {remaining // 60}m {remaining % 60}s.",
            "cooldown_remaining": remaining,
        }), 429
    _last_backtest_run = now
    import subprocess, threading as _threading
    base = os.path.dirname(__file__)
    script = os.path.join(base, "backtest.py")

    def _run():
        try:
            subprocess.run(
                ["python3", script],
                cwd=base,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except Exception:
            pass

    _threading.Thread(target=_run, daemon=True).start()
    return jsonify({
        "ok": True,
        "message": "Backtest avviato in background. I risultati saranno disponibili in ~30-60 secondi.",
        "cooldown": _BACKTEST_COOLDOWN,
    })


@app.route("/xgb-report", methods=["GET"])
def xgb_report():
    """Return last XGBoost training report."""
    import os as _os
    report_path = _os.path.join(_os.path.dirname(__file__), "datasets", "xgb_report.txt")
    if not _os.path.exists(report_path):
        return jsonify({"error": "No XGBoost report found. Run train_xgb.py first."}), 404
    try:
        with open(report_path, "r") as f:
            content = f.read()
        lines = content.strip().split("\n")
        return jsonify({"report": content, "lines": len(lines), "ok": True})
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500

# ── TRAINING STATUS ───────────────────────────────────────────────────────────

@app.route("/training-status", methods=["GET"])
def training_status():
    """
    Returns auto-training system status: last retrain date, model accuracy,
    bets since retrain, next scheduled retrain (Sunday 3AM).
    """
    import datetime as _dt
    import re as _re

    base = os.path.dirname(__file__)
    model_path = os.path.join(base, "models", "xgb_direction.pkl")
    report_path = os.path.join(base, "datasets", "xgb_report.txt")

    # Last retrain timestamp — prefer "Generated:" line in xgb_report.txt
    # (immune to Railway deploys resetting mtime). Fallback: model file mtime.
    last_retrain_ts = None
    last_retrain_iso = None
    if os.path.exists(report_path):
        try:
            txt_ts = open(report_path).read()
            m_ts = _re.search(
                r"Generated:\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})",
                txt_ts,
            )
            if m_ts:
                last_retrain_ts = _dt.datetime.strptime(
                    m_ts.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S"
                )
        except Exception:
            pass
    if last_retrain_ts is None and os.path.exists(model_path):
        mtime = os.path.getmtime(model_path)
        last_retrain_ts = _dt.datetime.utcfromtimestamp(mtime)
    if last_retrain_ts is not None:
        last_retrain_iso = last_retrain_ts.strftime("%Y-%m-%d %H:%M UTC")

    # Parse accuracy from xgb_report.txt
    direction_acc = None
    train_n = None
    if os.path.exists(report_path):
        try:
            txt = open(report_path).read()
            m = _re.search(r"Direction model accuracy\s+([\d.]+)%", txt)
            if m:
                direction_acc = float(m.group(1))
            m2 = _re.search(r"Totale righe:\s+(\d+)", txt)
            if m2:
                train_n = int(m2.group(1))
        except Exception:
            pass

    # Bets since last retrain (from Supabase)
    bets_since = None
    sb_url, sb_key = _sb_config()
    if sb_url and sb_key and last_retrain_ts:
        try:
            cutoff = last_retrain_ts.strftime("%Y-%m-%dT%H:%M:%S")
            r = requests.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                f"?select=id&bet_taken=eq.true"
                f"&created_at=gt.{cutoff}",
                headers={
                    "apikey": sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Prefer": "count=exact",
                },
                timeout=5,
            )
            cr = r.headers.get("Content-Range", "")
            if "/" in cr:
                bets_since = int(cr.split("/")[1])
        except Exception:
            pass

    # Next scheduled retrain: next Sunday at 3AM UTC
    now_utc = _dt.datetime.utcnow()
    days_until_sunday = (6 - now_utc.weekday()) % 7
    if days_until_sunday == 0 and now_utc.hour >= 3:
        days_until_sunday = 7
    next_retrain_dt = (now_utc + _dt.timedelta(days=days_until_sunday)).replace(
        hour=3, minute=0, second=0, microsecond=0
    )
    next_retrain_iso = next_retrain_dt.strftime("%Y-%m-%d %H:%M UTC")

    # Status pill logic
    days_since = None
    if last_retrain_ts:
        days_since = (now_utc - last_retrain_ts).days
    retrain_threshold = 30  # bets needed for meaningful retrain
    if bets_since is not None and bets_since >= retrain_threshold:
        status = "RETRAIN_READY"
    elif days_since is not None and days_since <= 3:
        status = "RECENT"
    else:
        status = "ON_TRACK"

    # Calibration info (in-memory, resets on restart)
    last_cal_iso = None
    cal_remaining_secs = None
    if _force_retrain_last > 0:
        last_cal_iso = _dt.datetime.utcfromtimestamp(_force_retrain_last).strftime("%Y-%m-%d %H:%M UTC")
        elapsed = _dt.datetime.utcnow().timestamp() - _force_retrain_last
        cal_remaining_secs = max(0, int(3600 - elapsed))

    return jsonify({
        "last_retrain_iso": last_retrain_iso,
        "last_retrain_ts": last_retrain_ts.isoformat() if last_retrain_ts else None,
        "direction_acc": direction_acc,
        "train_n": train_n,
        "bets_since_retrain": bets_since,
        "next_retrain_iso": next_retrain_iso,
        "next_retrain_ts": next_retrain_dt.isoformat(),
        "retrain_threshold": retrain_threshold,
        "days_since_retrain": days_since,
        "status": status,
        # Calibration (force-retrain, in-memory)
        "last_calibrated_iso": last_cal_iso,
        "calibration_cooldown_remaining_secs": cal_remaining_secs,
        # Bot configuration & model status (for Training Tab in dashboard)
        "dead_hours": sorted(list(DEAD_HOURS_UTC)),
        "confidence_threshold": float(os.environ.get("CONF_THRESHOLD", "0.55")),
        "base_size_btc": float(os.environ.get("BASE_SIZE", "0.002")),
        "xgb_loaded": _XGB_MODEL is not None,
        "correctness_loaded": _xgb_correctness is not None,
        "model_path": model_path if os.path.exists(model_path) else None,
    })


# ── CONFIDENCE WATCHER ────────────────────────────────────────────────────────

@app.route("/confidence-stats", methods=["GET"])
def confidence_stats():
    """
    Analizza la distribuzione delle confidence degli ultimi N segnali.
    Rileva pattern 'stuck' (confidenza bloccata su un valore fisso).
    """
    try:
        n = min(int(request.args.get("n", 50)), 200)
        supabase_url, supabase_key = _sb_config()
        if not supabase_url or not supabase_key:
            return jsonify({"error": "supabase not configured"}), 500

        url = (
            f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
            f"?select=id,confidence,direction,bet_taken,created_at"
            f"&order=id.desc&limit={n}"
        )
        res = requests.get(url, headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }, timeout=8)
        if not res.ok:
            return jsonify({"error": f"supabase {res.status_code}"}), 502

        rows = res.json()
        if not rows:
            return jsonify({"error": "no data"}), 404

        confs = [float(r["confidence"]) for r in rows if r.get("confidence") is not None]
        if not confs:
            return jsonify({"error": "no confidence data"}), 404

        avg = sum(confs) / len(confs)
        variance = sum((c - avg) ** 2 for c in confs) / len(confs)
        std = variance ** 0.5
        mn, mx = min(confs), max(confs)

        # Distribuzione per bucket
        buckets = {
            "0.50-0.54": 0, "0.55-0.59": 0, "0.60-0.64": 0,
            "0.65-0.69": 0, "0.70-0.74": 0, "0.75-0.80": 0
        }
        for c in confs:
            if c < 0.55: buckets["0.50-0.54"] += 1
            elif c < 0.60: buckets["0.55-0.59"] += 1
            elif c < 0.65: buckets["0.60-0.64"] += 1
            elif c < 0.70: buckets["0.65-0.69"] += 1
            elif c < 0.75: buckets["0.70-0.74"] += 1
            else: buckets["0.75-0.80"] += 1

        # Threshold corrente
        threshold = float(os.environ.get("CONF_THRESHOLD", "0.55"))
        below_threshold = sum(1 for c in confs if c < threshold)

        # Trend: ultimi 10 vs precedenti
        recent = confs[:10] if len(confs) >= 10 else confs
        older = confs[10:30] if len(confs) >= 20 else []
        avg_recent = sum(recent) / len(recent)
        avg_older = sum(older) / len(older) if older else avg_recent
        trend = "rising" if avg_recent > avg_older + 0.01 else (
            "falling" if avg_recent < avg_older - 0.01 else "flat"
        )

        # Stuck detection: std < 0.02 su ultimi 10 segnali
        std_recent = (sum((c - avg_recent) ** 2 for c in recent) / len(recent)) ** 0.5
        stuck = std_recent < 0.02 and avg_recent < threshold + 0.05
        stuck_range = f"{min(recent):.2f}-{max(recent):.2f}" if stuck else None

        # Beat rate: % segnali che superano threshold
        beat_pct = round(100 * (len(confs) - below_threshold) / len(confs), 1)

        return jsonify({
            "n": len(confs),
            "avg": round(avg, 4),
            "std": round(std, 4),
            "min": round(mn, 4),
            "max": round(mx, 4),
            "threshold": threshold,
            "below_threshold": below_threshold,
            "beat_threshold_pct": beat_pct,
            "distribution": buckets,
            "trend": trend,
            "avg_recent_10": round(avg_recent, 4),
            "avg_older_10_30": round(avg_older, 4),
            "stuck": stuck,
            "stuck_range": stuck_range,
            "status": "🔴 STUCK" if stuck else ("🟡 LOW" if avg < threshold else "🟢 OK"),
        })
    except Exception as exc:
        app.logger.error("confidence_stats error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ── TRADING STATS ─────────────────────────────────────────────────────────────

@app.route("/trading-stats", methods=["GET"])
def trading_stats():
    """
    Legge la riga più recente dalla tabella trading_stats su Supabase
    e restituisce i dati in JSON.
    """
    try:
        supabase_url, supabase_key = _sb_config()

        if not supabase_url or not supabase_key:
            return jsonify({"error": "Supabase credentials not configured"}), 500

        url = f"{supabase_url}/rest/v1/trading_stats?select=*&limit=1"
        res = requests.get(url, headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }, timeout=8)

        if not res.ok:
            return jsonify({"error": f"Supabase HTTP {res.status_code}"}), 502

        rows = res.json()
        if not rows:
            return jsonify({"status": "ok", "data": None})

        return jsonify({"status": "ok", "data": rows[0]})

    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500

# ── MACRO GUARD ──────────────────────────────────────────────────────────────

# Cache in memoria: {"data": [...], "ts": float}
_macro_cache: dict = {"data": None, "ts": 0.0}

_MACRO_CACHE_TTL = 3600  # 1 ora
_MACRO_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def _fetch_macro_calendar() -> dict:
    """Fetcha il calendario ForexFactory con cache 1h.
    Ritorna {"data": [...], "fetch_failed": bool}.
    fetch_failed=True se la rete ha fallito e non c'è cache.
    """
    global _macro_cache
    now_ts = time.time()
    if _macro_cache["data"] is not None and (now_ts - _macro_cache["ts"]) < _MACRO_CACHE_TTL:
        return {"data": _macro_cache["data"], "fetch_failed": False}
    try:
        r = requests.get(_MACRO_CALENDAR_URL, timeout=5)
        if r.ok:
            data = r.json()
            _macro_cache = {"data": data, "ts": now_ts}
            return {"data": data, "fetch_failed": False}
    except Exception:
        pass
    # Ritorna cache scaduta se disponibile (meglio che niente)
    cached = _macro_cache["data"]
    return {"data": cached or [], "fetch_failed": cached is None}


@app.route("/macro-guard", methods=["GET"])
def macro_guard():
    """Controlla se nelle prossime 2h ci sono eventi macro USD ad alto impatto.

    Risposta:
      {"blocked": true,  "reason": "NFP in 47min", "event": {...}}
      {"blocked": false}
      {"blocked": false, "error": "calendar_unavailable"}
    """
    err = _check_api_key()
    if err:
        return err

    cal = _fetch_macro_calendar()
    events = cal["data"]
    if cal["fetch_failed"]:
        return jsonify({"blocked": False, "error": "calendar_fetch_error"})
    if not events:
        return jsonify({"blocked": False, "error": "calendar_unavailable"})

    now_utc = _dt.datetime.now(_dt.timezone.utc)
    window_end = now_utc + _dt.timedelta(hours=2)

    for event in events:
        # Filtra solo USD ad alto impatto
        if event.get("country") != "USD":
            continue
        if event.get("impact") not in ("High", "red"):
            continue

        raw_date = event.get("date", "")
        if not raw_date:
            continue

        try:
            # La data è ISO 8601 con offset, es. "2026-02-26T08:30:00-05:00"
            event_dt = _dt.datetime.fromisoformat(raw_date)
            # Normalizza a UTC
            event_dt_utc = event_dt.astimezone(_dt.timezone.utc)
        except Exception:
            continue

        # Evento nella finestra [now, now+2h]
        if now_utc <= event_dt_utc <= window_end:
            delta_min = int((event_dt_utc - now_utc).total_seconds() / 60)
            title = event.get("title", "Unknown event")
            reason = f"{title} in {delta_min}min"
            return jsonify({
                "blocked": True,
                "reason": reason,
                "event": {
                    "title": title,
                    "country": event.get("country"),
                    "date_utc": event_dt_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "impact": event.get("impact"),
                    "forecast": event.get("forecast"),
                    "previous": event.get("previous"),
                    "minutes_away": delta_min,
                },
            })

    return jsonify({"blocked": False})


# ── ON-CHAIN AUDIT TRAIL (Polygon PoS) ────────────────────────────────────────
#
# Richiede: web3 (pip), POLYGON_PRIVATE_KEY + POLYGON_CONTRACT_ADDRESS su Railway.
# Hash formula commit:  keccak256(abi.encodePacked(betId, direction, confidence, entryPrice, betSize, timestamp))
# Hash formula resolve: keccak256(abi.encodePacked(betId, exitPrice, pnlUsd, won, closeTimestamp))
#
# ABI minimo del contratto BTCBotAudit.sol (solo funzioni usate):
_BTCBOT_AUDIT_ABI = [
    {"inputs":[{"internalType":"uint256","name":"betId","type":"uint256"},
               {"internalType":"bytes32","name":"commitHash","type":"bytes32"}],
     "name":"commit","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"betId","type":"uint256"},
               {"internalType":"bytes32","name":"resolveHash","type":"bytes32"},
               {"internalType":"bool","name":"won","type":"bool"}],
     "name":"resolve","outputs":[],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"betId","type":"uint256"}],
     "name":"getCommit","outputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"betId","type":"uint256"}],
     "name":"isCommitted","outputs":[{"internalType":"bool","name":"","type":"bool"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"internalType":"uint256","name":"betId","type":"uint256"}],
     "name":"isResolved","outputs":[{"internalType":"bool","name":"","type":"bool"}],
     "stateMutability":"view","type":"function"},
]

def _get_web3_contract():
    """Restituisce (w3, contract, account) oppure raise RuntimeError se non configurato."""
    try:
        from web3 import Web3
        from web3.middleware import geth_poa_middleware
    except ImportError:
        raise RuntimeError("web3 non installato")

    private_key = os.environ.get("POLYGON_PRIVATE_KEY", "")
    contract_address = os.environ.get("POLYGON_CONTRACT_ADDRESS", "")
    if not private_key or not contract_address:
        raise RuntimeError("POLYGON_PRIVATE_KEY o POLYGON_CONTRACT_ADDRESS non configurati")

    rpc_url = os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
    w3.middleware_onion.inject(geth_poa_middleware, layer=0)

    account = w3.eth.account.from_key(private_key)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(contract_address),
        abi=_BTCBOT_AUDIT_ABI
    )
    return w3, contract, account


@app.route("/commit-prediction", methods=["POST"])
def commit_prediction():
    """
    Committa l'hash di una prediction su Polygon.
    Body JSON: { bet_id, direction, confidence, entry_price, bet_size, timestamp }
    Salva onchain_commit_hash + onchain_commit_tx su Supabase.
    """
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    required = ["bet_id", "direction", "confidence", "entry_price", "bet_size", "timestamp"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"ok": False, "error": f"Campi mancanti: {missing}"}), 400

    bet_id = int(data["bet_id"])
    direction = str(data["direction"]).upper()     # "UP" o "DOWN"
    confidence = float(data["confidence"])
    entry_price = float(data["entry_price"])
    bet_size = float(data["bet_size"])
    ts = int(data["timestamp"])

    try:
        from web3 import Web3
        try:
            w3, contract, account = _get_web3_contract()
        except RuntimeError as cfg_err:
            app.logger.error(f"[ONCHAIN] config error: {cfg_err}")
            return jsonify({"ok": False, "error": "polygon_not_configured"}), 503

        # Calcola hash deterministico della prediction
        commit_hash = Web3.solidity_keccak(
            ["uint256", "string", "uint256", "uint256", "uint256", "uint256"],
            [bet_id, direction, int(confidence * 1e6), int(entry_price * 1e2), int(bet_size * 1e8), ts]
        )

        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        tx = contract.functions.commit(bet_id, commit_hash).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": 80000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        tx_hex = tx_hash.hex()

        commit_hash_hex = commit_hash.hex()

        app.logger.info(f"[ONCHAIN] commit bet #{bet_id} → tx {tx_hex}")

        # Aggiorna Supabase — errore non critico (tx già inviata on-chain)
        try:
            _supabase_update(bet_id, {
                "onchain_commit_hash": commit_hash_hex,
                "onchain_commit_tx": tx_hex,
            })
        except Exception as sb_err:
            app.logger.error(f"[ONCHAIN] Supabase update failed for bet #{bet_id}: {sb_err}")
            return jsonify({"ok": True, "commit_hash": commit_hash_hex, "tx": tx_hex,
                            "warning": "tx sent but Supabase update failed"})

        return jsonify({"ok": True, "commit_hash": commit_hash_hex, "tx": tx_hex})

    except Exception as e:
        app.logger.error(f"[ONCHAIN] commit_prediction error: {e}")
        app.logger.exception("Endpoint error")
        return jsonify({"ok": False, "error": "internal_error"}), 500


@app.route("/resolve-prediction", methods=["POST"])
def resolve_prediction():
    """
    Risolve l'hash dell'outcome di una bet su Polygon.
    Body JSON: { bet_id, exit_price, pnl_usd, won, close_timestamp }
    Salva onchain_resolve_hash + onchain_resolve_tx su Supabase.
    """
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    required = ["bet_id", "exit_price", "pnl_usd", "won", "close_timestamp"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"ok": False, "error": f"Campi mancanti: {missing}"}), 400

    bet_id = int(data["bet_id"])
    exit_price = float(data["exit_price"])
    pnl_usd = float(data["pnl_usd"])
    won = bool(data["won"])
    close_ts = int(data["close_timestamp"])

    try:
        from web3 import Web3
        try:
            w3, contract, account = _get_web3_contract()
        except RuntimeError as cfg_err:
            app.logger.error(f"[ONCHAIN] config error: {cfg_err}")
            return jsonify({"ok": False, "error": "polygon_not_configured"}), 503

        resolve_hash = Web3.solidity_keccak(
            ["uint256", "uint256", "int256", "bool", "uint256"],
            [bet_id, int(exit_price * 1e2), int(pnl_usd * 1e6), won, close_ts]
        )

        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        tx = contract.functions.resolve(bet_id, resolve_hash, won).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": 80000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        tx_hex = tx_hash.hex()

        resolve_hash_hex = resolve_hash.hex()

        app.logger.info(f"[ONCHAIN] resolve bet #{bet_id} won={won} → tx {tx_hex}")

        # Aggiorna Supabase — errore non critico (tx già inviata on-chain)
        try:
            _supabase_update(bet_id, {
                "onchain_resolve_hash": resolve_hash_hex,
                "onchain_resolve_tx": tx_hex,
            })
        except Exception as sb_err:
            app.logger.error(f"[ONCHAIN] Supabase update failed for bet #{bet_id}: {sb_err}")
            return jsonify({"ok": True, "resolve_hash": resolve_hash_hex, "tx": tx_hex,
                            "warning": "tx sent but Supabase update failed"})

        return jsonify({"ok": True, "resolve_hash": resolve_hash_hex, "tx": tx_hex})

    except Exception as e:
        app.logger.error(f"[ONCHAIN] resolve_prediction error: {e}")
        app.logger.exception("Endpoint error")
        return jsonify({"ok": False, "error": "internal_error"}), 500


# ── ON-CHAIN FASI AGGIUNTIVE (INPUT HASH · FILL CONFIRM · SL/TP) ──────────────
# Convenzione offset bet_id (usa existing commit() senza redeploy contratto):
#   Fase inputs:   bet_id + 10_000_000
#   Fase fill:     bet_id + 20_000_000
#   Fase stops:    bet_id + 30_000_000

@app.route("/commit-inputs", methods=["POST"])
def commit_inputs():
    """
    Committa hash degli input pre-LLM su Polygon.
    Body JSON: { btc_price, rsi14, fg_value, funding_rate, timestamp, [bet_id] }
    - Se bet_id omesso o 0: usa timestamp+10_000_000_000 come onchain_id (pre-row)
      → il tx viene restituito ma NON aggiornato in Supabase (bet_id sconosciuto)
    - Se bet_id > 0: usa bet_id+10_000_000 e aggiorna onchain_inputs_tx in Supabase
    Prova che gli input di mercato erano reali PRIMA della chiamata LLM.
    """
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    required = ["btc_price", "rsi14", "fg_value", "funding_rate", "timestamp"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"ok": False, "error": f"Campi mancanti: {missing}"}), 400

    bet_id    = int(data.get("bet_id") or 0)
    btc_price = float(data["btc_price"])
    rsi14     = float(data["rsi14"])
    fg_value  = int(data["fg_value"])
    funding   = float(data["funding_rate"])
    ts        = int(data["timestamp"])

    try:
        from web3 import Web3
        try:
            w3, contract, account = _get_web3_contract()
        except RuntimeError as cfg_err:
            app.logger.error(f"[ONCHAIN] config error: {cfg_err}")
            return jsonify({"ok": False, "error": "polygon_not_configured"}), 503

        # bet_id=0 → usa timestamp come ID univoco per commit pre-row
        onchain_id = (bet_id + 10_000_000) if bet_id > 0 else (ts + 10_000_000_000)
        commit_hash = Web3.solidity_keccak(
            ["uint256", "uint256", "int256", "int256", "int256", "uint256"],
            [bet_id, int(btc_price * 1e2), int(rsi14 * 1e6), fg_value,
             int(funding * 1e8), ts]
        )
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        tx = contract.functions.commit(onchain_id, commit_hash).build_transaction({
            "from": account.address, "nonce": nonce,
            "gas": 80000, "gasPrice": w3.to_wei("30", "gwei"), "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        tx_hex = tx_hash.hex()
        app.logger.info(f"[ONCHAIN] inputs onchain_id={onchain_id} → tx {tx_hex}")

        if bet_id > 0:
            try:
                _supabase_update(bet_id, {"onchain_inputs_tx": tx_hex})
            except Exception as sb_err:
                app.logger.error(f"[ONCHAIN] Supabase update failed: {sb_err}")
                return jsonify({"ok": True, "tx": tx_hex, "onchain_id": onchain_id,
                                "warning": "tx sent but Supabase update failed"})

        return jsonify({"ok": True, "tx": tx_hex, "onchain_id": onchain_id})

    except Exception as e:
        app.logger.error(f"[ONCHAIN] commit_inputs error: {type(e).__name__}: {e}")
        app.logger.exception("Endpoint error")
        return jsonify({"ok": False, "error": "internal_error"}), 500


@app.route("/commit-fill", methods=["POST"])
def commit_fill():
    """
    Committa il prezzo di fill reale post-esecuzione Kraken.
    Body JSON: { bet_id, entry_fill_price, timestamp_fill }
    Crea record immutabile dello slippage reale.
    """
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    required = ["bet_id", "entry_fill_price", "timestamp_fill"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"ok": False, "error": f"Campi mancanti: {missing}"}), 400

    bet_id     = int(data["bet_id"])
    fill_price = float(data["entry_fill_price"])
    ts_fill    = int(data["timestamp_fill"])

    try:
        from web3 import Web3
        try:
            w3, contract, account = _get_web3_contract()
        except RuntimeError as cfg_err:
            app.logger.error(f"[ONCHAIN] config error: {cfg_err}")
            return jsonify({"ok": False, "error": "polygon_not_configured"}), 503

        onchain_id = bet_id + 20_000_000
        commit_hash = Web3.solidity_keccak(
            ["uint256", "uint256", "uint256"],
            [bet_id, int(fill_price * 1e2), ts_fill]
        )
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        tx = contract.functions.commit(onchain_id, commit_hash).build_transaction({
            "from": account.address, "nonce": nonce,
            "gas": 80000, "gasPrice": w3.to_wei("30", "gwei"), "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        tx_hex = tx_hash.hex()
        app.logger.info(f"[ONCHAIN] fill bet #{bet_id} price={fill_price} → tx {tx_hex}")

        try:
            _supabase_update(bet_id, {"onchain_fill_tx": tx_hex})
        except Exception as sb_err:
            app.logger.error(f"[ONCHAIN] Supabase update failed: {sb_err}")
            return jsonify({"ok": True, "tx": tx_hex, "warning": "tx sent but Supabase update failed"})

        return jsonify({"ok": True, "tx": tx_hex, "onchain_id": onchain_id})

    except Exception as e:
        app.logger.error(f"[ONCHAIN] commit_fill error: {type(e).__name__}: {e}")
        app.logger.exception("Endpoint error")
        return jsonify({"ok": False, "error": "internal_error"}), 500


@app.route("/commit-stops", methods=["POST"])
def commit_stops():
    """
    Committa prezzi SL/TP post-piazzamento su Polygon.
    Body JSON: { bet_id, sl_price, tp_price, timestamp }
    Prova che lo stop era piazzato PRIMA di qualsiasi movimento significativo.
    """
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    required = ["bet_id", "sl_price", "tp_price", "timestamp"]
    missing = [f for f in required if f not in data]
    if missing:
        return jsonify({"ok": False, "error": f"Campi mancanti: {missing}"}), 400

    bet_id   = int(data["bet_id"])
    sl_price = float(data["sl_price"])
    tp_price = float(data["tp_price"])
    ts       = int(data["timestamp"])

    try:
        from web3 import Web3
        try:
            w3, contract, account = _get_web3_contract()
        except RuntimeError as cfg_err:
            app.logger.error(f"[ONCHAIN] config error: {cfg_err}")
            return jsonify({"ok": False, "error": "polygon_not_configured"}), 503

        onchain_id = bet_id + 30_000_000
        commit_hash = Web3.solidity_keccak(
            ["uint256", "uint256", "uint256", "uint256"],
            [bet_id, int(sl_price * 1e2), int(tp_price * 1e2), ts]
        )
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        tx = contract.functions.commit(onchain_id, commit_hash).build_transaction({
            "from": account.address, "nonce": nonce,
            "gas": 80000, "gasPrice": w3.to_wei("30", "gwei"), "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
        tx_hex = tx_hash.hex()
        app.logger.info(f"[ONCHAIN] stops bet #{bet_id} sl={sl_price} tp={tp_price} → tx {tx_hex}")

        try:
            _supabase_update(bet_id, {"onchain_stops_tx": tx_hex})
        except Exception as sb_err:
            app.logger.error(f"[ONCHAIN] Supabase update failed: {sb_err}")
            return jsonify({"ok": True, "tx": tx_hex, "warning": "tx sent but Supabase update failed"})

        return jsonify({"ok": True, "tx": tx_hex, "onchain_id": onchain_id})

    except Exception as e:
        app.logger.error(f"[ONCHAIN] commit_stops error: {type(e).__name__}: {e}")
        app.logger.exception("Endpoint error")
        return jsonify({"ok": False, "error": "internal_error"}), 500


def _supabase_update(bet_id: int, fields: dict):
    """Helper: aggiorna una riga Supabase per bet_id."""
    sb_url, sb_key = _sb_config()
    url = f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}"
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    r = requests.patch(url, json=fields, headers=headers, timeout=10)
    try:
        r.raise_for_status()
    except Exception as e:
        app.logger.error("_supabase_update bet_id=%s error: %s", bet_id, e)


# ── NEWS BLOCKCHAIN FACT-CHECK ────────────────────────────────────────────────
#
# Committa l'hash di una news su Polygon PRIMA della pubblicazione marketing.
# Crea prova crittografica immutabile che la fonte esisteva prima del post.
# Convention onchain_id: 50_000_000 + news_db_id (distinguibile da bet IDs).
# Hash formula: sha256(url + "|" + headline + "|" + str(unix_timestamp))

@app.route("/news-fact-check", methods=["POST"])
def news_fact_check():
    """
    Registra l'hash di una news su Polygon prima della pubblicazione.
    Body JSON: { url, headline, content_snippet?, source?, news_published_at? }
    Returns: { ok, id, hash, onchain_id, tx_hash, polygonscan_url }
    """
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    url = str(data.get("url", "")).strip()
    headline = str(data.get("headline", "")).strip()
    if not url:
        return jsonify({"ok": False, "error": "url richiesto"}), 400

    content_snippet = str(data.get("content_snippet", ""))[:500]
    source = str(data.get("source", ""))[:100]
    news_published_at = data.get("news_published_at")  # ISO string o null

    # Calcola timestamp di riferimento per l'hash
    try:
        if news_published_at:
            import datetime as _dt_nfc
            ts_nfc = int(_dt_nfc.datetime.fromisoformat(
                news_published_at.replace("Z", "+00:00")).timestamp())
        else:
            ts_nfc = int(_dt.datetime.utcnow().timestamp())
    except Exception:
        ts_nfc = int(_dt.datetime.utcnow().timestamp())

    # Hash SHA-256 della news (deterministico: stessa news = stesso hash)
    raw_nfc = f"{url}|{headline}|{ts_nfc}".encode("utf-8")
    hash_hex = hashlib.sha256(raw_nfc).hexdigest()

    # Insert in Supabase (ottieni ID per onchain_id convention)
    supabase_url, supabase_key = _sb_config()
    news_id = None
    try:
        row = {
            "url": url,
            "headline": headline,
            "content_snippet": content_snippet,
            "source": source,
            "news_published_at": news_published_at,
            "hash_sha256": hash_hex,
            "status": "pending",
        }
        resp = requests.post(
            f"{supabase_url}/rest/v1/news_fact_checks",
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json=row,
            timeout=8,
        )
        if resp.ok:
            news_id = resp.json()[0]["id"]
        else:
            app.logger.warning(f"[NEWS-FC] Supabase insert failed: {resp.status_code}")
    except Exception as e:
        app.logger.warning(f"[NEWS-FC] Supabase insert error: {e}")

    onchain_id = 50_000_000 + (news_id or 0)

    # Commit su Polygon
    try:
        w3, contract, account = _get_web3_contract()
        commit_hash_bytes = hashlib.sha256(raw_nfc).digest()  # 32 bytes
        nonce = w3.eth.get_transaction_count(account.address, "pending")
        tx = contract.functions.commit(onchain_id, commit_hash_bytes).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": 80000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "chainId": 137,
        })
        signed = account.sign_transaction(tx)
        tx_hash_raw = w3.eth.send_raw_transaction(signed.rawTransaction)
        tx_hex = tx_hash_raw.hex()
        polygonscan_url = f"https://polygonscan.com/tx/{tx_hex}"
        app.logger.info(f"[NEWS-FC] id={news_id} onchain_id={onchain_id} → tx {tx_hex}")

        # Aggiorna Supabase con tx
        if news_id:
            try:
                requests.patch(
                    f"{supabase_url}/rest/v1/news_fact_checks?id=eq.{news_id}",
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "onchain_id": onchain_id,
                        "polygon_tx_hash": tx_hex,
                        "polygonscan_url": polygonscan_url,
                        "status": "committed",
                    },
                    timeout=8,
                )
            except Exception as e_upd:
                app.logger.warning(f"[NEWS-FC] Supabase update error: {e_upd}")

        return jsonify({
            "ok": True,
            "id": news_id,
            "hash": hash_hex,
            "onchain_id": onchain_id,
            "tx_hash": tx_hex,
            "polygonscan_url": polygonscan_url,
        })

    except Exception as e:
        app.logger.error(f"[NEWS-FC] Polygon commit failed: {e}")
        # Aggiorna status failed su Supabase
        if news_id:
            try:
                requests.patch(
                    f"{supabase_url}/rest/v1/news_fact_checks?id=eq.{news_id}",
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                    },
                    json={"status": "failed"},
                    timeout=8,
                )
            except Exception:
                pass
        return jsonify({
            "ok": False,
            "id": news_id,
            "hash": hash_hex,
            "error": str(e),
            "note": "Hash salvato in Supabase ma non committato on-chain",
        }), 500


# ── DASHBOARD ────────────────────────────────────────────────────────────────

@app.route("/agent.json", methods=["GET"])
@app.route("/.well-known/agent.json", methods=["GET"])
def agent_json():
    """Machine-readable identity file for AI agents (agent.json standard)."""
    try:
        with open("static/agent.json", "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = '{"name":"BTC Predictor","url":"https://btcpredictor.io"}'
    return content, 200, {
        "Content-Type": "application/json; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=3600",
    }

@app.route("/AGENTS.md", methods=["GET"])
def agents_md():
    """AGENTS.md — guide for AI agents and contributors."""
    try:
        with open("AGENTS.md", "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = "# AGENTS.md\nhttps://github.com/mattiacalastri/btc_predictions/blob/main/AGENTS.md\n"
    return content, 200, {
        "Content-Type": "text/markdown; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
    }

@app.route("/llms.txt", methods=["GET"])
def llms_txt():
    """AI crawler context file (llms.txt standard)."""
    try:
        with open("static/llms.txt", "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = "# BTC Predictor\nhttps://btcpredictor.io\n"
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.route("/og-image.png", methods=["GET"])
def og_image():
    """Serve the Open Graph image for social sharing."""
    import os as _os
    img_path = _os.path.join(_os.path.dirname(__file__), "og-image.png")
    if not _os.path.exists(img_path):
        return "", 404
    with open(img_path, "rb") as f:
        data = f.read()
    return data, 200, {"Content-Type": "image/png", "Cache-Control": "public, max-age=86400"}

_GOOGLE_SITE_VERIFICATION = os.environ.get("GOOGLE_SITE_VERIFICATION", "")


@app.route("/google<code>.html", methods=["GET"])
def google_verification(code):
    """Google Search Console HTML file verification."""
    expected = _GOOGLE_SITE_VERIFICATION
    if not expected or code != expected:
        return "Not Found", 404
    html = f"google-site-verification: google{code}.html"
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    """robots.txt — allow all crawlers, point to llms.txt."""
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /place-bet\n"
        "Disallow: /close-position\n"
        "Disallow: /pause\n"
        "Disallow: /resume\n"
        "Disallow: /admin/\n"
        "Disallow: /cockpit\n"
        "Disallow: /health\n"
        "Disallow: /predict-xgb\n"
        "Disallow: /marketing-stats\n"
        "\n"
        "# AI crawlers\n"
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "User-agent: ClaudeBot\n"
        "Allow: /\n"
        "User-agent: PerplexityBot\n"
        "Allow: /\n"
        "\n"
        "Sitemap: https://btcpredictor.io/sitemap.xml\n"
        "LLMs: https://btcpredictor.io/llms.txt\n"
        "AgentProfile: https://btcpredictor.io/agent.json\n"
        "AgentGuide: https://btcpredictor.io/AGENTS.md\n"
    )
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Sitemap with lastmod dates for better crawl budget."""
    today = _dt.date.today().isoformat()
    pages = [
        ("https://btcpredictor.io",                        "daily",   "1.0", today),
        ("https://btcpredictor.io/dashboard",              "hourly",  "1.0", today),
        ("https://btcpredictor.io/manifesto",              "monthly", "0.8", "2026-02-27"),
        ("https://btcpredictor.io/prevedibilita-perfetta", "monthly", "0.9", "2026-02-27"),
        ("https://btcpredictor.io/contributors",           "weekly",  "0.7", "2026-03-01"),
        ("https://btcpredictor.io/xgboost-spiegato",       "monthly", "0.8", "2026-02-27"),
        ("https://btcpredictor.io/aureo",                  "monthly", "0.7", "2026-03-01"),
        ("https://btcpredictor.io/legal",                  "monthly", "0.3", "2026-03-01"),
    ]
    urls = ""
    for loc, freq, prio, lastmod in pages:
        urls += (
            f"  <url>\n"
            f"    <loc>{loc}</loc>\n"
            f"    <lastmod>{lastmod}</lastmod>\n"
            f"    <changefreq>{freq}</changefreq>\n"
            f"    <priority>{prio}</priority>\n"
            f"  </url>\n"
        )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f'{urls}'
        '</urlset>\n'
    )
    return xml, 200, {"Content-Type": "application/xml"}

@app.route("/legal", methods=["GET"])
def legal():
    """Legal Notice, Financial Disclaimer & Privacy Policy page."""
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Legal Notice &amp; Privacy Policy — BTC Predictor</title>
<meta name="robots" content="index, follow">
<style>
  *, *::before, *::after { box-sizing: border-box; }
  body { background: #080c19; color: #c8d0e0; font-family: 'JetBrains Mono', 'Courier New', monospace; font-size: 13px; line-height: 1.7; margin: 0; padding: 0; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 48px 24px 80px; }
  h1 { color: #00ff88; font-size: 20px; letter-spacing: 2px; margin-bottom: 8px; }
  h2 { color: #7eb8f7; font-size: 13px; letter-spacing: 1.5px; margin: 36px 0 10px; border-bottom: 1px solid rgba(255,255,255,0.06); padding-bottom: 6px; }
  p, li { color: rgba(200,208,224,0.8); margin: 0 0 10px; }
  ul { padding-left: 20px; }
  a { color: #00ff88; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .meta { font-size: 10px; color: rgba(255,255,255,0.3); margin-bottom: 40px; letter-spacing: 1px; }
  .back { display: inline-block; margin-bottom: 32px; font-size: 11px; color: rgba(255,255,255,0.4); letter-spacing: 1px; border: 1px solid rgba(255,255,255,0.1); padding: 5px 14px; border-radius: 2px; }
  .back:hover { color: #00ff88; border-color: rgba(0,255,136,0.3); }
</style>
</head>
<body>
<div class="wrap">
  <a href="/dashboard" class="back">← BACK TO DASHBOARD</a>
  <h1>LEGAL NOTICE &amp; PRIVACY POLICY</h1>
  <div class="meta">BTC Predictor · btcpredictor.io · Last updated: 2026-02-26</div>

  <h2>1. LEGAL NOTICE (AVVISO LEGALE)</h2>
  <p>This website and its services are operated by <strong>Astra Digital Marketing</strong> (hereinafter "we", "us", "the operator").<br>
  Contact: <a href="mailto:signal@btcpredictor.io">signal@btcpredictor.io</a></p>
  <p>The BTC Predictor dashboard and all associated software is published as open-source under the MIT License. The source code is publicly available at <a href="https://github.com/mattiacalastri/btc_predictions" target="_blank" rel="noopener">github.com/mattiacalastri/btc_predictions</a>.</p>

  <h2>2. FINANCIAL DISCLAIMER</h2>
  <p><strong>This website, dashboard, and all associated content is for informational and educational purposes only. Nothing on this site constitutes financial advice, investment advice, trading advice, or any other form of advice.</strong></p>
  <ul>
    <li>The performance metrics, win rates, PnL figures, and backtesting results displayed are historical data and are <strong>not indicative of future results</strong>.</li>
    <li>Cryptocurrency and cryptocurrency futures trading involves <strong>substantial risk of loss</strong>. You may lose some or all of your capital.</li>
    <li>The operator is <strong>not a licensed financial advisor</strong>, broker, or investment firm in any jurisdiction.</li>
    <li>Any automated trading system — including this one — can malfunction, produce erroneous signals, or fail to execute orders correctly. Technical failures, API outages, or exchange issues may result in losses beyond those modeled.</li>
    <li>By accessing this dashboard or deploying any code from this repository, you <strong>accept full personal responsibility</strong> for any and all trading decisions, outcomes, and financial losses.</li>
    <li>This system is subject to applicable laws and regulations in your jurisdiction, including but not limited to MiCA / ESMA (EU), SEC (US), and other local financial regulations. It is your responsibility to ensure compliance.</li>
  </ul>
  <p>The operator expressly disclaims all liability for any direct, indirect, incidental, or consequential damages arising from use of or reliance on this service.</p>

  <h2>3. PRIVACY POLICY &amp; COOKIE POLICY</h2>
  <p>This website uses analytics and monitoring tools to improve the service. All third-party scripts are loaded <strong>only after explicit user consent</strong> via the cookie banner. No registration or login is required. No user accounts exist.</p>

  <h2>3.1 THIRD-PARTY ANALYTICS TOOLS</h2>
  <p>The following tools are activated <strong>only if you accept analytics</strong> via the cookie banner:</p>
  <ul>
    <li><strong>Google Analytics 4</strong> — Provider: Google LLC. Collects: page views, traffic sources, time on site. Cookies: <code>_ga</code>, <code>_gid</code>, <code>_gat</code>. Default: denied until consent. <a href="https://policies.google.com/privacy" target="_blank" rel="noopener">Google Privacy Policy ↗</a></li>
    <li><strong>Microsoft Clarity</strong> — Provider: Microsoft Corporation. Collects: heatmaps, session replay, click tracking. Storage: <code>clarity_*</code>, <code>clr_*</code> in localStorage. Default: not loaded until consent. <a href="https://privacy.microsoft.com/en-us/privacystatement" target="_blank" rel="noopener">Microsoft Privacy Policy ↗</a></li>
    <li><strong>Sentry</strong> — Provider: Functional Software Inc. Purpose: JavaScript error monitoring and performance tracking. <code>sendDefaultPii: false</code> — no personally identifiable information is sent. Default: not loaded until consent. <a href="https://sentry.io/privacy/" target="_blank" rel="noopener">Sentry Privacy Policy ↗</a></li>
  </ul>
  <p>If you <strong>decline</strong> the cookie banner, none of these scripts are loaded. Your choice is stored in <code>localStorage</code> (key: <code>btcp_ga_consent</code>) to avoid showing the banner on every visit.</p>

  <h2>3.2 LOCAL STORAGE USAGE</h2>
  <p>We store the following data in your browser's localStorage — never on our servers:</p>
  <ul>
    <li><code>btcp_ga_consent</code> — your analytics preference ("granted" or "denied")</li>
    <li><code>introDismissed</code> — whether you have closed the tutorial intro (UI state only)</li>
    <li><code>clarity_*</code>, <code>clr_*</code> — set by Microsoft Clarity if analytics accepted</li>
    <li><code>_sentry_*</code> — set by Sentry if analytics accepted</li>
  </ul>
  <p>You can clear all localStorage data at any time via your browser's developer tools (Application → Local Storage → Clear All).</p>

  <h2>3.3 DATA SUBJECT RIGHTS (GDPR ART. 12-22)</h2>
  <p>You have the right to: access your data · request erasure · object to processing · file a complaint with the Italian data protection authority (<a href="https://www.garanteprivacy.it" target="_blank" rel="noopener">Garante per la Protezione dei Dati Personali ↗</a>).</p>
  <p>To exercise these rights: <a href="mailto:signal@btcpredictor.io">signal@btcpredictor.io</a></p>

  <h2>3.4 BACKEND &amp; API</h2>
  <ul>
    <li><strong>No registration or login</strong> required. No user accounts exist.</li>
    <li>The dashboard fetches live data from our own backend API (Railway) and from on-chain public data (Polygon PoS). No personal data is transmitted in these requests.</li>
    <li>The <code>/submit-contribution</code> endpoint accepts voluntary text submissions. Submitted text is stored in our database (Supabase) and may be displayed publicly. Do not include personal data in submissions.</li>
    <li>If you contact us by email at <a href="mailto:signal@btcpredictor.io">signal@btcpredictor.io</a>, your email address and message will be stored only to respond to your enquiry and will not be shared with third parties.</li>
  </ul>
  <p>Last updated: 2026-02-27. For questions: <a href="mailto:signal@btcpredictor.io">signal@btcpredictor.io</a></p>

  <h2>4. INTELLECTUAL PROPERTY</h2>
  <p>The source code of BTC Predictor is released under the <strong>MIT License</strong>. You are free to use, modify, and distribute it subject to the license terms. The "BTC Predictor" name, "Astra Digital Marketing" name, and associated branding remain the property of the operator.</p>

  <h2>5. GOVERNING LAW</h2>
  <p>This legal notice is governed by Italian law. Any disputes arising from the use of this service shall be subject to the exclusive jurisdiction of the competent Italian courts.</p>

  <div style="margin-top:48px;padding-top:20px;border-top:1px solid rgba(255,255,255,0.06);font-size:10px;color:rgba(255,255,255,0.25);letter-spacing:0.5px">
    BTC Predictor · <a href="https://btcpredictor.io/dashboard">btcpredictor.io</a> · Open source on <a href="https://github.com/mattiacalastri/btc_predictions" target="_blank" rel="noopener">GitHub</a> · On-chain audit: <a href="https://polygonscan.com/address/0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55" target="_blank" rel="noopener">Polygon PoS ↗</a>
  </div>
</div>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/", methods=["GET"])
def index():
    with open("home.html", "r") as f:
        html = f.read()
    if _GOOGLE_SITE_VERIFICATION:
        meta = f'<meta name="google-site-verification" content="{_GOOGLE_SITE_VERIFICATION}">'
        html = html.replace("</head>", meta + "\n</head>", 1)
    return html, 200, {"Content-Type": "text/html"}


@app.route("/manifesto", methods=["GET"])
def manifesto():
    with open("manifesto.html", "r") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html"}


@app.route("/prevedibilita-perfetta", methods=["GET"])
def prevedibilita():
    with open("prevedibilita.html", "r") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html"}


@app.route("/investors", methods=["GET"])
def investors():
    with open("investors.html", "r") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html"}


@app.route("/aureo", methods=["GET"])
def aureo():
    with open("aureo.html", "r") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html"}


@app.route("/contributors", methods=["GET"])
def contributors():
    with open("contributors.html", "r") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html"}


_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')

@app.route("/satoshi-lead", methods=["POST"])
def satoshi_lead():
    """Salva email raccolta dal widget Satoshi in Supabase leads."""
    data = request.get_json(silent=True) or {}

    # ── reCAPTCHA v3 (primary) + Turnstile fallback ───────────────────
    recaptcha_ok = _verify_recaptcha(data.get("recaptcha_token", ""), "satoshi_lead")
    if not recaptcha_ok:
        ts_secret = os.environ.get("TURNSTILE_SECRET_KEY", "")
        if ts_secret:
            cf_token = str(data.get("cf_turnstile_token", "")).strip()
            if not cf_token:
                return jsonify({"ok": False, "error": "captcha_required"}), 400
            try:
                ts_resp = requests.post(
                    "https://challenges.cloudflare.com/turnstile/v0/siteverify",
                    json={"secret": ts_secret, "response": cf_token},
                    timeout=5,
                )
                if not ts_resp.json().get("success"):
                    app.logger.warning("satoshi_lead: captcha failed ip=%s",
                                       request.remote_addr)
                    return jsonify({"ok": False, "error": "captcha_failed"}), 400
            except Exception as exc:
                app.logger.error("satoshi_lead: turnstile error %s", exc)
        elif _RECAPTCHA_SECRET:
            return jsonify({"ok": False, "error": "captcha_required"}), 400
    # ────────────────────────────────────────────────────────────────────

    email = str(data.get("email", "")).strip().lower()
    if not _EMAIL_RE.match(email):
        return jsonify({"ok": False, "error": "invalid_email"}), 400
    source   = str(data.get("source", "satoshi_widget"))[:64]
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        app.logger.error("satoshi_lead: missing SUPABASE_URL/KEY env")
        return jsonify({"ok": False, "error": "server_error"}), 500
    try:
        headers = {
            "apikey": sb_key,
            "Authorization": f"Bearer {sb_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }
        payload = {"email": email, "source": source, "metadata": metadata}
        resp = requests.post(
            f"{sb_url}/rest/v1/leads",
            json=payload,
            headers=headers,
            timeout=8,
        )
        # 201 = inserted, 409 = duplicate (already captured) — both are ok
        if resp.status_code in (201, 204, 409):
            return jsonify({"ok": True})
        app.logger.error("satoshi_lead supabase error: %s", resp.status_code)
        return jsonify({"ok": False, "error": "server_error"}), 500
    except Exception as exc:
        app.logger.error("satoshi_lead error: %s", exc)
        return jsonify({"ok": False, "error": "server_error"}), 500


@app.route("/xgboost-spiegato", methods=["GET"])
def xgboost_spiegato():
    with open("xgboost.html", "r") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html"}


# ── News feed RSS cache (10 min) ─────────────────────────────────────────────
_news_cache: dict = {"data": None, "ts": 0.0}
_NEWS_FEEDS = [
    ("CoinDesk",        "https://www.coindesk.com/arc/outboundfeeds/rss/"),
    ("CoinTelegraph",   "https://cointelegraph.com/rss"),
    ("Bitcoin Magazine","https://bitcoinmagazine.com/feed"),
    ("Decrypt",         "https://decrypt.co/feed"),
]

@app.route("/news-feed", methods=["GET"])
def news_feed():
    """Aggrega RSS crypto news — cache 10 min, NO auth required."""
    import xml.etree.ElementTree as ET
    import email.utils

    global _news_cache
    now = time.time()
    if _news_cache["data"] is not None and now - _news_cache["ts"] < 600:
        return jsonify({"items": _news_cache["data"], "cached": True})

    items = []
    for source, url in _NEWS_FEEDS:
        try:
            resp = requests.get(url, timeout=6,
                                headers={"User-Agent": "BTCPredictor/1.0"})
            if not resp.ok:
                continue
            root = ET.fromstring(resp.content)
            for item in root.findall(".//item")[:5]:
                title   = (item.findtext("title") or "").strip()
                link    = (item.findtext("link") or "").strip()
                pub_raw = (item.findtext("pubDate") or "").strip()
                summary = (item.findtext("description") or "").strip()
                # strip HTML tags from summary
                summary = re.sub(r"<[^>]+>", "", summary)[:160].strip()
                # parse pubDate → ISO
                pub_iso = None
                try:
                    ts = email.utils.parsedate_to_datetime(pub_raw).isoformat()
                    pub_iso = ts
                except Exception:
                    pub_iso = pub_raw
                if title and link:
                    items.append({"title": title, "link": link,
                                  "pub": pub_iso, "source": source,
                                  "summary": summary})
        except Exception:
            continue

    # Sort by pub descending (ISO strings sort correctly), take top 15
    items.sort(key=lambda x: x.get("pub") or "", reverse=True)
    items = items[:15]
    _news_cache["data"] = items
    _news_cache["ts"]   = now
    return jsonify({"items": items, "cached": False})


@app.route("/on-chain-audit", methods=["GET"])
def on_chain_audit():
    """
    Proof Chain integrity — NO auth required.
    Usa select=* per compatibilità con RLS (stesso pattern di /signals).
    """
    import datetime as _dt

    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return jsonify({"error": "no_supabase"}), 500

    try:
        # Usa lo stesso pattern URL di /signals (select=* per evitare 401 su colonne)
        url = (f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
               f"?select=id,direction,confidence,correct,pnl_usd,created_at"
               f",onchain_commit_tx,onchain_resolve_tx"
               f",onchain_commit_hash,onchain_resolve_hash"
               f"&bet_taken=eq.true&order=id.desc&limit=30")
        r = requests.get(url, headers={
            "apikey": sb_key,
            "Authorization": f"Bearer {sb_key}",
        }, timeout=8)
        if not r.ok:
            # Fallback: query senza colonne on-chain per diagnostica
            url_fb = (f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                      f"?select=id,direction,confidence,correct,pnl_usd,created_at"
                      f"&bet_taken=eq.true&order=id.desc&limit=30")
            r_fb = requests.get(url_fb, headers={
                "apikey": sb_key, "Authorization": f"Bearer {sb_key}"
            }, timeout=8)
            if not r_fb.ok:
                return jsonify({"error": "supabase_error",
                                "status": r.status_code,
                                "fallback_status": r_fb.status_code}), 500
            bets = r_fb.json()
            onchain_cols = False
        else:
            bets = r.json()
            onchain_cols = True
    except Exception as e:
        app.logger.exception("Endpoint error")
        return jsonify({"error": "internal_error"}), 500

    total_bets  = len(bets)
    with_commit = sum(1 for b in bets if b.get("onchain_commit_tx"))
    with_resolve = sum(1 for b in bets if b.get("onchain_resolve_tx"))

    closed_bets = [b for b in bets if b.get("correct") is not None]
    closed_with_commit = sum(1 for b in closed_bets if b.get("onchain_commit_tx"))
    with_full_proof = sum(
        1 for b in closed_bets
        if b.get("onchain_commit_tx") and b.get("onchain_resolve_tx")
    )

    # Integrity score = full_proof / closed_bets_with_commit
    if closed_with_commit > 0:
        integrity_score = round(with_full_proof / closed_with_commit * 100, 1)
    elif closed_bets:
        integrity_score = 0.0
    else:
        integrity_score = None

    # Last commit timestamp
    last_commit_at = None
    for b in bets:
        if b.get("onchain_commit_tx"):
            last_commit_at = b.get("created_at")
            break

    entries = []
    for b in bets:
        has_commit  = bool(b.get("onchain_commit_tx"))
        has_resolve = bool(b.get("onchain_resolve_tx"))
        is_closed   = b.get("correct") is not None
        if is_closed and has_commit and has_resolve:
            status = "full_proof"
        elif not is_closed and has_commit:
            status = "pending_resolve"
        elif is_closed and has_commit and not has_resolve:
            status = "missing_resolve"
        else:
            status = "no_proof"
        entries.append({
            "id":           b["id"],
            "direction":    b.get("direction"),
            "confidence":   b.get("confidence"),
            "correct":      b.get("correct"),
            "pnl_usd":      b.get("pnl_usd"),
            "created_at":   b.get("created_at"),
            "commit_tx":    b.get("onchain_commit_tx"),
            "resolve_tx":   b.get("onchain_resolve_tx"),
            "commit_hash":  b.get("onchain_commit_hash"),
            "resolve_hash": b.get("onchain_resolve_hash"),
            "status":       status,
        })

    return jsonify({
        "contract":          "0xe4661F7dB62644951Eb1F9Fd23DB90e647833a55",
        "onchain_cols":      onchain_cols,
        "total_bets":        total_bets,
        "with_commit":       with_commit,
        "with_resolve":      with_resolve,
        "with_full_proof":   with_full_proof,
        "integrity_score":   integrity_score,
        "last_commit_at":    last_commit_at,
        "maintenance_mode":  total_bets == 0,
        "entries":           entries,
    })


@app.route("/marketing", methods=["GET"])
def marketing():
    with open("marketing.html", "r") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html"}


@app.route("/marketing-stats", methods=["GET"])
def marketing_stats():
    """Dati pubblici/marketing — NO auth required."""
    import datetime as _dt
    import re as _re

    result = {}

    # ── 1. Telegram member count ──────────────────────────────────
    # getChatMemberCount funziona con username pubblico @BTCPredictorBot
    # anche se il bot non è admin del canale (confermato 2026-02-28)
    try:
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if tg_token:
            r = requests.get(
                f"https://api.telegram.org/bot{tg_token}/getChatMemberCount"
                f"?chat_id=@BTCPredictorBot",
                timeout=5,
            )
            tg_data = r.json() if r.ok else {}
            if tg_data.get("ok"):
                result["telegram_members"] = tg_data.get("result")
            else:
                result["telegram_members"] = None
        else:
            result["telegram_members"] = None
    except Exception:
        result["telegram_members"] = None

    # ── 2. Wallet ────────────────────────────────────────────────
    # PolygonScan V1 deprecato (richiede API key) — link diretto, no API call
    wallet_addr = "0x7Ac896F18ce52a0520dA49C3129520f7B70d51f0"
    published_in_site = False
    try:
        with open(os.path.join(os.path.dirname(__file__), "index.html"), "r") as _f:
            published_in_site = wallet_addr[:12] in _f.read()
    except Exception:
        pass

    result["wallet"] = {
        "address":           wallet_addr,
        "published_in_site": published_in_site,
        "polygonscan_url":   f"https://polygonscan.com/address/{wallet_addr}",
    }

    # ── 3. SEO checks on index.html ──────────────────────────────
    seo = {"og_image": False, "meta_description": False, "json_ld": False, "canonical": False}
    try:
        with open(os.path.join(os.path.dirname(__file__), "index.html"), "r") as _f:
            idx = _f.read()
        seo["og_image"]         = 'og:image' in idx
        seo["meta_description"] = 'name="description"' in idx
        seo["json_ld"]          = 'application/ld+json' in idx
        seo["canonical"]        = 'rel="canonical"' in idx
    except Exception:
        pass
    result["seo"] = seo

    # ── 4. Last retrain ──────────────────────────────────────────
    last_retrain = None
    base        = os.path.dirname(__file__)
    report_path = os.path.join(base, "datasets", "xgb_report.txt")
    model_path  = os.path.join(base, "models", "xgb_direction.pkl")
    if os.path.exists(report_path):
        try:
            txt = open(report_path).read()
            m = _re.search(r"Generated:\s*(\d{4}-\d{2}-\d{2})", txt)
            if m:
                last_retrain = m.group(1)
        except Exception:
            pass
    if not last_retrain and os.path.exists(model_path):
        try:
            mtime = os.path.getmtime(model_path)
            last_retrain = _dt.datetime.utcfromtimestamp(mtime).strftime("%Y-%m-%d")
        except Exception:
            pass
    result["last_retrain"] = last_retrain

    # ── 5. Bet stats for retrain window ──────────────────────────
    try:
        sb_url, sb_key = _sb_config()
        _headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}", "Prefer": "count=exact"}
        r_clean = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}",
            headers=_headers,
            params={"bet_taken": "eq.true", "correct": "not.is.null", "select": "id", "limit": "0"},
            timeout=5,
        )
        clean_bets = int(r_clean.headers.get("content-range", "*/0").split("/")[-1])
        r_wins = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}",
            headers=_headers,
            params={"bet_taken": "eq.true", "correct": "eq.true", "select": "id", "limit": "0"},
            timeout=5,
        )
        wins = int(r_wins.headers.get("content-range", "*/0").split("/")[-1])
        wr = round(100.0 * wins / clean_bets, 1) if clean_bets > 0 else None
        result["bet_stats"] = {"clean_bets": clean_bets, "wins": wins, "win_rate_pct": wr}
    except Exception:
        result["bet_stats"] = {"clean_bets": 0, "wins": 0, "win_rate_pct": None}

    return jsonify(result)


@app.route("/privacy", methods=["GET"])
def privacy():
    with open("privacy.html", "r") as f:
        html = f.read()
    return html, 200, {"Content-Type": "text/html"}


_CACHE_BUST = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "1")[:8]

@app.route("/dashboard", methods=["GET"])
def dashboard():
    # Use the actual request host so API calls always go same-origin.
    # This avoids CORS issues and DNS propagation problems when accessed
    # via a custom domain (e.g. btcpredictor.io vs railway.app).
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    railway_url = f"{scheme}://{request.host}"
    with open("index.html", "r") as f:
        html = f.read()
    read_key = os.environ.get("READ_API_KEY", "")
    inject = f'<script>window.RAILWAY_URL = {json.dumps(railway_url)};window.READ_API_KEY = {json.dumps(read_key)};</script>'
    # Sostituisci placeholder cache_bust nel link CSS
    html = html.replace("__CACHE_BUST__", _CACHE_BUST)
    gv = ""
    if _GOOGLE_SITE_VERIFICATION:
        gv = f'<meta name="google-site-verification" content="{_GOOGLE_SITE_VERIFICATION}">\n'
    html = html.replace("</head>", gv + inject + "\n</head>", 1)
    return html, 200, {"Content-Type": "text/html"}

@app.errorhandler(404)
def page_not_found(e):
    with open("404.html", "r") as f:
        html = f.read()
    return html, 404, {"Content-Type": "text/html"}


# ── COCKPIT — Private Command Center ────────────────────────────────────────
# Secure dashboard for Mattia to monitor AI agents, bot status, and system health.
# Auth: stateless via X-Cockpit-Token header (no Flask sessions needed).
# Token set via COCKPIT_TOKEN env var (separate from BOT_API_KEY).

_COCKPIT_TOKEN = os.environ.get("COCKPIT_TOKEN", "")
_cockpit_rl = {}  # rate limiting for cockpit auth


def _check_cockpit_auth():
    """Verify cockpit token from header. Returns None if ok, error response if not."""
    if not _COCKPIT_TOKEN:
        return jsonify({"error": "cockpit_disabled", "msg": "COCKPIT_TOKEN not configured"}), 503
    token = request.headers.get("X-Cockpit-Token", "")
    if not token or not _hmac.compare_digest(token, _COCKPIT_TOKEN):
        return jsonify({"error": "forbidden"}), 403
    return None


@app.route("/cockpit", methods=["GET"])
def cockpit_page():
    """Serve the cockpit HTML dashboard."""
    if not _COCKPIT_TOKEN:
        return "Cockpit disabled (COCKPIT_TOKEN not set)", 503
    try:
        with open("cockpit.html", "r") as f:
            html = f.read()
        return html, 200, {"Content-Type": "text/html"}
    except FileNotFoundError:
        return "cockpit.html not found", 404


@app.route("/cockpit/api/auth", methods=["POST"])
def cockpit_auth():
    """Validate cockpit token. Rate limited: max 5 attempts per minute per IP."""
    ip = request.remote_addr or "unknown"
    now = time.time()
    # Rate limit cleanup
    _cockpit_rl[ip] = [t for t in _cockpit_rl.get(ip, []) if now - t < 60]
    if len(_cockpit_rl.get(ip, [])) >= 5:
        app.logger.warning("[COCKPIT] Auth rate limited for %s", ip)
        return jsonify({"error": "rate_limited"}), 429
    _cockpit_rl.setdefault(ip, []).append(now)

    data = request.get_json(force=True) or {}
    token = data.get("token", "")
    if not _COCKPIT_TOKEN:
        return jsonify({"error": "cockpit_disabled"}), 503
    if _hmac.compare_digest(token, _COCKPIT_TOKEN):
        app.logger.info("[COCKPIT] Auth success from %s", ip)
        return jsonify({"status": "ok"}), 200
    else:
        app.logger.warning("[COCKPIT] Auth failed from %s", ip)
        return jsonify({"error": "forbidden"}), 403


@app.route("/cockpit/api/agents", methods=["GET"])
def cockpit_agents():
    """Return AI agent states from Supabase cockpit_events table."""
    err = _check_cockpit_auth()
    if err:
        return err
    agents = []
    events = []
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
            # Fetch latest state per agent
            resp = requests.get(
                f"{sb_url}/rest/v1/cockpit_events?select=*&order=updated_at.desc&limit=50",
                headers=headers, timeout=5
            )
            if resp.ok:
                rows = resp.json()
                # Deduplicate: keep latest per clone_id
                seen = set()
                for row in rows:
                    cid = row.get("clone_id", "")
                    if cid and cid not in seen:
                        seen.add(cid)
                        agents.append({
                            "clone_id": cid,
                            "name": row.get("name", cid),
                            "role": row.get("role", ""),
                            "status": row.get("status", "pending"),
                            "model": row.get("model", ""),
                            "current_task": row.get("current_task", ""),
                            "last_message": row.get("last_message", ""),
                            "thought": row.get("thought", ""),
                            "cost_usd": float(row.get("cost_usd", 0)),
                            "max_budget": float(row.get("max_budget", 0)),
                            "elapsed_sec": float(row.get("elapsed_sec", 0)),
                            "tasks": json.loads(row.get("tasks_json", "[]")) if row.get("tasks_json") else [],
                            "next_action": row.get("next_action", ""),
                            "next_action_time": row.get("next_action_time", ""),
                            "result_summary": row.get("result_summary", ""),
                            "notes": row.get("notes", ""),
                            "priority": bool(row.get("priority", False)),
                        })
                    # All rows become events
                    events.append({
                        "timestamp": row.get("updated_at", ""),
                        "source": row.get("name", cid),
                        "message": row.get("last_message", ""),
                        "level": "error" if row.get("status") == "error" else (
                            "success" if row.get("status") == "done" else "info"
                        ),
                    })
    except Exception as e:
        app.logger.warning("[COCKPIT] Error fetching agents: %s", e)

    return jsonify({"agents": agents, "events": events}), 200


@app.route("/cockpit/api/overview", methods=["GET"])
def cockpit_overview():
    """Return system overview: bot status, positions, predictions, agent summary."""
    err = _check_cockpit_auth()
    if err:
        return err

    overview = {
        "bot_paused": bool(_BOT_PAUSED),
        "dry_run": os.environ.get("DRY_RUN", "").lower() == "true",
        "mode": "DRY RUN" if os.environ.get("DRY_RUN", "").lower() == "true" else (
            "PAUSED" if _BOT_PAUSED else "LIVE"
        ),
        "open_positions": 0,
        "position_detail": "",
        "today_predictions": 0,
        "predictions_detail": "",
        "win_rate": None,
        "winrate_detail": "",
        "agents_total_cost": 0.0,
        "agents_running": 0,
        "agents_total": 0,
        "cost_detail": "",
        "current_phase": "-",
        "phase_detail": "",
        "uptime": "-",
        # v2 fields
        "total_pnl": 0.0,
        "today_pnl": 0.0,
        "profit_factor": None,
        "wallet_equity": None,
        "capital_base": float(os.environ.get("CAPITAL_USD", 0)),
        "latest_onchain_tx": None,
        "today_ghosts": 0,
        "latest_prediction": None,
    }

    try:
        sb_url, sb_key = _sb_config()
        if not sb_url or not sb_key:
            return jsonify(overview), 200
        headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}

        # Open positions count
        try:
            from kraken.futures import User as _KUser
            _ku = _KUser(
                key=os.environ.get("KRAKEN_API_KEY", ""),
                secret=os.environ.get("KRAKEN_API_SECRET", "")
            )
            _positions = _ku.get_open_positions()
            if isinstance(_positions, dict):
                _positions = _positions.get("openPositions", [])
            overview["open_positions"] = len([p for p in (_positions or []) if float(p.get("size", 0)) > 0])
            if overview["open_positions"] > 0:
                p = _positions[0]
                overview["position_detail"] = f"{p.get('side', '?')} {p.get('symbol', '')} {p.get('size', '')}"
            # Wallet equity from Kraken flex account
            try:
                _wallets = _ku.get_wallets()
                if isinstance(_wallets, dict):
                    flex = _wallets.get("flex", _wallets.get("multiCollateral", {}))
                    overview["wallet_equity"] = float(flex.get("pv", flex.get("portfolioValue", 0)))
            except Exception:
                pass
        except Exception:
            overview["position_detail"] = "Kraken API unavailable"

        # Today's predictions (expanded select for pnl + ghost + latest)
        today_str = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        resp = requests.get(
            f"{sb_url}/rest/v1/predictions?select=id,correct,confidence,direction,pnl_usd,bet_taken,created_at,tx_hash"
            f"&created_at=gte.{today_str}T00:00:00Z&order=created_at.desc",
            headers=headers, timeout=5
        )
        if resp.ok:
            preds = resp.json()
            taken = [p for p in preds if p.get("bet_taken") is not False]
            ghosts = [p for p in preds if p.get("bet_taken") is False]
            overview["today_predictions"] = len(taken)
            correct = [p for p in taken if p.get("correct") is True]
            wrong = [p for p in taken if p.get("correct") is False]
            pending = [p for p in taken if p.get("correct") is None]
            overview["predictions_detail"] = f"{len(correct)}W {len(wrong)}L {len(pending)}P"
            # Today P&L
            overview["today_pnl"] = sum(float(p.get("pnl_usd") or 0) for p in taken)
            # Today ghosts count
            overview["today_ghosts"] = len(ghosts)
            # Latest prediction
            if taken:
                lp = taken[0]
                overview["latest_prediction"] = {
                    "direction": lp.get("direction"),
                    "confidence": lp.get("confidence"),
                    "correct": lp.get("correct"),
                    "created_at": lp.get("created_at"),
                }
            # Latest on-chain tx hash (first non-null tx_hash)
            for p in preds:
                if p.get("tx_hash"):
                    overview["latest_onchain_tx"] = p["tx_hash"]
                    break

        # Overall win rate (last 50 evaluated bets) + total P&L + profit factor
        resp2 = requests.get(
            f"{sb_url}/rest/v1/predictions?select=correct,pnl_usd"
            f"&correct=not.is.null&bet_taken=eq.true&order=created_at.desc&limit=50",
            headers=headers, timeout=5
        )
        if resp2.ok:
            evaluated = resp2.json()
            if evaluated:
                wins = sum(1 for p in evaluated if p.get("correct") is True)
                overview["win_rate"] = wins / len(evaluated)
                overview["winrate_detail"] = f"{wins}/{len(evaluated)} (last 50 evaluated)"
                # Total P&L and profit factor from evaluated
                pnls = [float(p.get("pnl_usd") or 0) for p in evaluated]
                overview["total_pnl"] = sum(pnls)
                gross_wins = sum(v for v in pnls if v > 0)
                gross_losses = abs(sum(v for v in pnls if v < 0))
                overview["profit_factor"] = round(gross_wins / gross_losses, 2) if gross_losses > 0 else None

        # Agent summary from cockpit_events
        try:
            resp3 = requests.get(
                f"{sb_url}/rest/v1/cockpit_events?select=clone_id,status,cost_usd,phase"
                f"&order=updated_at.desc&limit=20",
                headers=headers, timeout=5
            )
            if resp3.ok:
                agent_rows = resp3.json()
                seen = {}
                for r in agent_rows:
                    cid = r.get("clone_id", "")
                    if cid and cid not in seen:
                        seen[cid] = r
                overview["agents_total"] = len(seen)
                overview["agents_running"] = sum(1 for r in seen.values() if r.get("status") == "running")
                overview["agents_total_cost"] = sum(float(r.get("cost_usd", 0)) for r in seen.values())
                budget = 42.0  # from orchestration plan
                overview["cost_detail"] = f"${overview['agents_total_cost']:.2f} / ${budget:.2f}"
                # Phase detection
                phases = set(r.get("phase", "") for r in seen.values() if r.get("status") == "running")
                if "B" in phases:
                    overview["current_phase"] = "B"
                    overview["phase_detail"] = "Write-heavy (C1, C2)"
                elif "A" in phases:
                    overview["current_phase"] = "A"
                    overview["phase_detail"] = "Read-heavy (C3-C6)"
                elif all(r.get("status") == "done" for r in seen.values()):
                    overview["current_phase"] = "C"
                    overview["phase_detail"] = "Merge & Integration"
                else:
                    overview["current_phase"] = "-"
                    overview["phase_detail"] = "Idle"
        except Exception:
            pass

    except Exception as e:
        app.logger.warning("[COCKPIT] Overview error: %s", e)

    return jsonify(overview), 200


@app.route("/cockpit/api/bot-toggle", methods=["POST"])
def cockpit_bot_toggle():
    """Toggle bot paused state. No body needed — it's a toggle."""
    err = _check_cockpit_auth()
    if err:
        return err
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT
    _refresh_bot_paused()
    _BOT_PAUSED = not _BOT_PAUSED
    _BOT_PAUSED_REFRESHED_AT = time.time()
    _save_bot_paused(_BOT_PAUSED)
    app.logger.info("[COCKPIT] Bot toggled → paused=%s", _BOT_PAUSED)
    return jsonify({"paused": _BOT_PAUSED}), 200


def _valid_clone_id(cid):
    """Whitelist clone_id to prevent query injection via Supabase REST URL."""
    return bool(cid and re.fullmatch(r'c[1-6]', cid))


@app.route("/cockpit/api/agents/reset", methods=["POST"])
def cockpit_agents_reset():
    """Reset one or all agents in cockpit_events to pending state."""
    err = _check_cockpit_auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    clone_id = data.get("clone_id")
    if clone_id and not _valid_clone_id(clone_id):
        return jsonify({"error": "invalid_clone_id"}), 400
    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return jsonify({"error": "supabase_not_configured"}), 503

    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    patch_body = {
        "status": "pending",
        "last_message": "",
        "result_summary": "",
        "cost_usd": 0,
        "elapsed_sec": 0,
        "notes": "",
        "priority": False,
    }
    url = f"{sb_url}/rest/v1/cockpit_events"
    if clone_id:
        url += f"?clone_id=eq.{clone_id}"
    else:
        url += "?clone_id=neq.___"  # match all rows

    try:
        resp = requests.patch(url, json=patch_body, headers=headers, timeout=5)
        if not resp.ok:
            return jsonify({"error": "supabase_error", "detail": resp.text}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    reset_ids = [clone_id] if clone_id else ["c1", "c2", "c3", "c4", "c5", "c6"]
    app.logger.info("[COCKPIT] Agents reset: %s", reset_ids)
    return jsonify({"reset": reset_ids}), 200


@app.route("/cockpit/api/agents/update", methods=["POST"])
def cockpit_agents_update():
    """Update agent note or priority flag."""
    err = _check_cockpit_auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    clone_id = data.get("clone_id")
    action = data.get("action")
    value = data.get("value")

    if not clone_id or not _valid_clone_id(clone_id) or action not in ("note", "priority"):
        return jsonify({"error": "invalid_params", "msg": "valid clone_id (c1-c6) + action(note|priority) required"}), 400

    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return jsonify({"error": "supabase_not_configured"}), 503

    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    if action == "note":
        patch_body = {"notes": str(value or "")[:500]}
    else:
        patch_body = {"priority": bool(value)}

    try:
        resp = requests.patch(
            f"{sb_url}/rest/v1/cockpit_events?clone_id=eq.{clone_id}",
            json=patch_body, headers=headers, timeout=5,
        )
        if not resp.ok:
            return jsonify({"error": "supabase_error", "detail": resp.text}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"ok": True}), 200


@app.route("/cockpit/api/ghosts", methods=["GET"])
def cockpit_ghosts():
    """Return last 10 ghost (skipped) signals for Ghost Mode."""
    err = _check_cockpit_auth()
    if err:
        return err
    ghosts = []
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
            resp = requests.get(
                f"{sb_url}/rest/v1/predictions"
                f"?select=id,direction,confidence,reason,created_at,ghost_correct,signal_price"
                f"&bet_taken=eq.false&order=created_at.desc&limit=10",
                headers=headers, timeout=5,
            )
            if resp.ok:
                ghosts = resp.json()
    except Exception as e:
        app.logger.warning("[COCKPIT] Ghosts error: %s", e)
    return jsonify({"ghosts": ghosts}), 200


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
