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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import sentry_sdk
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, redirect
from flask_compress import Compress
from kraken.futures import Trade, User
from constants import TAKER_FEE, _BIAS_MAP
from portfolio_engine import PortfolioEngine, PortfolioDecision
import council_engine

VERSION = "2.6.2"


# ── Resilient HTTP Sessions (retry + connection pooling) ──────────────────────
# Root cause fix: every zombie bet in history traced back to a single HTTP call
# failing without retry.  These sessions add 3-attempt exponential backoff on
# transient errors (502/503/504) and reuse TCP connections (TLS handshake once).

def _build_session(retries=3, backoff=0.4, status_forcelist=(429, 502, 503, 504)):
    """Create a requests.Session with retry + connection pooling."""
    s = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=status_forcelist,
        allowed_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(pool_connections=5, pool_maxsize=20, max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

_sb_session = _build_session()      # Supabase REST API
_kraken_session = _build_session()   # Kraken futures API
_tg_session = _build_session()       # Telegram Bot API
_n8n_session = _build_session()      # n8n webhooks
_ext_session = _build_session()      # external APIs (alternative.me, Google, etc.)

sentry_sdk.init(
    dsn=os.environ.get("SENTRY_DSN", ""),
    traces_sample_rate=0.0,   # solo error monitoring, no performance tracing
    send_default_pii=False,
)

app = Flask(__name__)
Compress(app)  # gzip all responses >500 bytes — cuts dashboard from 411KB to ~80KB


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
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com "
        "https://fonts.googleapis.com "
        "https://js-de.sentry-cdn.com https://www.googletagmanager.com https://www.clarity.ms "
        "https://www.google.com/recaptcha/ https://www.gstatic.com/recaptcha/ "
        "https://challenges.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: https:; "
        "frame-src https://www.google.com/recaptcha/ https://challenges.cloudflare.com; "
        "connect-src 'self' https://*.railway.app https://oimlamjilivrcnhztwvj.supabase.co "
        "https://api.binance.com "
        "https://sentry.io https://*.sentry-cdn.com https://www.clarity.ms "
        "https://www.google-analytics.com https://n8n.srv1432354.hstgr.cloud;"
    )
    return response


_PAGES_DIR = os.path.join(os.path.dirname(__file__), "pages")


def _read_page(filename):
    """Read an HTML page from the pages/ directory."""
    path = os.path.join(_PAGES_DIR, filename)
    if not os.path.isfile(path):
        return f"<h1>404</h1><p>Page not found: {filename}</p>"
    with open(path, "r") as f:
        return f.read()


def _sb_config() -> tuple:
    """Return (supabase_url, supabase_key). Single source of truth for Supabase env vars."""
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")
    return url, key


# ── Cockpit Log Helper ──────────────────────────────────────────────────────
# Fire-and-forget: never blocks the caller, never raises.
_LOG_VALID_LEVELS = {"info", "success", "warning", "error", "critical"}

def _push_cockpit_log(source: str, level: str, title: str, message: str = "", metadata=None):
    """Insert a row into cockpit_log. Truly non-blocking via daemon thread."""
    if level not in _LOG_VALID_LEVELS:
        level = "info"
    def _do_push():
        try:
            sb_url, sb_key = _sb_config()
            if not sb_url or not sb_key:
                return
            _sb_session.post(
                f"{sb_url}/rest/v1/cockpit_log",
                json={
                    "source": source[:50],
                    "level": level,
                    "title": title[:120],
                    "message": message[:2000],
                    "metadata": json.dumps(metadata or {}),
                },
                headers={
                    "apikey": sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                timeout=3,
            )
        except Exception:
            pass  # never block the caller
    threading.Thread(target=_do_push, daemon=True).start()


# ── Rate limiting (S-20) ──────────────────────────────────────────────────────
_RATE_STORE: dict = {}  # key → (count, window_start)
_RATE_LOCK = threading.Lock()  # thread-safe per Gunicorn multi-worker (stesso processo)
_RL_WINDOW = 60         # secondi per finestra
_RL_MAX_DEFAULT = 100   # chiamate per finestra default
_XGB_GATE_MIN_BETS = int(os.environ.get("XGB_MIN_BETS", "100"))  # bet pulite necessarie per attivare il gate


def _check_rate_limit(key: str, max_calls: int = _RL_MAX_DEFAULT) -> bool:
    """Return True if call is allowed, False if rate-limited.
    Uses sliding windows of _RL_WINDOW seconds with automatic cleanup.
    Thread-safe via _RATE_LOCK (Gunicorn threaded mode).
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


# ── PnL calculation — single source of truth ──────────────────────────────────
# Previously duplicated in 4 locations (close_position, rescue_orphaned,
# place_bet reverse, backfill_bet).  Now unified here.

def _calculate_pnl(entry_price: float, exit_price: float, bet_size: float,
                   direction: str, fee_rate: float = TAKER_FEE,
                   funding_fee: float = 0.0) -> dict:
    """Calculate PnL for a closed position.

    Returns dict with keys:
        pnl_gross (float): raw directional PnL before fees
        fee_usd   (float): total taker fee (entry + exit)
        pnl_net   (float): net PnL after fees + funding
        pnl_pct   (float): percentage move in trade direction
        correct   (bool):  True if gross PnL > 0
        actual_direction (str): "UP" if exit > entry, else "DOWN"
    """
    if direction == "UP":
        pnl_gross = (exit_price - entry_price) * bet_size
    else:
        pnl_gross = (entry_price - exit_price) * bet_size

    fee_usd = bet_size * (entry_price + exit_price) * fee_rate
    pnl_net = round(pnl_gross - fee_usd + funding_fee, 6)

    if entry_price > 0:
        if direction == "UP":
            pnl_pct = round((exit_price - entry_price) / entry_price * 100, 4)
        else:
            pnl_pct = round((entry_price - exit_price) / entry_price * 100, 4)
    else:
        pnl_pct = 0.0

    return {
        "pnl_gross": pnl_gross,
        "fee_usd": round(fee_usd, 6),
        "pnl_net": pnl_net,
        "pnl_pct": pnl_pct,
        "correct": pnl_gross > 0,
        "actual_direction": "UP" if exit_price > entry_price else "DOWN",
    }


# ── Input validation helpers (H-2, H-4) ──────────────────────────────────────

def _safe_float(val, default: float, min_v: float | None = None, max_v: float | None = None) -> float:
    """Convert val to float with NaN, Infinity and out-of-range protection.
    Returns default if conversion fails or value is out of range.
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
    """Convert val to int with clamp and fallback. Never raises ValueError on the caller."""
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

_model_lock = threading.Lock()

def _load_xgb_model():
    global _XGB_MODEL
    model_path = os.path.join(os.path.dirname(__file__), "models", "xgb_direction.pkl")
    if os.path.exists(model_path):
        temp = joblib_load(model_path)
        from xgboost import XGBClassifier
        assert isinstance(temp, XGBClassifier), "Direction model type mismatch"
        with _model_lock:
            _XGB_MODEL = temp
        print(f"[XGB] Model loaded from {model_path}")
    else:
        print(f"[XGB] Model not found at {model_path} — /predict-xgb will return agree=True")

_load_xgb_model()

# Regime labels (per /btc-regime e logging)
_REGIME_LABELS = {0: "RANGING", 1: "TRENDING", 2: "VOLATILE"}

def _compute_regime_4h_live() -> dict:
    """
    Calcola il regime di mercato BTC corrente da klines 4h.
    Primary: Kraken Spot OHLC (no georestriction).
    Fallback: Binance (may return 451 from Railway).

    Returns dict con keys:
        regime_label (int): 0=RANGING, 1=TRENDING, 2=VOLATILE
        regime_name  (str): "RANGING" | "TRENDING" | "VOLATILE"
        atr_4h_pct   (float): ATR(14) normalizzato sul prezzo (%)
        trend_strength (float): |EMA5-EMA20|/EMA20 × 100 (%)
        trend_direction (str): "UP" | "DOWN"
        source (str): "kraken" | "binance"
    """
    _ERR_RESULT = {"regime_label": 0, "regime_name": "RANGING", "atr_4h_pct": 0.0, "trend_strength": 0.0, "trend_direction": "UP"}

    def _parse_ohlc(closes, highs, lows, source):
        """Common ATR/EMA calculation from OHLC arrays."""
        if len(closes) < 16:
            return {**_ERR_RESULT, "error": "insufficient_data", "source": source}

        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i]  - closes[i - 1]),
            )
            trs.append(tr)
        atr14 = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / max(len(trs), 1)
        atr_4h_pct = round((atr14 / closes[-1]) * 100.0, 4) if closes[-1] > 0 else 0.0

        def _ema(vals, period):
            k = 2.0 / (period + 1)
            e = vals[0]
            for v in vals[1:]:
                e = v * k + e * (1.0 - k)
            return e

        ema5  = _ema(closes[-5:],  5)  if len(closes) >= 5  else closes[-1]
        ema20 = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
        trend_strength = round(abs(ema5 - ema20) / ema20 * 100.0, 4) if ema20 > 0 else 0.0

        if trend_strength > 0.5:
            label = 1  # TRENDING
        elif atr_4h_pct > 1.5:
            label = 2  # VOLATILE
        else:
            label = 0  # RANGING

        return {
            "regime_label":   label,
            "regime_name":    _REGIME_LABELS[label],
            "atr_4h_pct":     atr_4h_pct,
            "trend_strength": trend_strength,
            "trend_direction": "UP" if ema5 > ema20 else "DOWN",
            "source":         source,
        }

    import urllib.request as _ureq
    import urllib.parse as _uparse
    import ssl as _ssl
    import json as _json
    import certifi as _certifi
    _ctx = _ssl.create_default_context(cafile=_certifi.where())

    # ── Primary: Kraken Spot OHLC (no georestriction from Railway) ────────
    try:
        params = _uparse.urlencode({"pair": "XBTUSD", "interval": 240})
        url = f"https://api.kraken.com/0/public/OHLC?{params}"
        req = _ureq.Request(url, headers={"User-Agent": "btcbot/1.0"})
        with _ureq.urlopen(req, context=_ctx, timeout=8) as resp:
            data = _json.loads(resp.read().decode())

        errors = data.get("error", [])
        if errors:
            raise ValueError(f"Kraken OHLC errors: {errors}")

        result = data.get("result", {})
        # Kraken returns {pair_name: [[time, open, high, low, close, vwap, volume, count], ...], "last": ...}
        pair_key = next((k for k in result if k != "last"), None)
        if not pair_key:
            raise ValueError("No pair data in Kraken OHLC response")

        candles = result[pair_key]
        # Take last 22 candles (Kraken returns up to 720)
        candles = candles[-22:]
        closes = [float(c[4]) for c in candles]
        highs  = [float(c[2]) for c in candles]
        lows   = [float(c[3]) for c in candles]

        regime = _parse_ohlc(closes, highs, lows, "kraken")
        if "error" not in regime:
            return regime
    except Exception as e:
        app.logger.warning("_compute_regime_4h_live Kraken failed: %s", e)

    # ── Fallback: Binance (may 451 from Railway geo) ──────────────────────
    try:
        params = _uparse.urlencode({"symbol": "BTCUSDT", "interval": "4h", "limit": 22})
        url = f"https://api.binance.com/api/v3/klines?{params}"
        req = _ureq.Request(url, headers={"User-Agent": "btcbot/1.0"})
        with _ureq.urlopen(req, context=_ctx, timeout=8) as resp:
            klines = _json.loads(resp.read().decode())

        closes = [float(k[4]) for k in klines]
        highs  = [float(k[2]) for k in klines]
        lows   = [float(k[3]) for k in klines]

        return _parse_ohlc(closes, highs, lows, "binance")
    except Exception as e2:
        app.logger.warning("_compute_regime_4h_live Binance fallback also failed: %s", e2)

    return {**_ERR_RESULT, "error": "all_sources_failed"}


def _compute_micro_regime_1h() -> dict:
    """
    Compute microtrend direction on 1H timeframe.
    Used to penalize counter-microtrend signals (bounces in 4H downtrend).
    Returns: { "micro_dir": "UP"|"DOWN", "micro_strength": float, "error": str|None }
    """
    import urllib.request as _m_ureq
    import urllib.parse as _m_uparse
    import ssl as _m_ssl
    import json as _m_json
    import certifi as _m_certifi
    _m_ctx = _m_ssl.create_default_context(cafile=_m_certifi.where())

    _ERR = {"micro_dir": "UNKNOWN", "micro_strength": 0.0, "error": "fetch_failed"}
    try:
        # Kraken 1H klines
        params = _m_uparse.urlencode({"pair": "XBTUSD", "interval": 60, "since": 0})
        url = f"https://api.kraken.com/0/public/OHLC?{params}"
        req = _m_ureq.Request(url, headers={"User-Agent": "btcbot/1.0"})
        with _m_ureq.urlopen(req, context=_m_ctx, timeout=8) as resp:
            data = _m_json.loads(resp.read().decode())
        candles = (data.get("result", {}).get("XXBTZUSD")
                   or data.get("result", {}).get("XBTUSD") or [])
        if len(candles) < 20:
            raise ValueError("not enough 1H candles")
        closes = [float(c[4]) for c in candles[-22:]]
    except Exception:
        try:
            params = _m_uparse.urlencode({"symbol": "BTCUSDT", "interval": "1h", "limit": 22})
            url = f"https://api.binance.com/api/v3/klines?{params}"
            req = _m_ureq.Request(url, headers={"User-Agent": "btcbot/1.0"})
            with _m_ureq.urlopen(req, context=_m_ctx, timeout=8) as resp:
                klines = _m_json.loads(resp.read().decode())
            closes = [float(k[4]) for k in klines]
        except Exception:
            return _ERR

    def _ema(vals, p):
        k = 2 / (p + 1)
        e = vals[0]
        for v in vals[1:]:
            e = v * k + e * (1 - k)
        return e

    ema5  = _ema(closes[-5:],  5)  if len(closes) >= 5  else closes[-1]
    ema20 = _ema(closes[-20:], 20) if len(closes) >= 20 else closes[-1]
    strength = abs(ema5 - ema20) / ema20 * 100.0 if ema20 > 0 else 0.0
    direction = "UP" if ema5 > ema20 else "DOWN"
    return {"micro_dir": direction, "micro_strength": round(strength, 4), "error": None}


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

# ── Adaptive Calibration Engine (ACE) ────────────────────────────────────────
from adaptive_engine import AdaptiveEngine
_sb_url_ace, _sb_key_ace = (
    os.environ.get("SUPABASE_URL", "").rstrip("/"),
    os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", ""),
)
_adaptive_engine = AdaptiveEngine(
    sb_url=_sb_url_ace, sb_key=_sb_key_ace,
    table=os.environ.get("SUPABASE_TABLE", "btc_predictions"),
)
# Initial calculation (background, non-blocking)
def _ace_initial_calc():
    try:
        _adaptive_engine.recalculate(trigger="startup")
    except Exception:
        pass
threading.Thread(target=_ace_initial_calc, daemon=True).start()
print(f"[ACE] Adaptive engine initialized (disabled={_adaptive_engine.disabled})")

_portfolio_engine = PortfolioEngine()
print(f"[PE] Portfolio engine initialized (disabled={_portfolio_engine.disabled})")

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
_CALIBRATION_LOCK = threading.Lock()

def get_calibrated_wr(conf):
    with _CALIBRATION_LOCK:
        cal = dict(CONF_CALIBRATION)
    for (lo, hi), wr in cal.items():
        if lo <= conf < hi:
            return wr
    return 0.50

# ── Auto-calibration: ore morti (aggiornato da /reload-calibration) ───────────
# Calibrazione 2026-02-28 su 650 segnali pre-Day0 (xgb_report.txt hourly WR):
#   5h 42.3% | 7h 42.1% | 10h 44.0% | 11h 42.9% | 17h 43.6% | 19h 44.4%
# (soglia live: n>=8 && WR<45%; 11h ha 7 bet — incluso come prior di calibrazione)
# Post-Day0: refresh_dead_hours() non trova dati → usa questi come fallback.
DEAD_HOURS_UTC: set = {5, 7, 10, 11, 17, 19}
_DEAD_HOURS_LOCK = threading.Lock()

def refresh_calibration():
    """Refresh CONF_CALIBRATION from real Supabase WR per confidence bucket."""
    global CONF_CALIBRATION
    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return {"ok": False, "error": "no_supabase_env"}
    try:
        r = _sb_session.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=confidence,correct&bet_taken=eq.true&correct=not.is.null"
            "&close_reason=neq.data_gap",
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
        with _CALIBRATION_LOCK:
            CONF_CALIBRATION = new_cal
        print(f"[CAL] Calibration updated: {stats}")
        return {"ok": True, "stats": stats, "total_rows": len(rows)}
    except Exception as e:
        app.logger.exception("Calibration refresh error")
        return {"ok": False, "error": "refresh_error"}

def refresh_dead_hours():
    """Refresh DEAD_HOURS_UTC: hours with WR < 45% and at least 8 bets. Hour extracted from created_at."""
    global DEAD_HOURS_UTC
    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return {"ok": False, "error": "no_supabase_env"}
    try:
        r = _sb_session.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=created_at,correct&bet_taken=eq.true&correct=not.is.null"
            "&close_reason=neq.data_gap",
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
            if len(vals) >= 5 and wr < 0.35:
                dead.add(h)
        # fallback: se non ci sono ore con n>=5 e WR<35%, usa prior da calibrazione storica
        with _DEAD_HOURS_LOCK:
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
SLIPPAGE_MAX_PCT = _safe_float(
    os.environ.get("SLIPPAGE_MAX_PCT", "0.005"),
    default=0.005, min_v=0.0, max_v=0.1
)
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "btc_predictions")
_ALLOWED_TABLES = {"btc_predictions", "sandbox_btc_predictions"}
if SUPABASE_TABLE not in _ALLOWED_TABLES:
    raise ValueError(f"SUPABASE_TABLE '{SUPABASE_TABLE}' not in whitelist {_ALLOWED_TABLES}")
_PAUSE_LOCK = threading.Lock()
_BOT_PAUSED = True               # fail-safe default: paused until Supabase confirms otherwise
_BOT_PAUSED_REFRESHED_AT = 0.0  # timestamp of last Supabase read (0.0 → forces refresh on first call)
_RESUMED_AT = ""                 # ISO timestamp of last /resume — circuit breaker ignores bets before this
_CB_TRIPPED_AT = 0.0             # timestamp of last circuit-breaker auto-pause (0.0 = never)
_CB_COOLDOWN_SEC = 1800          # 30 min minimum wait before manual resume after circuit breaker trip
_costs_cache = {"data": None, "ts": 0.0}
_CACHE_LOCK = threading.Lock()  # protects _costs_cache, _public_stats_cache, _macro_cache

# [FIX3] Trade cooldown — prevent over-trading (31 trades in 3h = fee drag)
_TRADE_LOCK = threading.Lock()
_LAST_TRADE_PLACED_AT = 0.0
TRADE_COOLDOWN_MINUTES = _safe_float(os.environ.get("TRADE_COOLDOWN_MINUTES", "30"), default=30.0, min_v=0.0, max_v=1440.0)

# [FIX4] Minimum ATR filter — skip low-volatility signals below breakeven threshold
MIN_ATR_PCT = _safe_float(os.environ.get("MIN_ATR_PCT", "0.15"), default=0.15, min_v=0.0, max_v=5.0)

# [FIX5] Trend alignment filter — block counter-trend trades in trending regimes
TREND_ALIGN_FILTER = os.environ.get("TREND_ALIGN_FILTER", "true").lower() == "true"

# AI Council mode (Fase 2) — TECNICO + SENTIMENT + QUANT vote before each trade
COUNCIL_MODE = os.environ.get("COUNCIL_MODE", "false").lower() == "true"

# Council status cache — Thoth Protocol (sess.166)
# Stores last deliberation result for GET /council-status (read-only, no auth)
_COUNCIL_LOCK = threading.Lock()
_COUNCIL_DELIBERATING = False
_COUNCIL_LAST: dict = {}       # populated after every run_round1()
_COUNCIL_HISTORY: list = []    # ring buffer, last 9 deliberations

# [FIX2] Prediction horizon (env-driven, used by /config and ATR scaling)
PREDICTION_HORIZON_MINUTES = _safe_float(os.environ.get("PREDICTION_HORIZON_MINUTES", "30"), default=30.0, min_v=1.0, max_v=1440.0)

# Startup security validation (H-3)
if not os.environ.get("BOT_API_KEY"):
    app.logger.warning("[SECURITY] BOT_API_KEY not set — all protected endpoints are unauthenticated!")
    sentry_sdk.capture_message("SECURITY: BOT_API_KEY missing at startup", level="warning")

def _refresh_bot_paused():
    """Read paused state from Supabase bot_state. Called on restart and every 5 min.
    Fail-safe: _BOT_PAUSED defaults True at boot; only set False when Supabase confirms.
    """
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT
    try:
        sb_url, sb_key = _sb_config()
        if not sb_url or not sb_key:
            return
        r = _sb_session.get(
            f"{sb_url}/rest/v1/bot_state?key=eq.paused&select=value",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            timeout=3,
        )
        if r.ok:
            data = r.json()
            with _PAUSE_LOCK:
                if data:
                    _BOT_PAUSED = data[0].get("value", "false").lower() in ("true", "1")
                else:
                    _BOT_PAUSED = False
                _BOT_PAUSED_REFRESHED_AT = time.time()
    except Exception:
        pass


def _save_resumed_at(iso_ts: str):
    """Persist resumed_at timestamp to Supabase bot_state (upsert)."""
    try:
        sb_url, sb_key = _sb_config()
        if not sb_url or not sb_key:
            return
        _sb_session.post(
            f"{sb_url}/rest/v1/bot_state",
            json={"key": "resumed_at", "value": iso_ts},
            headers={
                "apikey": sb_key,
                "Authorization": f"Bearer {sb_key}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal,resolution=merge-duplicates",
            },
            timeout=3,
        )
    except Exception as e:
        app.logger.error(f"[SAVE_RESUMED_AT] failed: {e}")


def _load_resumed_at() -> str:
    """Load resumed_at from Supabase bot_state. Returns empty string if not found."""
    try:
        sb_url, sb_key = _sb_config()
        if not sb_url or not sb_key:
            return ""
        r = _sb_session.get(
            f"{sb_url}/rest/v1/bot_state?key=eq.resumed_at&select=value",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            timeout=3,
        )
        if r.ok:
            data = r.json()
            if data:
                return data[0].get("value", "")
    except Exception:
        pass
    return ""


# Refresh calibration all'avvio — each step independent so one failure doesn't skip the rest
def _boot_load_resumed_at():
    global _RESUMED_AT
    _RESUMED_AT = _load_resumed_at()
    if _RESUMED_AT:
        app.logger.info(f"[BOOT] Restored _RESUMED_AT={_RESUMED_AT}")

for _boot_fn in [
    lambda: refresh_calibration(),
    lambda: refresh_dead_hours(),
    lambda: _refresh_bot_paused(),
    lambda: _boot_load_resumed_at(),
    lambda: _push_cockpit_log("app", "success", "Bot STARTED",
                               f"v{VERSION} boot complete — paused={_BOT_PAUSED}, resumed_at={_RESUMED_AT}"),
]:
    try:
        _boot_fn()
    except Exception as _boot_err:
        app.logger.warning(f"[BOOT] step failed: {_boot_err}")


def _save_bot_paused(paused: bool):
    """Persist paused state to Supabase bot_state."""
    try:
        sb_url, sb_key = _sb_config()
        if not sb_url or not sb_key:
            return
        r = _sb_session.patch(
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
        if not r.ok:
            app.logger.error(f"[SAVE_PAUSED] Supabase returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        app.logger.error(f"[SAVE_PAUSED] failed: {e}")


def _check_api_key():
    """Verify X-API-Key header with timing-safe compare (hmac.compare_digest).
    If BOT_API_KEY not configured, log warning and pass (backwards compat).
    """
    bot_key = os.environ.get("BOT_API_KEY", "")
    if not bot_key:
        return jsonify({"error": "Server misconfigured: API key not set"}), 503
    req_key = request.headers.get("X-API-Key", "")
    if not _hmac.compare_digest(req_key.encode(), bot_key.encode()):
        return jsonify({"error": "Unauthorized"}), 401
    return None


def _check_read_key():
    """Auth per endpoint read-only (signals, account-summary, equity-history, risk-metrics).
    Accetta READ_API_KEY (iniettato nel dashboard) oppure BOT_API_KEY (n8n/interni).
    Se READ_API_KEY non configurata → 503 (fail-closed, non fail-open).
    """
    read_key = os.environ.get("READ_API_KEY", "")
    if not read_key:
        return jsonify({"error": "Server misconfigured: READ_API_KEY not set"}), 503
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
    if hour_bucket is None:
        hour_bucket = int(time.time()) // 3600
    bot_key = os.environ.get("BOT_API_KEY", "anonymous")
    raw = f"{contrib_id}:{action}:{hour_bucket}"
    return _hmac.new(bot_key.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]


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
        r = _kraken_session.get(KRAKEN_BASE + "/derivatives/api/v3/servertime", timeout=5)
        return r.json().get("serverTime")
    except Exception:
        return None

def get_open_position(symbol: str):
    """
    Read position via SDK (auth=True) => no manual signing.
    Returns:
      None if flat
      { "side": "long"/"short", "size": float, "price": float } if open
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
    want_open=True  -> wait for a position to appear
    want_open=False -> wait for it to disappear (flat)
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
    """Return current mark price from Kraken Futures. 0.0 on failure."""
    try:
        trade = get_trade_client()
        result = trade.request(method="GET", uri="/derivatives/api/v3/tickers", auth=False)
        tickers = result.get("tickers", []) or []
        ticker = next((t for t in tickers if (t.get("symbol") or "").upper() == symbol.upper()), None)
        return float(ticker.get("markPrice") or 0) if ticker else 0.0
    except Exception:
        return 0.0


def _get_funding_fee() -> float:
    """Read unrealizedFunding from Kraken flex account.
    Negative = paid, positive = received. Returns 0.0 on failure (fail-open)."""
    try:
        user = get_user_client()
        flex = user.get_wallets().get("accounts", {}).get("flex", {})
        return float(flex.get("unrealizedFunding") or 0)
    except Exception:
        return 0.0


def _close_prev_bet_on_reverse(old_side: str, exit_price: float, closed_size: float,
                               funding_fee: float = 0.0):
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
        resp = _sb_session.get(query, headers=headers, timeout=5)
        rows = resp.json() if resp.ok else []
        if not rows:
            return

        row = rows[0]
        bet_id = row["id"]
        entry_price = float(row.get("entry_fill_price") or row.get("btc_price_entry") or exit_price)
        bet_size = float(row.get("bet_size") or closed_size or 0.0005)

        _pnl = _calculate_pnl(entry_price, exit_price, bet_size, old_direction, funding_fee=funding_fee)

        # &correct=is.null → atomicità ottimistica: nessuna doppia risoluzione (S-17)
        patch_url = f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}&correct=is.null"
        patch_headers = {**headers, "Content-Type": "application/json", "Prefer": "return=minimal"}
        patch_data = {
            "btc_price_exit":     exit_price,
            "exit_fill_price":    exit_price,
            "correct":            _pnl["correct"],
            "pnl_usd":            _pnl["pnl_net"],
            "pnl_pct":            _pnl["pnl_pct"],
            "close_reason":       "closed_by_reverse_bet",
            "has_real_exit_fill": False,
            "source_updated_by":  "place_bet_reverse",
        }
        if funding_fee != 0.0:
            patch_data["funding_fee"] = round(funding_fee, 6)
        _sb_session.patch(patch_url, json=patch_data, headers=patch_headers, timeout=5)
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
        me = flex.get("marginEquity")
        if me is None:
            me = flex.get("pv") or flex.get("portfolioValue")
        if me is not None:
            wallet_equity = float(me)
    except Exception:
        app.logger.debug("[health] wallet equity fetch failed", exc_info=True)

    # base_size — from bet-sizing logic (last 10 trades, default conf 0.62)
    base_size = 0.002
    supabase_ok = None
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            r = _sb_session.get(
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
        "version": VERSION,
        "dry_run": DRY_RUN,
        "supabase_table": SUPABASE_TABLE,
        "paused": _BOT_PAUSED,
        "bot_paused": bool(_BOT_PAUSED),
        "capital": capital,
        "wallet_equity": wallet_equity,
        "base_size": base_size,
        "confidence_threshold": float(os.environ.get("CONF_THRESHOLD", "0.62")),
        "xgb_gate_active": _clean_bets >= _XGB_GATE_MIN_BETS,
        "xgb_clean_bets": _clean_bets,
        "xgb_min_bets": _XGB_GATE_MIN_BETS,
        "polygon_configured": _polygon_configured,
        "supabase_ok": supabase_ok,
        "council_mode_active": COUNCIL_MODE,
    })


# ── CONFIG (FIX 2C — exposes runtime config for n8n wf02) ────────────────────

@app.route("/config", methods=["GET"])
def get_config():
    """Exposes runtime configuration for n8n and monitoring."""
    auth_err = _check_read_key()
    if auth_err:
        return auth_err
    return jsonify({
        "prediction_horizon_minutes": PREDICTION_HORIZON_MINUTES,
        "trade_cooldown_minutes": TRADE_COOLDOWN_MINUTES,
        "min_atr_pct": MIN_ATR_PCT,
        "trend_align_filter": TREND_ALIGN_FILTER,
        "sl_pct_env": float(os.environ.get("SL_PCT", "0.5")),
        "tp_pct_env": float(os.environ.get("TP_PCT", "1.0")),
        "dead_hours_utc": sorted(list(DEAD_HOURS_UTC)),
        "dry_run": DRY_RUN,
        "paused": bool(_BOT_PAUSED),
        "conf_threshold": float(os.environ.get("CONF_THRESHOLD", "0.62")),
    })


# ── ADAPTIVE ENGINE ESTIMATE ──────────────────────────────────────────────────

@app.route("/adaptive-estimate", methods=["GET"])
def adaptive_estimate():
    """Current adaptive engine state."""
    auth_err = _check_read_key()
    if auth_err:
        return auth_err
    return jsonify(_adaptive_engine.get_estimate())


# ── BRAIN STATE (wf08 Brain Monitor) ────────────────────────────────────────

@app.route("/brain-state", methods=["GET"])
def brain_state():
    """Aggregated bot state for wf08 Brain Monitor. Single call = full picture."""
    auth_err = _check_read_key()
    if auth_err:
        return auth_err
    now = _dt.datetime.now(_dt.timezone.utc)
    state = {
        "ts": now.isoformat(),
        "version": VERSION,
        "paused": bool(_BOT_PAUSED),
        "dry_run": DRY_RUN,
        "conf_threshold": float(os.environ.get("CONF_THRESHOLD", "0.62")),
        "capital": float(os.environ.get("CAPITAL_USD") or os.environ.get("CAPITAL", 100)),
    }

    # 1. BTC Price (via Kraken Futures tickers)
    try:
        trade = get_trade_client()
        result = trade.request(method="GET", uri="/derivatives/api/v3/tickers", auth=False)
        tickers = result.get("tickers", []) or []
        ticker = next((t for t in tickers if (t.get("symbol") or "").upper() == DEFAULT_SYMBOL.upper()), None)
        mp = ticker.get("markPrice") if ticker else None
        state["btc_price"] = float(mp) if mp is not None else None
    except Exception:
        state["btc_price"] = None

    # 2. Wallet equity
    try:
        user = get_user_client()
        flex = user.get_wallets().get("accounts", {}).get("flex", {})
        me = flex.get("marginEquity")
        if me is None:
            me = flex.get("pv") or flex.get("portfolioValue")
        state["equity"] = float(me) if me is not None else None
    except Exception:
        state["equity"] = None

    # 3. Open position
    try:
        pos = get_open_position(DEFAULT_SYMBOL)
        state["position"] = pos  # None if flat, {side, size, price} if open
    except Exception:
        state["position"] = None

    # 4. Recent signals + derived stats (last 6h)
    sb_url, sb_key = _sb_config()
    state["signals_6h"] = []
    state["direction_bias"] = None
    state["avg_confidence"] = None
    state["ghost"] = {"evaluated": 0, "correct": 0, "pending": 0, "wr": None}
    state["streak"] = {"direction": None, "count": 0}
    state["performance"] = None

    if sb_url and sb_key:
        sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
        cutoff_6h = (now - _dt.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            r = _sb_session.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=id,direction,confidence,bet_taken,correct,ghost_correct,"
                "ghost_evaluated_at,created_at,signal_price,noise_reason"
                f"&created_at=gte.{cutoff_6h}"
                "&order=created_at.desc&limit=30",
                headers=sb_headers, timeout=5,
            )
            signals = r.json() if r.ok else []
            state["signals_6h"] = signals

            # Direction bias
            up = sum(1 for s in signals if s.get("direction") == "UP")
            down = sum(1 for s in signals if s.get("direction") == "DOWN")
            state["direction_bias"] = {"up": up, "down": down, "total": len(signals)}

            # Avg confidence
            confs = [float(s["confidence"]) for s in signals if s.get("confidence")]
            state["avg_confidence"] = round(sum(confs) / len(confs), 3) if confs else None

            # Ghost stats
            ghost_eval = [s for s in signals if s.get("ghost_evaluated_at")]
            ghost_correct = sum(1 for s in ghost_eval if s.get("ghost_correct"))
            ghost_pending = sum(1 for s in signals if not s.get("ghost_evaluated_at"))
            state["ghost"] = {
                "evaluated": len(ghost_eval),
                "correct": ghost_correct,
                "pending": ghost_pending,
                "wr": round(ghost_correct / len(ghost_eval) * 100, 1) if ghost_eval else None,
            }

            # Streak (consecutive same direction)
            streak_count, streak_dir = 0, None
            for s in signals:
                d = s.get("direction")
                if streak_dir is None:
                    streak_dir = d
                    streak_count = 1
                elif d == streak_dir:
                    streak_count += 1
                else:
                    break
            state["streak"] = {"direction": streak_dir, "count": streak_count}
        except Exception:
            app.logger.debug("[brain-state] signals fetch failed", exc_info=True)

        # 5. Performance (last 10 resolved bets)
        try:
            r = _sb_session.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=correct,pnl_usd,confidence,direction"
                "&bet_taken=eq.true&correct=not.is.null"
                "&order=id.desc&limit=10",
                headers=sb_headers, timeout=5,
            )
            resolved = r.json() if r.ok else []
            if resolved:
                wins = sum(1 for t in resolved if t.get("correct"))
                state["performance"] = {
                    "wr_10": round(wins / len(resolved) * 100, 1),
                    "pnl_5": round(sum(float(t.get("pnl_usd") or 0) for t in resolved[:5]), 4),
                    "resolved_count": len(resolved),
                }
        except Exception:
            pass

    # 6. Macro events (next 2h)
    try:
        cal = _fetch_macro_calendar()
        events_raw = cal.get("data", []) if isinstance(cal, dict) else []
        window_end = now + _dt.timedelta(hours=2)
        upcoming = []
        for ev in events_raw:
            if ev.get("country") != "USD" or ev.get("impact") not in ("High", "red"):
                continue
            raw_date = ev.get("date", "")
            if not raw_date:
                continue
            try:
                ev_dt = _dt.datetime.fromisoformat(raw_date).astimezone(_dt.timezone.utc)
                if now <= ev_dt <= window_end:
                    delta_min = int((ev_dt - now).total_seconds() / 60)
                    upcoming.append({
                        "title": ev.get("title", "?"),
                        "impact": ev.get("impact"),
                        "minutes_away": delta_min,
                    })
            except Exception:
                continue
        state["macro_events"] = upcoming
    except Exception:
        state["macro_events"] = []

    # 7. XGB gate
    _clean = _get_clean_bet_count()
    state["xgb_gate"] = {
        "active": _clean >= _XGB_GATE_MIN_BETS,
        "clean_bets": _clean,
        "min_bets": _XGB_GATE_MIN_BETS,
    }

    # 8. Adaptive engine
    try:
        ace_st = _adaptive_engine.state
        state["adaptive"] = {
            "disabled": _adaptive_engine.disabled,
            "effective_threshold": ace_st.effective_threshold,
            "optimal_threshold": ace_st.optimal_threshold,
            "regime": ace_st.regime,
            "regime_adj": ace_st.regime_adj,
            "direction_bias_adj": ace_st.direction_bias_adj,
            "momentum_factor": ace_st.momentum_factor,
            "wr_50": ace_st.wr_50,
        }
    except Exception:
        state["adaptive"] = {"disabled": True, "error": "unavailable"}

    # 9. Portfolio engine
    try:
        _pe_pos = state.get("position")  # already fetched in section 3
        _pe_eq = state.get("equity") or float(os.environ.get("CAPITAL_USD") or os.environ.get("CAPITAL", 100))
        _pe_btc = state.get("btc_price") or 0.0
        _pe_perf = state.get("performance") or {}
        _pe_wr = _pe_perf.get("wr_10", 50.0) if _pe_perf else 50.0

        # PnL% for open position
        _pe_pnl_pct = 0.0
        if _pe_pos and _pe_btc > 0:
            _pe_entry = float(_pe_pos.get("price", 0) or 0)
            if _pe_entry > 0:
                _pe_sign = 1 if _pe_pos.get("side") == "long" else -1
                _pe_pnl_pct = (_pe_btc - _pe_entry) / _pe_entry * _pe_sign

        _pe_st = _portfolio_engine.build_state(
            position=_pe_pos,
            equity=_pe_eq,
            btc_price=_pe_btc,
            regime=state.get("adaptive", {}).get("regime", "UNKNOWN") if isinstance(state.get("adaptive"), dict) else "UNKNOWN",
            wr_10=_pe_wr,
            existing_pnl_pct=_pe_pnl_pct,
        )
        state["portfolio"] = {
            "disabled": _portfolio_engine.disabled,
            "net_direction": _pe_st.net_direction,
            "total_exposure_btc": _pe_st.total_exposure_btc,
            "total_exposure_pct": _pe_st.total_exposure_pct,
            "unrealized_pnl_usd": _pe_st.unrealized_pnl_usd,
            "unrealized_pnl_pct": round(_pe_pnl_pct * 100, 3),
            "risk_score": _pe_st.risk_score,
            "max_exposure_btc": _pe_st.max_exposure_btc,
        }
    except Exception:
        state["portfolio"] = {"disabled": True, "error": "unavailable"}

    return jsonify(state)


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
                resp = _tg_session.post(
                    f"https://api.telegram.org/bot{tg_token}/sendPhoto",
                    data={"chat_id": channel_id, "caption": caption, "parse_mode": parse_mode},
                    files={"photo": f},
                    timeout=20,
                )
        else:
            if len(text) > 4096:
                return jsonify({"error": "text too long (max 4096 chars)"}), 400
            resp = _tg_session.post(
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


# ── PUBLISH X / TWITTER ─────────────────────────────────────────────────────

def _twitter_oauth_header(method, url):
    """Build OAuth 1.0a Authorization header for Twitter API v2 (stdlib only)."""
    import urllib.parse as _up
    import uuid as _uuid
    import base64 as _b64

    consumer_key = os.environ.get("TWITTER_API_KEY", "")
    consumer_secret = os.environ.get("TWITTER_API_SECRET", "")
    token = os.environ.get("TWITTER_ACCESS_TOKEN", "")
    token_secret = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET", "")

    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": _uuid.uuid4().hex,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(int(time.time())),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    param_str = "&".join(
        f"{_up.quote(k, safe='')}={_up.quote(v, safe='')}"
        for k, v in sorted(oauth_params.items())
    )
    base_str = f"{method.upper()}&{_up.quote(url, safe='')}&{_up.quote(param_str, safe='')}"
    signing_key = f"{_up.quote(consumer_secret, safe='')}&{_up.quote(token_secret, safe='')}"
    signature = _b64.b64encode(
        _hmac.new(signing_key.encode(), base_str.encode(), hashlib.sha1).digest()
    ).decode()
    oauth_params["oauth_signature"] = signature
    return "OAuth " + ", ".join(
        f'{_up.quote(k, safe="")}="{_up.quote(v, safe="")}"'
        for k, v in sorted(oauth_params.items())
    )


@app.route("/publish-x", methods=["POST"])
def publish_x():
    """Pubblica un tweet via Twitter API v2 con OAuth 1.0a.
    Protected by BOT_API_KEY. Rate-limited a 5/min.
    Body JSON: {text: str}
    """
    err = _check_api_key()
    if err:
        return err
    _rl_key = f"pubx:{request.headers.get('X-Api-Key', request.remote_addr)}"
    if not _check_rate_limit(_rl_key, max_calls=5):
        return jsonify({"error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    if len(text) > 280:
        return jsonify({"error": "text too long (max 280 chars for tweets)"}), 400
    required = ["TWITTER_API_KEY", "TWITTER_API_SECRET",
                 "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        return jsonify({"error": f"Twitter not configured (missing: {', '.join(missing)})"}), 503
    try:
        url = "https://api.twitter.com/2/tweets"
        resp = _sb_session.post(
            url, json={"text": text},
            headers={"Authorization": _twitter_oauth_header("POST", url),
                     "Content-Type": "application/json"},
            timeout=15,
        )
        result = resp.json()
        if resp.status_code not in (200, 201):
            error_msg = result.get("detail") or result.get("title") or str(result)
            return jsonify({"error": f"Twitter API error: {error_msg}"}), 502
        tweet_data = result.get("data", {})
        return jsonify({"ok": True, "tweet_id": tweet_data.get("id")})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── PUBLISH LINKEDIN ────────────────────────────────────────────────────────

@app.route("/publish-linkedin", methods=["POST"])
def publish_linkedin():
    """Pubblica un post su LinkedIn via API v2.
    Protected by BOT_API_KEY. Rate-limited a 5/min.
    Body JSON: {text: str}
    """
    err = _check_api_key()
    if err:
        return err
    _rl_key = f"publi:{request.headers.get('X-Api-Key', request.remote_addr)}"
    if not _check_rate_limit(_rl_key, max_calls=5):
        return jsonify({"error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    text = str(data.get("text", "")).strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    if len(text) > 3000:
        return jsonify({"error": "text too long (max 3000 chars for LinkedIn)"}), 400
    access_token = os.environ.get("LINKEDIN_ACCESS_TOKEN", "")
    person_urn = os.environ.get("LINKEDIN_PERSON_URN", "")
    if not access_token or not person_urn:
        return jsonify({"error": "LinkedIn not configured (set LINKEDIN_ACCESS_TOKEN + LINKEDIN_PERSON_URN on Railway)"}), 503
    try:
        resp = _ext_session.post(
            "https://api.linkedin.com/v2/ugcPosts",
            json={
                "author": person_urn,
                "lifecycleState": "PUBLISHED",
                "specificContent": {
                    "com.linkedin.ugc.ShareContent": {
                        "shareCommentary": {"text": text},
                        "shareMediaCategory": "NONE",
                    }
                },
                "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
            },
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Restli-Protocol-Version": "2.0.0",
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            return jsonify({"ok": True, "post_id": resp.json().get("id", "")})
        return jsonify({"error": f"LinkedIn error ({resp.status_code}): {resp.text[:300]}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── PUBLISH REDDIT ──────────────────────────────────────────────────────────

@app.route("/publish-reddit", methods=["POST"])
def publish_reddit():
    """Pubblica un post su Reddit via API.
    Protected by BOT_API_KEY. Rate-limited a 3/min.
    Body JSON: {text: str, title: str, subreddit?: str}
    """
    err = _check_api_key()
    if err:
        return err
    _rl_key = f"pubrd:{request.headers.get('X-Api-Key', request.remote_addr)}"
    if not _check_rate_limit(_rl_key, max_calls=3):
        return jsonify({"error": "rate_limited"}), 429
    data = request.get_json(force=True) or {}
    text = str(data.get("text", "")).strip()
    title = str(data.get("title", "")).strip()
    if not text:
        return jsonify({"error": "text required"}), 400
    if not title:
        return jsonify({"error": "title required (Reddit posts need a title)"}), 400
    client_id = os.environ.get("REDDIT_CLIENT_ID", "")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
    username = os.environ.get("REDDIT_USERNAME", "")
    password = os.environ.get("REDDIT_PASSWORD", "")
    subreddit = data.get("subreddit") or os.environ.get("REDDIT_SUBREDDIT", "algotrading")
    if not all([client_id, client_secret, username, password]):
        return jsonify({"error": "Reddit not configured (set REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD on Railway)"}), 503
    try:
        # Step 1: OAuth2 token
        auth_resp = _ext_session.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(client_id, client_secret),
            data={"grant_type": "password", "username": username, "password": password},
            headers={"User-Agent": "btcpredictor-bot/1.0"},
            timeout=10,
        )
        if auth_resp.status_code != 200:
            return jsonify({"error": f"Reddit auth failed ({auth_resp.status_code})"}), 502
        token = auth_resp.json().get("access_token")
        if not token:
            return jsonify({"error": "Reddit auth: no access_token"}), 502
        # Step 2: Submit post
        resp = _ext_session.post(
            "https://oauth.reddit.com/api/submit",
            data={"kind": "self", "sr": subreddit, "title": title,
                  "text": text, "api_type": "json"},
            headers={"Authorization": f"Bearer {token}",
                     "User-Agent": "btcpredictor-bot/1.0"},
            timeout=15,
        )
        result = resp.json()
        json_data = result.get("json", {})
        errors = json_data.get("errors", [])
        if errors:
            return jsonify({"error": f"Reddit errors: {errors}"}), 502
        post_data = json_data.get("data", {})
        return jsonify({"ok": True, "post_url": post_data.get("url", ""),
                        "subreddit": subreddit})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ── PUBLISH STATUS ──────────────────────────────────────────────────────────

@app.route("/publish-status", methods=["GET"])
def publish_status():
    """Quali canali hanno credenziali configurate. Protected by BOT_API_KEY."""
    err = _check_api_key()
    if err:
        return err
    return jsonify({
        "telegram": bool(os.environ.get("TELEGRAM_BOT_TOKEN")),
        "x": all(os.environ.get(k) for k in [
            "TWITTER_API_KEY", "TWITTER_API_SECRET",
            "TWITTER_ACCESS_TOKEN", "TWITTER_ACCESS_TOKEN_SECRET"]),
        "linkedin": all(os.environ.get(k) for k in [
            "LINKEDIN_ACCESS_TOKEN", "LINKEDIN_PERSON_URN"]),
        "reddit": all(os.environ.get(k) for k in [
            "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
            "REDDIT_USERNAME", "REDDIT_PASSWORD"]),
        "reddit_subreddit": os.environ.get("REDDIT_SUBREDDIT", "algotrading"),
    })


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
                    r = _kraken_session.get(
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
                    f"&select=id,entry_fill_price,btc_price_entry,bet_size,direction,funding_rate,created_at"
                ) if _explicit else (
                    f"{_sb_url}/rest/v1/{SUPABASE_TABLE}"
                    f"?bet_taken=eq.true&correct=is.null"
                    f"&order=id.desc&limit=1"
                    f"&select=id,entry_fill_price,btc_price_entry,bet_size,direction,funding_rate,created_at"
                )
                _or = _sb_session.get(_oq, headers=_sb_h, timeout=5)
                _orphans = _or.json() if _or.ok else []
            except Exception:
                _orphans = []

            if not _orphans:
                return jsonify({
                    "status": "no_position",
                    "message": "No open position, nothing to close.",
                    "symbol": symbol
                })

            # [FIX1A] Bet orfana trovata — SL ha già chiuso la posizione Kraken.
            # Prima cerca il fill reale da Kraken, poi fallback a position_gone.
            _row = _orphans[0]
            _obid = _row["id"]
            _entry = float(_row.get("entry_fill_price") or _row.get("btc_price_entry") or 0)
            _bsize = float(_row.get("bet_size") or 0.001)
            _dir   = _row.get("direction", "UP")

            # Step 1: Try to find real SL fill from Kraken
            # [FIX8] Prefer fill matching the stored sl_order_id over "most recent fill".
            # Previous logic used most recent fill for the symbol, causing all concurrent
            # orphan bets to be assigned the same exit price (67978 bug on 4 Mar).
            _real_exit_price = None
            _fill_source = None
            _sl_oid = _row.get("sl_order_id")
            try:
                _trade_k = get_trade_client()
                _fills_resp = _trade_k.request(
                    method="GET",
                    uri="/derivatives/api/v3/fills",
                    auth=True,
                )
                _all_fills = _fills_resp.get("fills", []) or []
                _symbol_fills = [
                    f for f in _all_fills
                    if (f.get("symbol") or "").upper() == symbol.upper()
                ]
                if _symbol_fills:
                    # Prefer fill that matches the stored SL order ID
                    if _sl_oid:
                        _matched = [f for f in _symbol_fills if f.get("order_id") == _sl_oid]
                        if _matched:
                            _symbol_fills = _matched
                            _fill_source = "kraken_sl_order"
                    if not _fill_source:
                        _fill_source = "kraken_fill_recent"
                    # Most recent fill first
                    _symbol_fills.sort(key=lambda f: f.get("fillTime", ""), reverse=True)
                    _real_exit_price = float(_symbol_fills[0].get("price", 0))
                    app.logger.info(
                        f"[FIX8] Orphan {_obid}: found Kraken fill price={_real_exit_price} "
                        f"source={_fill_source} (order={_symbol_fills[0].get('order_id')})"
                    )
            except Exception as _fill_err:
                app.logger.warning(f"[FIX8] Kraken fills lookup failed for orphan {_obid}: {_fill_err}")

            # Step 2: If no fill found, mark as position_gone with NULL pnl
            if not _real_exit_price or _real_exit_price <= 0:
                _patch = {
                    "correct":           None,
                    "close_reason":      "position_gone",
                    "source_updated_by": "close_orphan_no_fill",
                }
                _supabase_update(_obid, _patch, only_if_unresolved=True)
                app.logger.warning(
                    f"[FIX1] Orphan bet {_obid}: no Kraken fill found. "
                    f"Marked position_gone with pnl=NULL (not fabricated)."
                )
                _push_cockpit_log("app", "warning", "Orphan: position_gone",
                                  f"Bet {_obid}: no fill found on Kraken, pnl not fabricated")
                return jsonify({
                    "status":        "resolved_orphan",
                    "message":       "Position gone — no fill found on Kraken. PnL not fabricated.",
                    "bet_id":        _obid,
                    "close_reason":  "position_gone",
                    "pnl_usd":       None,
                    "symbol":        symbol,
                })

            # Step 3: Calculate real PnL from Kraken fill
            if _entry > 0:
                if _dir == "UP":
                    _pg = (_real_exit_price - _entry) * _bsize
                    _correct = _real_exit_price > _entry
                    _adir = "UP" if _correct else "DOWN"
                else:
                    _pg = (_entry - _real_exit_price) * _bsize
                    _correct = _real_exit_price < _entry
                    _adir = "DOWN" if _correct else "UP"
                _fee = _bsize * (_entry + _real_exit_price) * TAKER_FEE

                # Funding cost from Supabase funding_rate + hold time
                _funding_cost = 0.0
                _fr = _row.get("funding_rate")
                _created = _row.get("created_at")
                if _fr is not None and _created:
                    try:
                        _fr_f = float(_fr)
                        from datetime import datetime as _dt_fc, timezone as _tz_fc
                        _created_dt = _dt_fc.fromisoformat(_created.replace("Z", "+00:00"))
                        _mins_held = max(0, (_dt_fc.now(_tz_fc.utc) - _created_dt).total_seconds() / 60)
                        _funding_cost = _bsize * _fr_f * (_mins_held / 480) * _real_exit_price
                        app.logger.info(f"[FUNDING] orphan bet {_obid}: rate={_fr_f}, mins={_mins_held:.0f}, cost={_funding_cost:.6f}")
                    except Exception as _fc_err:
                        app.logger.warning(f"[FUNDING] orphan bet {_obid} calc failed: {_fc_err}")

                _pnl = round(_pg - _fee - _funding_cost, 6)
                _patch = {
                    "exit_fill_price":  _real_exit_price,
                    "btc_price_exit":   _real_exit_price,
                    "correct":          _correct,
                    "actual_direction": _adir,
                    "pnl_usd":          _pnl,
                    "fees_total":       round(_fee, 6),
                    "close_reason":     "sl_already_closed",
                    "source_updated_by": f"close_orphan_{_fill_source}",
                }
                if _funding_cost != 0.0:
                    _patch["funding_fee"] = round(-_funding_cost, 6)
                # Optimistic locking: only update if not already resolved
                _orp_sb_url, _orp_sb_key = _sb_config()
                _sb_session.patch(
                    f"{_orp_sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{_obid}&correct=is.null",
                    json=_patch,
                    headers={"apikey": _orp_sb_key, "Authorization": f"Bearer {_orp_sb_key}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"},
                    timeout=10,
                )
                app.logger.info(
                    f"[FIX1] Orphan bet {_obid} resolved via {_fill_source}: "
                    f"exit={_real_exit_price}, pnl={_pnl}, correct={_correct}"
                )
                return jsonify({
                    "status":              "resolved_orphan",
                    "message":             f"Orphan resolved via {_fill_source}.",
                    "bet_id":              _obid,
                    "exit_price_used":     _real_exit_price,
                    "fill_source":         _fill_source,
                    "pnl_usd":             _pnl,
                    "funding_cost_usd":    round(_funding_cost, 6),
                    "correct":             _correct,
                    "symbol":              symbol,
                })

            # entry price missing — can't calculate PnL
            _patch = {
                "correct":           None,
                "close_reason":      "position_gone",
                "source_updated_by": "close_orphan_no_entry",
            }
            _supabase_update(_obid, _patch, only_if_unresolved=True)
            app.logger.warning(
                f"[FIX1] Orphan bet {_obid}: fill found but entry price missing. "
                f"Marked position_gone."
            )
            return jsonify({
                "status":        "resolved_orphan",
                "message":       "Orphan resolved but entry price missing — PnL not calculated.",
                "bet_id":        _obid,
                "close_reason":  "position_gone",
                "pnl_usd":       None,
                "symbol":        symbol,
            })

        close_side = "sell" if pos["side"] == "long" else "buy"
        size = pos["size"]
        funding_fee = _get_funding_fee()  # read BEFORE closing — settles to 0 after close

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
                        f"?id=eq.{explicit_bet_id}&select=id,entry_fill_price,btc_price_entry,bet_size,direction,funding_rate,created_at"
                    )
                else:
                    query = (
                        f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
                        f"?bet_taken=eq.true&correct=is.null"
                        f"&order=id.desc&limit=1"
                        f"&select=id,entry_fill_price,btc_price_entry,bet_size,direction,funding_rate,created_at"
                    )
                resp = _sb_session.get(query, headers=headers, timeout=5)
                rows = resp.json() if resp.ok else []

                if rows and exit_fill_price and exit_fill_price > 0:
                    row = rows[0]
                    bet_id = row["id"]
                    entry_price = float(row.get("entry_fill_price") or row.get("btc_price_entry") or exit_fill_price)
                    bet_size = float(row.get("bet_size") or size or 0.001)
                    direction = row.get("direction", "UP")

                    _pnl = _calculate_pnl(entry_price, exit_fill_price, bet_size, direction, funding_fee=funding_fee)
                    patch_data = {
                        "exit_fill_price":    exit_fill_price,
                        "btc_price_exit":     exit_fill_price,
                        "correct":            _pnl["correct"],
                        "actual_direction":   _pnl["actual_direction"],
                        "pnl_usd":            _pnl["pnl_net"],
                        "pnl_pct":            _pnl["pnl_pct"],
                        "fees_total":         _pnl["fee_usd"],
                        "has_real_exit_fill": True,
                        "close_reason":       "manual_close",
                        "source_updated_by":  "wf02_close",
                    }
                    if funding_fee != 0.0:
                        patch_data["funding_fee"] = round(funding_fee, 6)
                    # Optimistic locking: only update if not already resolved
                    _cp_sb_url, _cp_sb_key = _sb_config()
                    _sb_session.patch(
                        f"{_cp_sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}&correct=is.null",
                        json=patch_data,
                        headers={"apikey": _cp_sb_key, "Authorization": f"Bearer {_cp_sb_key}",
                                 "Content-Type": "application/json", "Prefer": "return=minimal"},
                        timeout=10,
                    )
                    supabase_updated = True
                    app.logger.info(f"[close-position] Supabase updated: bet {bet_id}, pnl={pnl_net}, correct={correct}")
            except Exception as e:
                app.logger.warning(f"[close-position] Supabase update failed (non-critical): {e}")
                _push_cockpit_log("app", "warning", "Close position: Supabase update failed", str(e),
                                  {"symbol": symbol})

        return jsonify({
            "status": "closed" if (ok and after is None) else ("closing" if ok else "failed"),
            "symbol": symbol,
            "closed_side": pos["side"],
            "close_order_side": close_side,
            "size": size,
            "exit_fill_price": exit_fill_price,
            "pnl_usd": pnl_net,
            "funding_fee": round(funding_fee, 6),
            "supabase_updated": supabase_updated,
            "position_after": after,
            "raw": result,
        }), (200 if ok else 400)

    except Exception as e:
        app.logger.exception("Endpoint error")
        _push_cockpit_log("app", "error", "Close position FAILED", str(e), {"symbol": symbol})
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
    _push_cockpit_log("app", "warning", "Bot PAUSED", "Manual pause via /pause API")
    return jsonify({"paused": True, "message": "Bot paused — no new trades"}), 200


@app.route("/resume", methods=["POST"])
def resume_bot():
    err = _check_api_key()
    if err:
        return err
    # Cooldown guard: block resume if circuit breaker fired < 30 min ago
    elapsed = time.time() - _CB_TRIPPED_AT
    if _CB_TRIPPED_AT > 0 and elapsed < _CB_COOLDOWN_SEC:
        remaining = int((_CB_COOLDOWN_SEC - elapsed) / 60)
        app.logger.warning(f"[RESUME] Blocked — CB cooldown active, {remaining}m remaining")
        _push_cockpit_log("app", "warning", "Resume blocked",
                          f"Circuit-breaker cooldown: {remaining}m remaining (30m required)")
        return jsonify({
            "paused": True,
            "error": "cooldown_active",
            "message": f"Circuit breaker cooldown active — retry in {remaining} minutes",
            "cooldown_remaining_min": remaining,
        }), 429
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT, _RESUMED_AT
    _BOT_PAUSED = False
    _BOT_PAUSED_REFRESHED_AT = time.time()
    _RESUMED_AT = _dt.datetime.now(_dt.timezone.utc).isoformat()
    _save_bot_paused(False)
    _save_resumed_at(_RESUMED_AT)
    _push_cockpit_log("app", "success", "Bot RESUMED", f"Manual resume via /resume API — resumed_at={_RESUMED_AT}")
    return jsonify({"paused": False, "message": "Bot resumed — trading active", "resumed_at": _RESUMED_AT}), 200


# ── PLACE BET — helper privati ───────────────────────────────────────────────

def _get_clean_bet_count() -> int:
    """Return number of bets with known outcome in SUPABASE_TABLE. Cache 10 min.
    Used by XGBoost gate to check if dataset is large enough to trust the model."""
    global _XGB_CLEAN_BET_COUNT, _XGB_CLEAN_BET_CHECKED_AT
    if (
        _XGB_CLEAN_BET_COUNT is not None
        and time.time() - _XGB_CLEAN_BET_CHECKED_AT < _XGB_CLEAN_CACHE_TTL
    ):
        return _XGB_CLEAN_BET_COUNT
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            r = _sb_session.get(
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
    """Run dual-gate XGBoost. Returns (xgb_prob_up, early_exit_response_or_None).
    If XGB unavailable or fails → (0.5, None) = proceed normally."""
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
        _dow_xgb = _dt.datetime.now(_dt.timezone.utc).weekday()
        _session_xgb = 0 if _h < 8 else (1 if _h < 14 else 2)
        feat_row = [[
            confidence,
            float(data.get("fear_greed", data.get("fear_greed_value", 50))),
            float(data.get("rsi14", 50)),
            float(data.get("technical_score", 0)),
            math.sin(2 * math.pi * _h / 24),
            math.cos(2 * math.pi * _h / 24),
            float(_BIAS_MAP.get((data.get("technical_bias") or "").lower().strip(), 0)),
            1.0 if float(data.get("fear_greed_value", data.get("fear_greed", 50)) or 50) < 45 else 0.0,
            math.sin(2 * math.pi * _dow_xgb / 7),
            math.cos(2 * math.pi * _dow_xgb / 7),
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
    """Check bot_paused + circuit breaker. Returns Flask response if must stop, None if ok."""
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT
    if time.time() - _BOT_PAUSED_REFRESHED_AT > 300:
        _refresh_bot_paused()

    if _BOT_PAUSED:
        return jsonify({
            "status": "paused",
            "message": "Bot paused — no new trades opened",
            "direction": direction,
            "confidence": confidence,
        }), 200

    # Circuit breaker: 5 consecutive losses → auto-pause
    # Skip check if no bets have been placed since last /resume (prevents re-pause loop)
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            cb_query = (
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=correct&bet_taken=eq.true&correct=not.is.null"
                "&order=id.desc&limit=5"
            )
            if _RESUMED_AT:
                cb_query += f"&created_at=gte.{_RESUMED_AT}"
            r_cb = _sb_session.get(
                cb_query,
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                timeout=5,
            )
            if r_cb.status_code == 200:
                last5 = r_cb.json()
                if len(last5) == 5 and all(row.get("correct") is False for row in last5):
                    with _PAUSE_LOCK:
                        _BOT_PAUSED = True
                        _BOT_PAUSED_REFRESHED_AT = time.time()
                    global _CB_TRIPPED_AT
                    _CB_TRIPPED_AT = time.time()
                    try:
                        _save_bot_paused(True)
                    except Exception as _cb_save_err:
                        app.logger.error(f"[CIRCUIT_BREAKER] save_paused failed (DB down?): {_cb_save_err}")
                    app.logger.warning("[CIRCUIT_BREAKER] 5 consecutive losses → bot auto-paused")
                    _push_cockpit_log("app", "critical", "Circuit breaker tripped",
                                      "5 consecutive losses — bot auto-paused",
                                      {"direction": direction, "confidence": confidence})
                    return jsonify({
                        "status": "paused",
                        "reason": "circuit_breaker",
                        "message": "5 consecutive losses — bot auto-paused. Resume manually with /resume.",
                        "direction": direction,
                        "confidence": confidence,
                    }), 200
    except Exception as e:
        app.logger.warning(f"[CIRCUIT_BREAKER] check failed: {e}")
    return None


def _check_price_drift(signal_price, symbol, direction, confidence):
    """Pre-trade slippage guard. Returns (drift_pct, flask_response_or_None)."""
    if not signal_price or signal_price <= 0:
        return 0.0, None          # guard disabled — no signal price

    mark = _get_mark_price(symbol)
    if mark <= 0:
        return 0.0, None          # Kraken API failed — fail open

    drift = abs(mark - signal_price) / signal_price

    if drift > SLIPPAGE_MAX_PCT:
        _push_cockpit_log("app", "warning",
            f"Slippage guard: {drift:.4%} drift",
            f"signal={signal_price:.1f} mark={mark:.1f} threshold={SLIPPAGE_MAX_PCT:.4%} dir={direction}",
            {"direction": direction, "confidence": confidence,
             "signal_price": signal_price, "mark_price": mark,
             "drift_pct": round(drift, 6), "threshold": SLIPPAGE_MAX_PCT})
        return drift, (jsonify({
            "status": "skipped", "reason": "price_drift",
            "signal_price": signal_price, "mark_price": mark,
            "drift_pct": round(drift, 6), "threshold": SLIPPAGE_MAX_PCT,
            "direction": direction, "confidence": confidence,
        }), 200)

    return drift, None


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
    size = _safe_float(raw_size, default=0.0001, min_v=0.0, max_v=0.5)
    if size <= 0:
        size = 0.0001

    _raw_sp = data.get("signal_price")
    signal_price = _safe_float(_raw_sp, default=0.0, min_v=0.0) if _raw_sp is not None else 0.0

    if direction not in ("UP", "DOWN"):
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    # P0.2 — filtro ore morte (WR storico < 45% UTC, aggiornato da /reload-calibration)
    current_hour_utc = time.gmtime().tm_hour
    with _DEAD_HOURS_LOCK:
        _is_dead_hour = current_hour_utc in DEAD_HOURS_UTC
    if _is_dead_hour:
        return jsonify({
            "status": "skipped",
            "reason": "dead_hour",
            "hour_utc": current_hour_utc,
            "message": f"Hour {current_hour_utc}h UTC has historically low WR (<45%). Skipping bet."
        }), 200

    # [FIX3B] Cooldown — prevent over-trading (was 31 trades in 3h)
    global _LAST_TRADE_PLACED_AT
    with _TRADE_LOCK:
        _cooldown_elapsed = time.time() - _LAST_TRADE_PLACED_AT
    _cooldown_required = TRADE_COOLDOWN_MINUTES * 60
    if _cooldown_elapsed < _cooldown_required:
        _remaining = round((_cooldown_required - _cooldown_elapsed) / 60, 1)
        app.logger.info(f"[FIX3] Cooldown active: {_remaining}min remaining")
        return jsonify({
            "status": "skipped",
            "reason": "cooldown",
            "cooldown_minutes": TRADE_COOLDOWN_MINUTES,
            "remaining_minutes": _remaining,
            "direction": direction,
            "confidence": confidence,
        }), 200

    # ── AI Council deliberation (COUNCIL_MODE=true) ───────────────────────────
    if COUNCIL_MODE:
        # Pre-compute XGB probability for QUANT member (avoid circular import)
        _c_xgb_prob_up, _ = _run_xgb_gate(direction, confidence, data, current_hour_utc)
        _council_payload = {**data, "xgb_prob_up": _c_xgb_prob_up}
        with _COUNCIL_LOCK:
            global _COUNCIL_DELIBERATING
            _COUNCIL_DELIBERATING = True
        _council_votes = council_engine.run_round1(_council_payload)
        _council_result = council_engine.compute_weighted_vote(_council_votes)

        # Signal hash ties this cycle's votes together (5-min bucket)
        _council_hash = hashlib.sha256(
            f"{direction}{confidence}{int(time.time() // 300)}".encode()
        ).hexdigest()[:16]
        council_engine.log_votes_async(_council_votes, _council_hash)

        # Thoth Protocol — cache deliberation for /council-status
        _store_council_result(_council_votes, _council_result, _council_hash)

        _council_dir = _council_result["direction"]
        _council_agr = _council_result["agreement_score"]

        app.logger.info(
            f"[COUNCIL] dir={_council_dir} conf={_council_result['council_confidence']:.2f} "
            f"agreement={_council_agr:.2f} score={_council_result['score']:.2f} "
            f"original={direction}/{confidence:.2f}"
        )

        if _council_dir == "SKIP" or _council_agr < 0.50:
            return jsonify({
                "status": "skipped",
                "reason": "council_low_agreement",
                "council_result": _council_result,
                "original_direction": direction,
                "original_confidence": confidence,
            }), 200

        # Override direction + confidence with council decision
        _original_direction = direction  # preserve for counter-trend fallback
        direction = _council_dir
        confidence = _council_result["council_confidence"]
    else:
        _original_direction = direction

    # [FIX4+5] Regime check (single Binance API call for volatility + trend filters)
    _regime_data = None
    try:
        _regime_data = _compute_regime_4h_live()
    except Exception as _reg_err:
        app.logger.warning(f"[FIX4] Regime check failed (fail-open): {_reg_err}")
        # fail-open: skip both filters

    if _regime_data and "error" not in _regime_data:
        # [FIX4B] Volatility filter — skip if ATR below breakeven threshold
        _atr_pct = _regime_data.get("atr_4h_pct", 0.0)
        if _atr_pct < MIN_ATR_PCT:
            app.logger.info(
                f"[FIX4] Low volatility skip: ATR={_atr_pct:.4f}% < MIN={MIN_ATR_PCT}%"
            )
            return jsonify({
                "status": "skipped",
                "reason": "low_volatility",
                "atr_4h_pct": _atr_pct,
                "min_atr_pct": MIN_ATR_PCT,
                "direction": direction,
                "confidence": confidence,
            }), 200

        # [FIX5C] Counter-trend filter — block trades against strong trend
        # If council flipped direction and it's counter-trend, fall back to original
        # [FIX7] Removed _regime_name == "TRENDING" constraint: filter now fires in ALL
        # regimes (TRENDING, RANGING, VOLATILE) when trend is strong enough.
        # Threshold lowered 0.3→0.2 to catch intra-session downtrends classified as RANGING.
        if TREND_ALIGN_FILTER:
            _regime_name = _regime_data.get("regime_name", "")
            _trend_dir = _regime_data.get("trend_direction", "")
            _trend_str = _regime_data.get("trend_strength", 0.0)
            if (_trend_str > 0.2
                    and _trend_dir
                    and direction != _trend_dir):
                # Council flipped direction? Fall back to original if it aligns with trend
                if direction != _original_direction and _original_direction == _trend_dir:
                    app.logger.info(
                        f"[FIX5] Council flip rejected: council={direction} vs trend={_trend_dir}. "
                        f"Falling back to original={_original_direction}"
                    )
                    direction = _original_direction
                    # Keep council confidence but reduce by 10% as penalty for disagreement
                    confidence = round(confidence * 0.90, 4)
                else:
                    app.logger.info(
                        f"[FIX7] Counter-trend skip: signal={direction} vs trend={_trend_dir} "
                        f"(strength={_trend_str:.4f}%, regime={_regime_name})"
                    )
                    return jsonify({
                        "status": "skipped",
                        "reason": "counter_trend",
                        "signal_direction": direction,
                        "trend_direction": _trend_dir,
                        "trend_strength": _trend_str,
                        "regime": _regime_name,
                        "confidence": confidence,
                    }), 200

    # [FIX6] Micro-regime 1H filter — penalize signals against 1H micro-trend
    # Catches "bouncing in 4H downtrend" scenario: model predicts DOWN but 1H EMA is UP
    _micro = {"micro_dir": "UNKNOWN", "micro_strength": 0.0, "error": "not_computed"}
    try:
        _micro = _compute_micro_regime_1h()
        if (_micro.get("error") is None
                and _micro.get("micro_strength", 0) > 0.15
                and _micro.get("micro_dir") != direction):
            _penalty = 0.08  # 8% confidence penalty for counter-micro-trend signal
            _prev_conf = confidence
            confidence = round(confidence * (1.0 - _penalty), 4)
            app.logger.info(
                f"[FIX6] Micro-regime penalty: 1H={_micro['micro_dir']} "
                f"signal={direction} strength={_micro['micro_strength']:.4f}% "
                f"conf {_prev_conf:.3f} → {confidence:.3f}"
            )
    except Exception as _micro_err:
        app.logger.warning(f"[FIX6] Micro-regime check failed (fail-open): {_micro_err}")

    # Persist signal-time features to prediction row (best-effort, non-blocking)
    # bet_id passed by wf01B from "Create a row" node; used to backfill training features
    _bet_id_for_micro = data.get("bet_id")
    if _bet_id_for_micro:
        _signal_patch: dict = {}
        if _micro.get("error") is None:
            _signal_patch["micro_regime_1h"] = _micro.get("micro_dir")
            _signal_patch["micro_strength_1h"] = round(_micro.get("micro_strength", 0), 4)
        _fr = data.get("funding_rate")
        if _fr is not None:
            try:
                _signal_patch["funding_rate"] = round(float(_fr), 8)
            except (TypeError, ValueError):
                pass
        if _signal_patch:
            try:
                _sb_u, _sb_k = _sb_config()
                if _sb_u and _sb_k:
                    _sb_session.patch(
                        f"{_sb_u}/rest/v1/{SUPABASE_TABLE}?id=eq.{_bet_id_for_micro}",
                        json=_signal_patch,
                        headers={"apikey": _sb_k, "Authorization": f"Bearer {_sb_k}",
                                 "Content-Type": "application/json", "Prefer": "return=minimal"},
                        timeout=2,
                    )
            except Exception:
                pass  # fail-open — never block a trade for data logging

    # ACE — Adaptive Calibration Engine gate
    ace_result = _adaptive_engine.evaluate(confidence, direction)
    if not ace_result.get("should_trade", True):
        _push_cockpit_log("app", "info", "ACE skip",
                          f"dir={direction} conf={confidence:.2f} adj={ace_result.get('adjusted_conf'):.3f} "
                          f"thr={ace_result.get('effective_threshold'):.3f}")
        return jsonify({
            "status": "skipped",
            "reason": "adaptive_threshold",
            "direction": direction,
            "raw_confidence": confidence,
            "adaptive": ace_result,
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

    # Slippage guard: resolve signal_price from Supabase if not in POST body
    _sp_bet_id = None
    if signal_price <= 0:
        try:
            sb_url, sb_key = _sb_config()
            if sb_url and sb_key:
                r_sp = _sb_session.get(
                    f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                    f"?select=id,signal_price,btc_price_entry"
                    f"&direction=eq.{direction}&bet_taken=eq.false"
                    f"&order=id.desc&limit=1",
                    headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                    timeout=3)
                if r_sp.status_code == 200 and r_sp.json():
                    _sp_row = r_sp.json()[0]
                    _sp_bet_id = _sp_row.get("id")
                    signal_price = float(_sp_row.get("signal_price") or _sp_row.get("btc_price_entry") or 0)
        except Exception as _sp_err:
            app.logger.warning(f"[place-bet] signal_price fetch failed (fail-open): {_sp_err}")

    price_drift_pct, drift_exit = _check_price_drift(signal_price, symbol, direction, confidence)
    if drift_exit:
        if _sp_bet_id and signal_price > 0:
            try:
                sb_url, sb_key = _sb_config()
                if sb_url and sb_key:
                    _sb_session.patch(
                        f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{_sp_bet_id}",
                        json={"price_drift_pct": round(price_drift_pct, 6)},
                        headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}",
                                 "Content-Type": "application/json", "Prefer": "return=minimal"},
                        timeout=3)
            except Exception:
                pass
        return drift_exit

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
        # [FIX9] Pre-flight open-bet cap — checked BEFORE Kraken position lookup.
        # Previous bug: hard cap lived inside `if pos:`, so it was bypassed when
        # two opposing bets netted to a flat Kraken position (net long + short = 0).
        # Now we block any new bet if Supabase already has 1+ unresolved real bets.
        try:
            _sb_url_pf, _sb_key_pf = _sb_config()
            if _sb_url_pf and _sb_key_pf:
                _pf_r = _kraken_session.get(
                    f"{_sb_url_pf}/rest/v1/{SUPABASE_TABLE}"
                    "?select=id&bet_taken=eq.true&correct=is.null",
                    headers={"apikey": _sb_key_pf, "Authorization": f"Bearer {_sb_key_pf}",
                             "Prefer": "count=exact"},
                    timeout=5,
                )
                _pf_count = 0
                if _pf_r.status_code == 200:
                    _cr = _pf_r.headers.get("content-range", "")
                    try:
                        _pf_count = int(_cr.split("/")[1]) if "/" in _cr else len(_pf_r.json())
                    except Exception:
                        _pf_count = len(_pf_r.json())
                if _pf_count >= 1:
                    app.logger.warning(
                        f"[FIX9] Pre-flight cap: {_pf_count} unresolved bet(s) in Supabase — blocking"
                    )
                    return jsonify({
                        "status": "skipped",
                        "reason": f"MAX_OPEN_BETS reached ({_pf_count} unresolved bets)",
                        "symbol": symbol, "direction": direction, "no_stack": True,
                    }), 200
        except Exception as _pf_err:
            app.logger.warning(f"[FIX9] Pre-flight cap check failed (fail-open): {_pf_err}")

        pos = get_open_position(symbol)
        trade = get_trade_client()
        base_size = size  # preserve original size from payload

        # [FIX1B] Sync check: Kraken has position but Supabase has no open bet → orphan
        if pos:
            try:
                sb_url, sb_key = _sb_config()
                if sb_url and sb_key:
                    _sync_r = _kraken_session.get(
                        f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                        "?select=id&bet_taken=eq.true&correct=is.null&limit=1",
                        headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                        timeout=3,
                    )
                    if _sync_r.ok and not _sync_r.json():
                        # Kraken position exists but no open bet in Supabase → close orphan
                        app.logger.warning(
                            f"[FIX1B] Orphan position detected: Kraken has {pos['side']} "
                            f"but Supabase has no open bets. Closing orphan."
                        )
                        _push_cockpit_log("app", "warning", "Orphan position sync",
                                          f"Kraken {pos['side']} {pos.get('size')} — no Supabase bet. Closing.")
                        _close_side = "sell" if pos["side"] == "long" else "buy"
                        trade.create_order(
                            orderType="mkt", symbol=symbol, side=_close_side,
                            size=pos["size"], reduceOnly=True,
                        )
                        wait_for_position(symbol, want_open=False, retries=10, sleep_s=0.3)
                        pos = None  # position closed, proceed as flat
            except Exception as _sync_err:
                app.logger.warning(f"[FIX1B] Sync check failed (fail-open): {_sync_err}")
                # fail-open: proceed normally

        # ── Portfolio-Aware Decision Engine ────────────────────────────────────
        # Gather context for the Portfolio Engine (Supabase + Kraken)
        existing_bet_info = {}
        pyramid_count_existing = 1  # conservative default
        existing_entry_price = float(pos.get("price", 0) or 0) if pos else 0.0
        current_pnl_pct = 0.0
        _pe_wr_10 = 50.0
        _pe_streak_count = 0
        _pe_streak_dir = ""

        if pos:
            # Hard cap: count ALL open bets
            try:
                sb_url, sb_key = _sb_config()
                if sb_url and sb_key:
                    r_all = _sb_session.get(
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
                        app.logger.warning(f"[PE] Hard cap: {open_count} open bets — blocking")
                        return jsonify({
                            "status": "skipped",
                            "reason": f"MAX_OPEN_BETS reached ({open_count} open bets)",
                            "symbol": symbol, "direction": direction, "no_stack": True,
                        }), 200

                    # Fetch latest open bet info
                    r = _sb_session.get(
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
                        if row.get("entry_fill_price"):
                            existing_entry_price = float(row["entry_fill_price"])

                    # Fetch WR(10) and streak
                    r_perf = _sb_session.get(
                        f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                        "?select=correct&bet_taken=eq.true&correct=not.is.null"
                        "&order=id.desc&limit=10",
                        headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                        timeout=3,
                    )
                    if r_perf.status_code == 200 and r_perf.json():
                        resolved = r_perf.json()
                        wins = sum(1 for t in resolved if t.get("correct"))
                        _pe_wr_10 = round(wins / len(resolved) * 100, 1)
                        # Streak
                        for t in resolved:
                            _c = t.get("correct")
                            _s = "win" if _c else "loss"
                            if _pe_streak_count == 0:
                                _pe_streak_dir = _s
                                _pe_streak_count = 1
                            elif _s == _pe_streak_dir:
                                _pe_streak_count += 1
                            else:
                                break
            except Exception:
                pass  # fail-open: defaults will be used

            # Calculate PnL% (single mark price fetch, reused below)
            _pe_btc_price = _get_mark_price(symbol) or 0.0
            mark_price = _pe_btc_price or existing_entry_price
            if existing_entry_price > 0:
                _sign = 1 if pos["side"] == "long" else -1
                current_pnl_pct = (mark_price - existing_entry_price) / existing_entry_price * _sign

        # Get equity for risk score
        _pe_equity = float(os.environ.get("CAPITAL_USD") or os.environ.get("CAPITAL", 100))
        try:
            user = get_user_client()
            flex = user.get_wallets().get("accounts", {}).get("flex", {})
            _eq = flex.get("marginEquity") or flex.get("pv") or flex.get("portfolioValue")
            if _eq:
                _pe_equity = float(_eq)
        except Exception:
            pass

        if not pos:
            _pe_btc_price = _get_mark_price(symbol) or 0.0

        # Build portfolio state and evaluate signal
        _pe_decision = None
        _pe_state = None
        if not _portfolio_engine.disabled:
            try:
                _pe_state = _portfolio_engine.build_state(
                    position=pos,
                    equity=_pe_equity,
                    btc_price=_pe_btc_price,
                    regime=ace_result.get("regime", "UNKNOWN") if isinstance(ace_result, dict) else "UNKNOWN",
                    wr_10=_pe_wr_10,
                    streak_count=_pe_streak_count,
                    streak_direction=_pe_streak_dir,
                    existing_pnl_pct=current_pnl_pct,
                    existing_entry_price=existing_entry_price,
                    pyramid_count=pyramid_count_existing if pos else 0,
                )
                _pe_decision = _portfolio_engine.evaluate_signal(
                    portfolio=_pe_state,
                    direction=direction,
                    confidence=confidence,
                    xgb_prob_up=xgb_prob_up,
                    base_size=base_size,
                )
                app.logger.info(
                    f"[PE] action={_pe_decision.action} reason={_pe_decision.reason} "
                    f"size={_pe_decision.size} risk={_pe_state.risk_score:.0f} "
                    f"pnl={current_pnl_pct*100:.2f}%"
                )
            except Exception as _pe_err:
                app.logger.error(f"[PE] evaluate_signal failed, falling back to legacy: {_pe_err}")
                sentry_sdk.capture_exception(_pe_err)
                _pe_decision = None  # fallback to legacy

        # ── Legacy fallback (PE disabled or errored) ──────────────────────────
        if _pe_decision is None:
            if pos is None:
                _pe_decision = PortfolioDecision(action="OPEN", size=base_size, reason="legacy_flat")
            elif pos["side"] == desired_side:
                # Legacy pyramid logic
                _legacy_xgb = xgb_prob_up if direction == "UP" else (1.0 - xgb_prob_up)
                _legacy_pyr_size = max(0.001, base_size * 0.5)
                _legacy_can = (
                    pyramid_count_existing == 0
                    and (float(pos.get("size", 0)) + _legacy_pyr_size) <= 0.005
                    and ((_legacy_xgb > 0.70 and confidence > 0.72) or current_pnl_pct > 0.003)
                )
                if _legacy_can:
                    _pe_decision = PortfolioDecision(
                        action="PYRAMID", size=_legacy_pyr_size,
                        reason="legacy_pyramid", is_fallback=True,
                    )
                else:
                    _pe_decision = PortfolioDecision(action="SKIP", reason="legacy_same_dir_skip", is_fallback=True)
            else:
                # Legacy reverse logic
                _legacy_mark = _get_mark_price(symbol) or float(pos.get("price") or 0)
                _legacy_entry = float(pos.get("price") or 0)
                _legacy_profit = (
                    (_legacy_mark > _legacy_entry) if pos["side"] == "long"
                    else (_legacy_mark < _legacy_entry)
                ) if _legacy_entry > 0 and _legacy_mark > 0 else False
                if _legacy_profit:
                    _pe_decision = PortfolioDecision(action="SKIP", reason="legacy_preserve_profit", is_fallback=True)
                elif confidence >= 0.75:
                    _pe_decision = PortfolioDecision(
                        action="REVERSE", size=base_size,
                        close_size=float(pos.get("size", 0)),
                        reason="legacy_reverse", is_fallback=True,
                    )
                else:
                    _pe_decision = PortfolioDecision(action="SKIP", reason="legacy_low_conf_reverse", is_fallback=True)

        # ── Log decision to Supabase (best-effort) ────────────────────────────
        try:
            sb_url, sb_key = _sb_config()
            if sb_url and sb_key:
                _sb_session.post(
                    f"{sb_url}/rest/v1/bot_portfolio_decisions",
                    json={
                        "action": _pe_decision.action,
                        "reason": _pe_decision.reason[:200],
                        "confidence": round(confidence, 4),
                        "risk_score": round(_pe_state.risk_score, 1) if _pe_state and _pe_decision and not _pe_decision.is_fallback else None,
                        "portfolio_exposure_btc": round(_pe_state.total_exposure_btc, 6) if _pe_state and _pe_decision and not _pe_decision.is_fallback else None,
                        "unrealized_pnl_pct": round(current_pnl_pct, 6),
                        "position_direction": pos["side"] if pos else "flat",
                        "signal_direction": direction,
                        "size_decided": round(_pe_decision.size, 6),
                        "is_fallback": _pe_decision.is_fallback,
                    },
                    headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}",
                             "Content-Type": "application/json", "Prefer": "return=minimal"},
                    timeout=3,
                )
        except Exception as _pd_err:
            app.logger.warning(f"[PE] Portfolio decision log failed (non-blocking): {_pd_err}")

        # ── Execute decision ──────────────────────────────────────────────────

        # SKIP
        if _pe_decision.action == "SKIP":
            return jsonify({
                "status": "skipped",
                "reason": _pe_decision.reason,
                "symbol": symbol,
                "direction": direction,
                "confidence": confidence,
                "existing_position": pos,
                "no_stack": pos is not None and pos.get("side") == desired_side,
                "portfolio_decision": _pe_decision.to_dict(),
                **existing_bet_info,
            }), 200

        # PYRAMID
        if _pe_decision.action == "PYRAMID":
            pyramid_size = _pe_decision.size
            current_pos_size = float(pos.get("size", 0))
            try:
                _order_side = "buy" if direction == "UP" else "sell"
                pyramid_result = trade.create_order(
                    orderType="mkt",
                    symbol=symbol,
                    side=_order_side,
                    size=pyramid_size,
                )
                # UPDATE Supabase: pyramid_count + bet_size
                bet_id = existing_bet_info.get("existing_bet_id")
                if bet_id:
                    _sb_url, _sb_key = _sb_config()
                    try:
                        _patch_resp = _sb_session.patch(
                            f"{_sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}",
                            json={"pyramid_count": pyramid_count_existing + 1,
                                  "bet_size": round(current_pos_size + pyramid_size, 4)},
                            headers={"apikey": _sb_key, "Authorization": f"Bearer {_sb_key}",
                                     "Content-Type": "application/json", "Prefer": "return=minimal"},
                            timeout=5,
                        )
                        if _patch_resp.status_code not in (200, 204):
                            app.logger.error(f"[PE/pyramid] PATCH failed {_patch_resp.status_code}")
                            _push_cockpit_log("app", "error", "Pyramid PATCH failed",
                                              f"Status {_patch_resp.status_code}")
                    except Exception as _e:
                        app.logger.error(f"[PE/pyramid] PATCH exception: {_e}")
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
                    "pyramid_reason": _pe_decision.reason,
                    "order_id": _pyr_order_id,
                    "confidence": confidence,
                    "symbol": symbol,
                    "portfolio_decision": _pe_decision.to_dict(),
                }), 200
            except Exception:
                # Pyramid failed → skip
                return jsonify({
                    "status": "skipped",
                    "reason": "pyramid_order_failed",
                    "symbol": symbol, "direction": direction, "confidence": confidence,
                    "no_stack": True, **existing_bet_info,
                }), 200

        # REVERSE — close existing position, then fall through to OPEN
        if _pe_decision.action == "REVERSE":
            app.logger.info(
                f"[PE/reverse] Closing {pos['side']} position (reason={_pe_decision.reason})"
            )
            close_side = "sell" if pos["side"] == "long" else "buy"
            _funding_on_reverse = _get_funding_fee()
            try:
                trade.create_order(
                    orderType="mkt", symbol=symbol, side=close_side,
                    size=pos["size"], reduceOnly=True,
                )
            except Exception as _rev_err:
                app.logger.error(f"[PE/reverse] Close order FAILED: {_rev_err}")
                return jsonify({"status": "failed", "reason": "reverse_close_failed",
                                "error": str(_rev_err)}), 400
            _rev_pos = wait_for_position(symbol, want_open=False, retries=15, sleep_s=0.35)
            if _rev_pos is not None:
                app.logger.error("[PE/reverse] Position still open after close attempt")
                return jsonify({"status": "failed", "reason": "reverse_close_not_confirmed"}), 400
            exit_price_at_close = _get_mark_price(symbol) or _pe_btc_price
            _close_prev_bet_on_reverse(pos["side"], exit_price_at_close, pos["size"], _funding_on_reverse)
            time.sleep(2)
            size = _pe_decision.size  # use PE-decided size for new position

        # PARTIAL_CLOSE_AND_OPEN — close part of existing position, then open opposite
        if _pe_decision.action == "PARTIAL_CLOSE_AND_OPEN":
            app.logger.info(
                f"[PE/partial] Partial close {_pe_decision.close_size:.4f} of {pos['side']} "
                f"(reason={_pe_decision.reason})"
            )
            close_side = "sell" if pos["side"] == "long" else "buy"
            try:
                trade.create_order(
                    orderType="mkt", symbol=symbol, side=close_side,
                    size=_pe_decision.close_size, reduceOnly=True,
                )
            except Exception as _part_err:
                app.logger.error(f"[PE/partial] Close order FAILED: {_part_err}")
                return jsonify({"status": "failed", "reason": "partial_close_failed",
                                "error": str(_part_err)}), 400
            time.sleep(1)  # wait for partial fill
            size = _pe_decision.size  # reduced size for new opposite position

        # OPEN — standard new position (also reached after REVERSE/PARTIAL_CLOSE_AND_OPEN)
        if _pe_decision.action == "OPEN":
            size = _pe_decision.size

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
            _push_cockpit_log("app", "error", f"Order rejected: {send_status_type}",
                              f"Kraken rejected {direction} {size} {symbol}",
                              {"direction": direction, "size": size, "status": send_status_type})

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

                # [FIX2B] ATR-based SL/TP — scale ATR from 4h to prediction horizon
                _sl_fallback = float(os.environ.get("SL_PCT", "0.5"))
                _tp_fallback = float(os.environ.get("TP_PCT", "1.0"))
                sl_pct = _sl_fallback
                tp_pct = _tp_fallback
                if _regime_data and "error" not in _regime_data:
                    _atr_4h = _regime_data.get("atr_4h_pct", 0.0)
                    if _atr_4h > 0:
                        _scale = math.sqrt(PREDICTION_HORIZON_MINUTES / 240.0)
                        _atr_horizon = _atr_4h * _scale
                        sl_pct = max(0.3, min(1.5, _atr_horizon * 1.5))
                        tp_pct = sl_pct * 2
                        app.logger.info(
                            f"[FIX2B] ATR SL/TP: atr_4h={_atr_4h:.4f}%, scale={_scale:.3f}, "
                            f"atr_horizon={_atr_horizon:.4f}%, sl={sl_pct:.3f}%, tp={tp_pct:.3f}%"
                        )
                # Allow POST body override
                sl_pct = float(data.get("sl_pct", sl_pct))
                tp_pct = float(data.get("tp_pct", tp_pct))
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
                    else:
                        # [FIX1C] SL rejected — log explicitly
                        _sl_fail_status = sl_status.get("status", "unknown")
                        app.logger.error(
                            f"[FIX1C] SL order REJECTED: status={_sl_fail_status} "
                            f"direction={direction} sl_price={sl_price} size={size}"
                        )
                        _push_cockpit_log("app", "error", "SL order rejected",
                                          f"Status={_sl_fail_status}, sl_price={sl_price}, "
                                          f"direction={direction}, size={size}")
            except Exception as _sl_err:
                app.logger.error(f"[FIX1C] SL order exception: {_sl_err}")
                _push_cockpit_log("app", "error", "SL order exception", str(_sl_err))

        if ok:
            # [FIX3C] Update cooldown timestamp after successful trade
            with _TRADE_LOCK:
                _LAST_TRADE_PLACED_AT = time.time()

            _push_cockpit_log("app", "success", f"Bet placed: {direction} {symbol}",
                              f"Size={size}, fill={fill_price}, conf={confidence}",
                              {"direction": direction, "confidence": confidence, "size": size,
                               "fill_price": fill_price, "order_id": order_id})
            # Save price_drift_pct to Supabase (best-effort)
            if price_drift_pct > 0 and _sp_bet_id:
                try:
                    sb_url_d, sb_key_d = _sb_config()
                    if sb_url_d and sb_key_d:
                        _sb_session.patch(
                            f"{sb_url_d}/rest/v1/{SUPABASE_TABLE}?id=eq.{_sp_bet_id}",
                            json={"price_drift_pct": round(price_drift_pct, 6)},
                            headers={"apikey": sb_key_d, "Authorization": f"Bearer {sb_key_d}",
                                     "Content-Type": "application/json", "Prefer": "return=minimal"},
                            timeout=3)
                except Exception:
                    pass

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
            "price_drift_pct": round(price_drift_pct, 6),
            "portfolio_decision": _pe_decision.to_dict() if _pe_decision else None,
            "raw": result,
        }), (200 if ok else 400)

    except Exception as e:
        app.logger.exception("Endpoint error")
        _push_cockpit_log("app", "error", "Place bet FAILED", str(e),
                          {"direction": direction, "confidence": confidence})
        return jsonify({"status": "error", "error": "internal_error"}), 500

# ── DEBUG GEMINI ─────────────────────────────────────────────────────────────

@app.route("/debug-gemini", methods=["GET"])
def debug_gemini():
    """Diagnostic: list available Gemini models for the configured API key."""
    err = _check_read_key()
    if err:
        return err
    import requests as _r
    import certifi as _certifi
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        return jsonify({"error": "GEMINI_API_KEY not set", "key_prefix": ""}), 400
    results = {"key_prefix": key[:8] + "..."}
    for api_ver in ["v1", "v1beta"]:
        try:
            resp = _r.get(
                f"https://generativelanguage.googleapis.com/{api_ver}/models?key={key}",
                timeout=10, verify=_certifi.where(),
            )
            data = resp.json()
            models = [m["name"] for m in data.get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
            results[api_ver] = {"status": resp.status_code, "models": models[:20]}
        except Exception as e:
            results[api_ver] = {"error": str(e)}
    return jsonify(results)


# ── COUNCIL DELIBERATE ───────────────────────────────────────────────────────

@app.route("/council-deliberate", methods=["POST"])
def council_deliberate():
    """Run the AI Council deliberation round without executing a trade.

    Accepts the same JSON payload as /place-bet plus all market data fields.
    Returns: council decision, per-member votes, and agreement score.
    Set execute=true in the payload to forward approved decisions to /place-bet.
    Protected by BOT_API_KEY.
    """
    err = _check_api_key()
    if err:
        return err

    data = request.get_json(force=True) or {}
    direction = (data.get("direction") or "").upper()
    confidence = _safe_float(data.get("confidence", 0), default=0.0, min_v=0.0, max_v=1.0)

    if direction not in ("UP", "DOWN"):
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    current_hour_utc = time.gmtime().tm_hour
    xgb_prob_up, _ = _run_xgb_gate(direction, confidence, data, current_hour_utc)
    council_payload = {**data, "xgb_prob_up": xgb_prob_up}

    with _COUNCIL_LOCK:
        global _COUNCIL_DELIBERATING
        _COUNCIL_DELIBERATING = True
    votes = council_engine.run_round1(council_payload)
    result = council_engine.compute_weighted_vote(votes)

    signal_hash = hashlib.sha256(
        f"{direction}{confidence}{int(time.time() // 300)}".encode()
    ).hexdigest()[:16]
    council_engine.log_votes_async(votes, signal_hash)

    # Thoth Protocol — cache deliberation for /council-status
    _store_council_result(votes, result, signal_hash)

    return jsonify({
        "status": "ok",
        "signal_hash": signal_hash,
        "original_direction": direction,
        "original_confidence": confidence,
        "council_decision": result,
        "votes": votes,
        "council_mode_active": COUNCIL_MODE,
    })


# ── THOTH PROTOCOL — Council Status (sess.166) ───────────────────────────────

def _store_council_result(votes: list, result: dict, signal_hash: str):
    """Cache the latest council deliberation for /council-status."""
    global _COUNCIL_DELIBERATING
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
    safe_votes = []
    for v in votes:
        safe_votes.append({
            "member": v.get("member"),
            "direction": v.get("direction"),
            "confidence": round(float(v.get("confidence", 0)), 4),
            "reasoning": (v.get("reasoning") or "")[:100],
            "model_used": v.get("model_used"),
            "weight": v.get("weight"),
        })
    entry = {
        "timestamp": ts,
        "votes": safe_votes,
        "verdict": {
            "direction": result.get("direction"),
            "confidence": round(float(result.get("council_confidence", 0)), 4),
            "agreement": round(float(result.get("agreement_score", 0)), 4),
            "score": round(float(result.get("score", 0)), 4),
        },
        "signal_hash": signal_hash,
    }
    with _COUNCIL_LOCK:
        _COUNCIL_LAST.clear()
        _COUNCIL_LAST.update(entry)
        _COUNCIL_HISTORY.append(entry)
        # Ring buffer: keep last 9 (Tesla's number)
        while len(_COUNCIL_HISTORY) > 9:
            _COUNCIL_HISTORY.pop(0)
        _COUNCIL_DELIBERATING = False


@app.route("/council-status", methods=["GET"])
def council_status():
    """Read-only endpoint: last council deliberation + history (last 9).

    Public (no auth) — exposes only vote directions/confidence, no market data.
    Used by frontend councilTheater() for live deliberation visualization.
    """
    with _COUNCIL_LOCK:
        last = dict(_COUNCIL_LAST) if _COUNCIL_LAST else None
        history = list(_COUNCIL_HISTORY)
        deliberating = _COUNCIL_DELIBERATING
    return jsonify({
        "deliberating": deliberating,
        "last_deliberation": last,
        "history": history,
        "council_mode": COUNCIL_MODE,
    })


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



# ── PUBLIC STATS (homepage — no auth, cached 5min) ───────────────────────────

_public_stats_cache: dict = {}

@app.route("/public-stats", methods=["GET"])
def public_stats():
    """
    Public data for homepage: F&G, ghost WR, 24h BTC change, total signals.
    Cache 5min to avoid hammering alternative.me and Kraken on every pageview.
    """
    global _public_stats_cache
    now = time.time()
    if _public_stats_cache.get("ts", 0) > now - 300:
        return jsonify(_public_stats_cache["data"])

    result = {}

    # 1. Fear & Greed — alternative.me public API
    try:
        fg_resp = _ext_session.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=4
        )
        fg_entry = fg_resp.json().get("data", [{}])[0]
        result["fear_greed_value"] = int(fg_entry.get("value", 0))
        result["fear_greed_label"] = fg_entry.get("value_classification", "")
    except Exception:
        pass

    # 2. BTC 24h change — Kraken spot public ticker
    try:
        kr_resp = _kraken_session.get(
            "https://api.kraken.com/0/public/Ticker?pair=XXBTZUSD",
            timeout=4
        )
        kr_data = kr_resp.json().get("result", {}).get("XXBTZUSD", {})
        last  = float(kr_data.get("c", [0])[0] or 0)
        open_ = float(kr_data.get("o", 0) or 0)
        if last and open_:
            result["btc_change_24h"] = round((last - open_) / open_ * 100, 2)
    except Exception:
        pass

    # 3. Ghost WR last 20 — Supabase (anon key, read-only)
    # Colonne reali: confidence (non conf_score), ghost_correct (non correct)
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            ghost_url = (
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=ghost_correct,confidence"
                "&bet_taken=eq.false&ghost_correct=not.is.null"
                "&confidence=gte.0.60"
                "&order=created_at.desc&limit=20"
            )
            gh = _sb_session.get(
                ghost_url,
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                timeout=4
            ).json()
            if isinstance(gh, list) and gh:
                wins = sum(1 for r in gh if r.get("ghost_correct") is True)
                result["ghost_wr"] = round(wins / len(gh) * 100)
                result["ghost_n"]  = len(gh)
    except Exception:
        pass

    # 4. Total real bets — Supabase count header
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            count_resp = _sb_session.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=id&bet_taken=eq.true&correct=not.is.null"
                "&close_reason=neq.data_gap&limit=0",
                headers={
                    "apikey": sb_key,
                    "Authorization": f"Bearer {sb_key}",
                    "Prefer": "count=exact",
                },
                timeout=4
            )
            cr = count_resp.headers.get("content-range", "")
            total = cr.split("/")[-1] if "/" in cr else None
            if total and total.isdigit():
                result["total_bets"] = int(total)
    except Exception:
        pass

    with _CACHE_LOCK:
        _public_stats_cache = {"ts": now, "data": result}
    return jsonify(result)


# ── EXECUTION FEES ───────────────────────────────────────────────────────────

@app.route("/execution-fees", methods=["GET"])
def get_execution_fees():
    err = _check_read_key()
    if err:
        return err
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
                if ticker and pos["price"] > 0 and pos.get("size", 0) > 0:
                    mark = float(ticker.get("markPrice") or 0)
                    pos_sign = 1 if pos["side"] == "long" else -1
                    position_pnl = round((mark - pos["price"]) * pos_sign * pos["size"], 6)
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
            r_bets = _sb_session.get(
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
        res = _sb_session.get(url, headers=sb_headers, timeout=10)

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
            hist_res = _sb_session.get(hist_url, headers=sb_headers, timeout=10)
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
    err = _check_read_key()
    if err:
        return err
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
            "&close_reason=neq.data_gap"
            "&order=id.desc&limit=50"
        )
        res = _sb_session.get(url, headers={
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
                age_days = (time.time() - os.path.getmtime(ep_path)) / 86400
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
        tech_bias    = request.args.get("technical_bias", "").lower()

        _h2 = _safe_int(request.args.get("hour_utc", 12), default=12, min_v=0, max_v=23)
        _dow2 = _dt.datetime.now(_dt.timezone.utc).weekday()  # 0=Mon..6=Sun
        _session2 = 0 if _h2 < 8 else (1 if _h2 < 14 else 2)  # 0=Asia 1=London 2=NY
        _fg2 = _safe_float(request.args.get("fear_greed_value", 50), default=50.0, min_v=0.0, max_v=100.0)

        # P1: Regime di mercato 4h — calcolato live da Binance
        _regime_data = _compute_regime_4h_live()
        _regime_label = float(_regime_data["regime_label"])

        # Feature base (11 features — modelli precedenti al retrain P1)
        _feat_base = [
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
        ]

        # Aggiungi regime_label solo se il modello è stato addestrato con questa feature
        _model_features = list(getattr(_XGB_MODEL, "feature_names_in_", []))
        if _model_features and "regime_label" in _model_features:
            _feat_base.append(_regime_label)

        features = [_feat_base]

        prob = _XGB_MODEL.predict_proba(features)[0]  # [P(DOWN), P(UP)]
        xgb_dir = "UP" if prob[1] > prob[0] else "DOWN"
        agree = (xgb_dir == claude_dir) or (claude_dir in ("NO_BET", ""))

        return jsonify({
            "xgb_direction":  xgb_dir,
            "xgb_prob_up":    round(float(prob[1]), 3),
            "xgb_prob_down":  round(float(prob[0]), 3),
            "claude_direction": claude_dir,
            "agree":          agree,
            "regime":         _regime_data["regime_name"],
            "regime_label":   int(_regime_label),
        })

    except Exception as e:
        app.logger.error("predict_xgb error: %s", e)
        return jsonify({"xgb_direction": None, "agree": True, "reason": "internal_error"})


@app.route("/btc-regime", methods=["GET"])
def btc_regime():
    """
    Calcola il regime di mercato BTC corrente da Binance 4h klines.
    Returns: { regime_label, regime_name, atr_4h_pct, trend_strength }
      regime_label: 0=RANGING, 1=TRENDING, 2=VOLATILE
    """
    err = _check_api_key()
    if err:
        return err
    result = _compute_regime_4h_live()
    return jsonify(result)


# ── BET SIZING ───────────────────────────────────────────────────────────────

@app.route("/bet-sizing", methods=["GET"])
def bet_sizing():
    err = _check_read_key()
    if err:
        return err
    base_size  = _safe_float(request.args.get("base_size", 0.002),  default=0.002,  min_v=0.0001, max_v=0.1)
    confidence = _safe_float(request.args.get("confidence", 0.75), default=0.75,  min_v=0.0,    max_v=1.0)

    # Parametri aggiuntivi per XGBoost correctness model (opzionali, con default neutri)
    fear_greed = _safe_float(request.args.get("fear_greed_value", 50), default=50.0, min_v=0.0,   max_v=100.0)
    rsi14      = _safe_float(request.args.get("rsi14", 50),            default=50.0, min_v=0.0,   max_v=100.0)
    tech_score = _safe_float(request.args.get("technical_score", 0),   default=0.0,  min_v=-10.0, max_v=10.0)
    hour_utc   = _safe_int(request.args.get("hour_utc", time.gmtime().tm_hour), default=12, min_v=0, max_v=23)
    tech_bias   = request.args.get("technical_bias", "").lower()

    tech_bias_score    = float(_BIAS_MAP.get(tech_bias.strip(), 0))
    sig_fg_fear        = 1.0 if fear_greed < 45 else 0.0

    try:
        supabase_url, supabase_key = _sb_config()

        url = f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?select=correct,pnl_usd&bet_taken=eq.true&correct=not.is.null&order=id.desc&limit=10"
        res = _sb_session.get(url, headers={
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
                _dow3 = _dt.datetime.now(_dt.timezone.utc).weekday()  # 0=Mon..6=Sun
                _session3 = 0 if hour_utc < 8 else (1 if hour_utc < 14 else 2)
                feat_row = [[
                    confidence, fear_greed,
                    rsi14, tech_score,
                    math.sin(2 * math.pi * hour_utc / 24),  # hour_sin
                    math.cos(2 * math.pi * hour_utc / 24),  # hour_cos
                    tech_bias_score,                             # technical_bias_score
                    sig_fg_fear,                                 # signal_fg_fear
                    math.sin(2 * math.pi * _dow3 / 7),      # dow_sin
                    math.cos(2 * math.pi * _dow3 / 7),      # dow_cos
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
    Check orphaned bets (bet_taken=true, correct=null) and re-trigger wf02 for each.
    Call periodically from launchd every 5 minutes.
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
        r = _n8n_session.get(
            f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,direction,created_at,entry_fill_price,btc_price_entry,close_reason"
            "&bet_taken=eq.true&correct=is.null&order=created_at.asc",
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
        active_r = _n8n_session.get(
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
                    age_min = (_dt.datetime.now(_dt.timezone.utc) -
                               _dt.datetime.fromisoformat(started.replace("Z","+00:00"))).total_seconds() / 60
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
    # max_concurrent=1: previene batch-storm quando wf01A chiama rescue-orphaned
    # per ogni item (8 items × old max_concurrent=5 = 40 wf02 simultanei).
    # Il vecchio guard (status=waiting) era inutile: wf02 finisce in 4s.
    max_concurrent = 1
    triggered_count = 0
    RESCUE_WEBHOOK_URL = f"{n8n_url}/webhook/rescue-wf02"
    MAX_BET_HOURS = float(os.environ.get("MAX_BET_DURATION_HOURS", "4"))
    for bet in orphaned:
        bet_id = bet.get("id")

        # ── Stale bet path: risoluzione diretta senza wf02 ──────────────────
        bet_created = bet.get("created_at", "")
        try:
            created_dt = _dt.datetime.fromisoformat(
                bet_created.replace("Z", "+00:00")
            )
            age_hours = (_dt.datetime.now(_dt.timezone.utc) - created_dt).total_seconds() / 3600
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
                # Prezzo attuale: Kraken mark (primary) → Binance (fallback)
                exit_price = _get_mark_price(DEFAULT_SYMBOL) or 0.0
                if not exit_price:
                    try:
                        pr = _kraken_session.get(
                            "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                            timeout=4,
                        )
                        exit_price = float(pr.json()["price"]) if pr.ok else 0.0
                    except Exception:
                        pass
                if not exit_price:
                    exit_price = float(bet.get("entry_fill_price") or bet.get("btc_price_entry") or 0)
                entry = float(bet.get("entry_fill_price") or bet.get("btc_price_entry") or 0)
                direction = bet.get("direction", "UP")
                bet_size = float(bet.get("bet_size") or 0.001)
                _pnl = _calculate_pnl(entry, exit_price, bet_size, direction)
                correct = _pnl["correct"]
                # Aggiorna Supabase — &correct=is.null previene doppia risoluzione (S-17)
                upd = _sb_session.patch(
                    f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}&correct=is.null",
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal",
                    },
                    json={"exit_fill_price": exit_price, "correct": correct, "pnl_pct": _pnl["pnl_pct"],
                          "pnl_usd": _pnl["pnl_net"], "fees_total": _pnl["fee_usd"],
                          "actual_direction": _pnl["actual_direction"], "source_updated_by": "rescue_stale"},
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
            trig_r = _n8n_session.post(
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
    Auth: accetta BOT_API_KEY (X-API-Key) oppure COCKPIT_TOKEN (X-Cockpit-Token).
    """
    # Accept either BOT_API_KEY or COCKPIT_TOKEN — ghost eval is safe/read-write own data
    api_err = _check_api_key()
    cockpit_err = _check_cockpit_auth()
    if api_err is not None and cockpit_err is not None:
        return jsonify({"error": "Unauthorized"}), 401

    supabase_url, supabase_key = _sb_config()
    if not supabase_url or not supabase_key:
        return jsonify({"status": "error", "error": "Supabase not configured"}), 503

    now = _dt.datetime.now(_dt.timezone.utc)
    cutoff_recent = (now - _dt.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff_old = (now - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        resp = _sb_session.get(
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
        pnl_pct = (((exit_price - sp) / sp * 100) if direction == "UP" else ((sp - exit_price) / sp * 100)) if sp > 0 else 0.0

        try:
            upd = _sb_session.patch(
                f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{row_id}&ghost_evaluated_at=is.null",
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
                    "source_updated_by": "wf08",
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

    # ACE: trigger maybe_recalculate after ghost evaluation batch
    ace_recalc = False
    if evaluated:
        try:
            ace_recalc = _adaptive_engine.maybe_recalculate(trigger="ghost_batch")
        except Exception:
            pass

    app.logger.info(
        f"[ghost_evaluate] evaluated={len(evaluated)} errors={len(errors)}"
    )
    return jsonify({
        "status": "ok",
        "evaluated": len(evaluated),
        "message": f"Evaluated {len(evaluated)} ghost signals" if evaluated else "No signals evaluated in this batch",
        "errors": len(errors),
        "remaining": len(candidates) - batch_limit,
        "results": evaluated,
        "error_details": errors[:5],
        "ace_recalculated": ace_recalc,
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

    # Try Kraken Spot OHLC first (no georestriction)
    try:
        _kr = _kraken_session.get(
            f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1&since={target_unix}",
            timeout=8,
        )
        if _kr.ok:
            _kd = _kr.json()
            if not _kd.get("error"):
                _pair_key = next((k for k in _kd.get("result", {}) if k != "last"), None)
                if _pair_key and _kd["result"][_pair_key]:
                    return float(_kd["result"][_pair_key][0][4])  # close price
    except Exception as e:
        app.logger.warning(f"[ghost] Kraken OHLC exception: {e}")

    # Fallback: Binance (may 451 from Railway)
    try:
        r = _kraken_session.get(
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
        r2 = _ext_session.get(
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
    r = _sb_session.get(
        f"{sb_url}/rest/v1/{SUPABASE_TABLE}?classification=eq.SKIP&signal_price=is.null&btc_price_entry=not.is.null&select=id,btc_price_entry&order=id.asc",
        headers=headers, timeout=10
    )
    if not r.ok:
        return jsonify({"error": r.text[:200]}), 500
    rows = r.json()
    ok, err_list = 0, []
    for row in rows:
        upd = _sb_session.patch(
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
    Refresh CONF_CALIBRATION and DEAD_HOURS_UTC from live Supabase data.
    Called by launchd after each XGBoost retrain (POST to Railway URL).
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
    Trigger calibration refresh when bets >= 30.
    Rate limited: 1 request per hour. Does NOT run XGBoost training.
    Only refreshes in-memory confidence thresholds + dead hours from live Supabase data.
    """
    err = _check_read_key()
    if err:
        return err
    global _force_retrain_last
    now = time.time()
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
        "note": "Full XGBoost retrain runs daily at 03:00 UTC via n8n → /auto-retrain.",
    })


_AI_PREDICT_SYSTEM = (
    "You are a profitable Bitcoin short-term Automatic Prediction Trading Agent on 5-minute horizons. "
    "Each cycle evaluates multi-factor BTC/USD market data to predict UP or DOWN for a Kraken Futures perpetual trade on PF_XBTUSD. "
    "Be data-driven, avoid speculation, prioritize signal alignment and internal consistency.\n\n"
    "Think through all steps before producing the final JSON output.\n\n"
    "## STEP 0 — FORCE_NO_BET CHECK\n"
    "If force_no_bet = true: determine natural direction anyway, force confidence = 0.50, begin reasoning with 'STRUCTURAL OVERRIDE: [reason]'.\n\n"
    "## STEP 1 — TECHNICAL SCORE CEILING\n"
    "Read Technical Score from data. Let S = |score|.\n"
    "S <= 1.0 -> ceiling 0.62 | S 1.0-2.5 -> 0.65 | S 2.5-3.5 -> 0.65 | S 3.5-4.5 -> 0.72 | S > 4.5 -> 0.78\n"
    "Absolute max: 0.80.\n\n"
    "## STEP 2 — BASE SIGNAL WEIGHTS\n"
    "Technical Score (±7) — 40% | RSI14 — 10% | MACD — 10% | Derivatives (LS + Funding) — 13% | "
    "OI + Futures Volume — 12% | Taker Buy/Sell — 5% | MTF Consensus — 5% | News/Macro — 5%\n\n"
    "## DIRECTIONAL SYMMETRY RULES\n"
    "- EMA BULL -> UP unless contradicted | EMA BEAR -> DOWN unless contradicted | EMA NEUTRAL -> use MTF + taker\n"
    "- EMA opposes direction -> CAP 0.62\n"
    "- Derivatives are BIDIRECTIONAL: funding positive = bearish pressure, negative = bullish pressure\n"
    "- Only crypto-specific news has weight (10%). Generic macro = 0%.\n\n"
    "## CONFIDENCE CALIBRATION\n"
    "0.50-0.54 = conflicting | 0.55-0.62 = mild | 0.63-0.72 = clear (3+ agree) | 0.73-0.80 = strong convergence\n"
    "Use FULL range. Never default to 0.55.\n\n"
    "## KEY RULES\n"
    "- Doji pattern: -0.10 penalty\n"
    "- Thin volume: -0.15 penalty, CAP 0.61\n"
    "- Bollinger squeeze (<0.3%): -0.05\n"
    "- |score| >= 5 AND EMA opposes: -0.20\n"
    "- Capitulation Risk + crowd_long_contrarian: -0.20, CAP 0.55-0.59\n"
    "- Short Squeeze + crowd_short_contrarian: -0.20, CAP 0.55-0.59\n"
    "- < 3 categories align: -0.05\n"
    "- Final clamp: [0.50, 0.80]\n\n"
    "## ANTI-BIAS CHECK\n"
    "Before answering: Is direction data-driven or 'safe feeling'? Would flipping EMA flip your call?\n\n"
    "## OUTPUT\n"
    "Return SINGLE valid JSON. No markdown, no backticks.\n"
    '{"direction":"UP|DOWN","confidence":0.50-0.80,"reasoning":"min 30 chars",'
    '"signals":{"technical":"...","sentiment":"...","fear_greed":"...","volume":"...",'
    '"technical_bias_bullish":bool,"signal_fg_fear":bool},'
    '"telegram_message":"min 10 chars"}'
)


@app.route("/ai-predict-debug", methods=["POST"])
def ai_predict_debug():
    """Debug endpoint: returns received payload info without calling Claude."""
    err = _check_api_key()
    if err:
        return err
    raw = request.get_data(as_text=True)
    ct = request.content_type
    try:
        data = request.get_json(force=True) or {}
        keys = list(data.keys()) if isinstance(data, dict) else type(data).__name__
    except Exception as e:
        keys = f"parse_error: {e}"
    return jsonify({
        "content_type": ct,
        "raw_length": len(raw),
        "raw_preview": raw[:200],
        "parsed_type": type(data).__name__ if 'data' in dir() else "N/A",
        "keys": keys,
    })


@app.route("/ai-predict", methods=["POST"])
def ai_predict():
    """Run the AI prediction using Anthropic Claude (replaces n8n OpenRouter agent).

    Accepts the full market data JSON from n8n 01A/01B.
    Returns structured prediction: direction, confidence, reasoning, signals, telegram_message.
    """
    err = _check_api_key()
    if err:
        return err

    # Log incoming request for debugging
    _raw_body = request.get_data(as_text=True)
    try:
        data = request.get_json(force=True) or {}
    except Exception as _parse_err:
        app.logger.error(f"[AI_PREDICT] JSON parse error: {_parse_err}, raw={_raw_body[:200]}")
        return jsonify({"status": "error", "error": f"json_parse: {_parse_err}", "raw_preview": _raw_body[:200]}), 400

    app.logger.info(f"[AI_PREDICT] body_len={len(_raw_body)} keys={list(data.keys())[:10] if isinstance(data, dict) else type(data).__name__} mark_price={data.get('mark_price','missing') if isinstance(data, dict) else 'N/A'}")

    # Build the user message from market data (same fields n8n passes)
    lines = []
    lines.append(f"Current timestamp: {_dt.datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC")
    lines.append(f"\nBITCOIN LIVE PRICE: {data.get('mark_price', 'n/a')} USD")

    # Fear & Greed
    fg_data = data.get('data', [{}])
    if isinstance(fg_data, list) and fg_data:
        lines.append(f"Fear & Greed: {fg_data[0].get('value', 'n/a')} — {fg_data[0].get('value_classification', 'n/a')}")
    elif data.get('fear_greed_value'):
        lines.append(f"Fear & Greed: {data.get('fear_greed_value', 'n/a')}")

    # Derivatives
    lines.append(f"\nDERIVATIVES: Funding={data.get('funding_rate', 'n/a')} ({data.get('funding_signal', 'n/a')})")
    lines.append(f"L/S Ratio: {data.get('ls_ratio', 'n/a')} ({data.get('ls_bias', 'n/a')})")
    lines.append(f"Derivatives Summary: {data.get('derivatives_summary', 'n/a')}")

    # Candles
    for i in range(1, 4):
        o = data.get(f'candle_{i}_open', 'n/a')
        c = data.get(f'candle_{i}_close', 'n/a')
        v = data.get(f'candle_{i}_volume', 'n/a')
        lines.append(f"Candle {i}: open={o} close={c} vol={v}")
    lines.append(f"Momentum: {data.get('candle_momentum', 'n/a')}")
    lines.append(f"Candle Summary: {data.get('candle_summary', 'n/a')}")

    # Technical indicators
    ind = data.get('indicators', {})
    lines.append(f"\nTECHNICAL INDICATORS:")
    lines.append(f"RSI14: {ind.get('rsi14', data.get('rsi14', 'n/a'))} ({ind.get('rsi_signal', 'n/a')})")
    lines.append(f"EMA9/21/50: {ind.get('ema9', 'n/a')}/{ind.get('ema21', 'n/a')}/{ind.get('ema50', 'n/a')}")
    lines.append(f"EMA Trend: {ind.get('ema_trend', 'n/a')}")
    macd = ind.get('macd', {})
    if isinstance(macd, dict):
        lines.append(f"MACD: {macd.get('macd', 'n/a')} Signal: {macd.get('signal', 'n/a')} Hist: {macd.get('histogram', 'n/a')} Bias: {macd.get('bias', 'n/a')}")
    bb = ind.get('bollinger', {})
    if isinstance(bb, dict):
        lines.append(f"Bollinger: upper={bb.get('upper', 'n/a')} mid={bb.get('middle', 'n/a')} lower={bb.get('lower', 'n/a')} width={bb.get('width_pct', 'n/a')}%")
    lines.append(f"VWAP: {ind.get('vwap', 'n/a')} ({ind.get('vwap_position', 'n/a')})")
    lines.append(f"Volume: spike={ind.get('volume_spike', 'n/a')} ratio={ind.get('volume_ratio', 'n/a')}x state={ind.get('volume_state', 'n/a')}")
    lines.append(f"Candle Pattern: {ind.get('candle_pattern', 'n/a')}")
    lines.append(f"Support/Resistance: {ind.get('support', 'n/a')}/{ind.get('resistance', 'n/a')}")
    lines.append(f"ATR14: {data.get('atr14', 'n/a')} | ADX14: {data.get('adx14', 'n/a')} ({data.get('adx_signal', 'n/a')})")

    # Technical score
    ts = data.get('technical_score', {})
    if isinstance(ts, dict):
        lines.append(f"\nTechnical Score: {ts.get('value', 'n/a')} (max ±7) | {ts.get('bullish_votes', 'n/a')} bull / {ts.get('bearish_votes', 'n/a')} bear")
        lines.append(f"Technical Bias: {ts.get('bias', 'n/a')}")
        lines.append(f"Force LOW-CONFIDENCE: {ts.get('force_no_bet', False)} {ts.get('force_no_bet_reason', '')}")

    # Anchor values
    lines.append(f"\nANCHOR VALUES:")
    lines.append(f"Technical Score Anchor: {ts.get('value', 'n/a') if isinstance(ts, dict) else 'n/a'}")
    lines.append(f"OI Price Signal Anchor: {data.get('oi_price_signal', 'n/a')}")
    lines.append(f"LS Bias Anchor: {data.get('ls_bias', 'n/a')}")

    # Market regime
    lines.append(f"\nMarket Regime: {data.get('market_regime', 'n/a')} — {data.get('regime_note', 'n/a')}")

    # Order book
    lines.append(f"\nOrder Book: {data.get('order_book_summary', 'n/a')}")

    # Taker
    lines.append(f"\nTaker Buy: {data.get('taker_buy_pct', 'n/a')}% | Sell: {data.get('taker_sell_pct', 'n/a')}% | Signal: {data.get('taker_signal', 'n/a')}")

    # OI
    lines.append(f"\nOI Signal: {data.get('oi_signal', 'n/a')} | OI+Price: {data.get('oi_price_signal', 'n/a')}")

    # MTF
    mtf = data.get('mtf', {})
    if isinstance(mtf, dict):
        for tf_key, tf_label in [('tf15m', '15m'), ('tf4h', '4h')]:
            tf = mtf.get(tf_key, {})
            if isinstance(tf, dict):
                lines.append(f"{tf_label}: {tf.get('trend', 'UNKNOWN')} EMA9={tf.get('ema9', 'n/a')} EMA21={tf.get('ema21', 'n/a')} RSI={tf.get('rsi14', 'n/a')}")
        lines.append(f"MTF Consensus: {mtf.get('consensus', 'UNKNOWN')}")

    # News (truncated)
    for news_key in ['cnbc_news', 'coindesk_news', 'news_cryptocompare', 'sole24ore_news']:
        news_items = data.get(news_key, [])
        if isinstance(news_items, list) and news_items:
            label = news_key.upper().replace('_', ' ')
            lines.append(f"\n{label}:")
            for item in news_items[:10]:
                if isinstance(item, dict):
                    lines.append(f"  [{item.get('source', 'n/a')}] {item.get('title', 'n/a')} ({item.get('date', 'n/a')})")

    # Performance memory
    lines.append(f"\nPerformance Memory: {data.get('pattern_memory', 'n/a')}")
    lines.append(f"Live Calibration: {data.get('perf_stats_text', 'n/a')}")

    user_message = "\n".join(lines)

    _anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _anthropic_key:
        app.logger.error("[AI_PREDICT] ANTHROPIC_API_KEY not set")
        return jsonify({"status": "error", "error": "anthropic_key_missing"}), 503

    try:
        import anthropic
        import httpx
        import certifi as _certifi_ai
        client = anthropic.Anthropic(
            api_key=_anthropic_key,
            http_client=httpx.Client(verify=_certifi_ai.where()),
        )
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            temperature=0.3,
            system=_AI_PREDICT_SYSTEM,
            messages=[{"role": "user", "content": user_message}],
            timeout=30.0,
        )
        raw_text = msg.content[0].text if msg.content else ""

        # Parse JSON from response
        import re as _re
        parsed = {}
        raw_text_stripped = raw_text.strip()
        try:
            parsed = json.loads(raw_text_stripped)
        except (json.JSONDecodeError, ValueError):
            m = _re.search(r'\{[\s\S]+\}', raw_text_stripped)
            if m:
                try:
                    parsed = json.loads(m.group(0))
                except (json.JSONDecodeError, ValueError):
                    pass

        if not parsed or "direction" not in parsed:
            return jsonify({"status": "error", "error": "invalid_ai_response", "raw": raw_text[:500]}), 500

        # Wrap in "output" key to match n8n $json.output.* references
        return jsonify({
            "output": {
                "direction": parsed.get("direction", "").upper(),
                "confidence": max(0.50, min(0.80, float(parsed.get("confidence", 0.55)))),
                "reasoning": parsed.get("reasoning", ""),
                "signals": parsed.get("signals", {}),
                "telegram_message": parsed.get("telegram_message", ""),
            }
        })

    except SystemExit as e:
        app.logger.error(f"[AI_PREDICT] SystemExit caught (gunicorn shutdown mid-request): code={e.code}")
        return jsonify({"status": "error", "error": "worker_shutdown", "code": str(e.code)}), 503
    except Exception as e:
        import traceback
        app.logger.error(f"[AI_PREDICT] error: {e}\n{traceback.format_exc()}")
        return jsonify({"status": "error", "error": str(e)[:200], "trace": traceback.format_exc()[-300:]}), 500


_auto_retrain_last: float = 0.0
_AUTO_RETRAIN_COOLDOWN = 6 * 3600  # 6 hours

@app.route("/auto-retrain", methods=["POST"])
def auto_retrain():
    """
    Full retrain pipeline: build_dataset --include-ghost → train_xgboost → hot-reload models.
    Rate limited: once per 6 hours. Runs in background thread.
    Called daily by n8n scheduler.
    """
    err = _check_api_key()
    if err:
        return err

    global _auto_retrain_last
    now = time.time()
    elapsed = now - _auto_retrain_last
    if elapsed < _AUTO_RETRAIN_COOLDOWN:
        remaining = int(_AUTO_RETRAIN_COOLDOWN - elapsed)
        return jsonify({
            "ok": False, "error": "rate_limited",
            "message": f"Retrain already ran recently. Retry in {remaining // 3600}h {(remaining % 3600) // 60}m.",
            "cooldown_remaining": remaining,
        }), 429

    _auto_retrain_last = now
    import subprocess, threading as _threading

    base = os.path.dirname(__file__)

    def _run_retrain():
        try:
            # Step 1: build dataset with ghost signals
            app.logger.info("[RETRAIN] Step 1/3: building dataset...")
            r1 = subprocess.run(
                ["python3", "build_dataset.py", "--include-ghost"],
                cwd=base, capture_output=True, text=True, timeout=120,
            )
            if r1.returncode != 0:
                app.logger.error(f"[RETRAIN] build_dataset failed: {r1.stderr[-500:]}")
                return

            # Step 2: train XGBoost
            csv_path = os.path.join(base, "datasets", "features.csv")
            app.logger.info("[RETRAIN] Step 2/3: training XGBoost...")
            r2 = subprocess.run(
                ["python3", "train_xgboost.py", "--data", csv_path],
                cwd=base, capture_output=True, text=True, timeout=300,
            )
            if r2.returncode != 0:
                app.logger.error(f"[RETRAIN] train_xgboost failed: {r2.stderr[-500:]}")
                return

            # Step 3: hot-reload models in memory
            app.logger.info("[RETRAIN] Step 3/3: hot-reloading models...")
            _load_xgb_model()
            global _xgb_correctness
            corr_path = os.path.join(base, "models", "xgb_correctness.pkl")
            if os.path.exists(corr_path):
                temp_corr = joblib_load(corr_path)
                with _model_lock:
                    _xgb_correctness = temp_corr

            # Refresh calibration thresholds
            refresh_calibration()
            refresh_dead_hours()

            # Refresh adaptive engine
            _adaptive_engine.recalculate(trigger="retrain")

            app.logger.info("[RETRAIN] Pipeline completed successfully")

        except subprocess.TimeoutExpired:
            app.logger.error("[RETRAIN] Pipeline timed out")
        except Exception as e:
            app.logger.error(f"[RETRAIN] Pipeline error: {e}")

    _threading.Thread(target=_run_retrain, daemon=True).start()
    return jsonify({
        "ok": True,
        "message": "Retrain pipeline started: build_dataset → train_xgboost → hot-reload.",
        "cooldown": _AUTO_RETRAIN_COOLDOWN,
    })


@app.route("/costs", methods=["GET"])
def costs():
    err = _check_read_key()
    if err:
        return err
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
        res = _kraken_session.get(url, headers=sb_headers, timeout=5)
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
        res = _sb_session.get(url, headers=sb_headers, timeout=5)
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
        url = f"{sb_url}/rest/v1/{SUPABASE_TABLE}?select=id&limit=0"
        res = _sb_session.get(url, headers={**sb_headers, "Prefer": "count=exact", "Range": "0-0"}, timeout=5)
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
                r = _n8n_session.get(
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
               f"?select=id&created_at=gte.{month_start}T00:00:00&limit=0")
        res = _sb_session.get(url, headers={**sb_headers, "Prefer": "count=exact", "Range": "0-0"}, timeout=5)
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
        r = _sb_session.get(
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
        r = _sb_session.get(
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
            r = _n8n_session.get(
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
        r = _sb_session.get(
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
            r = _n8n_session.get(
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
        r = _sb_session.get(
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
        r = _sb_session.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,created_at,direction,btc_price_entry,bet_size"
            "&bet_taken=eq.true&correct=is.null&entry_fill_price=not.is.null&order=id.desc&limit=20",
            headers=sb_headers,
            timeout=6,
        )
        rows = r.json() if r.ok else []
    except Exception as e:
        return jsonify({"error": f"Supabase: {e}"}), 500

    now = _dt.datetime.now(_dt.timezone.utc)
    result = []
    for row in rows:
        minutes_open = 0
        try:
            created = _dt.datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
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
        r = _sb_session.get(
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

    if bet.get("btc_price_entry") is None or bet.get("bet_size") is None:
        return jsonify({"error": "bet missing entry_price or bet_size"}), 400
    entry_price = float(bet["btc_price_entry"])
    bet_size = float(bet["bet_size"])
    direction = bet["direction"]

    # 2. Calculate fields via unified PnL function
    _pnl = _calculate_pnl(entry_price, exit_price, bet_size, direction)

    correct = body.get("correct")
    if correct is None:
        correct = _pnl["correct"]
    else:
        correct = bool(correct)

    # 3. PATCH Supabase
    patch_data = {
        "btc_price_exit": exit_price,
        "actual_direction": _pnl["actual_direction"],
        "pnl_usd": round(_pnl["pnl_net"], 4),
        "pnl_pct": _pnl["pnl_pct"],
        "fees_total": _pnl["fee_usd"],
        "correct": correct,
        "close_reason": "manual_backfill",
        "source_updated_by": "manual_backfill",
    }
    try:
        pr = _sb_session.patch(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}&correct=is.null",
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
            wf_r = _n8n_session.get(
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
            ex_r = _n8n_session.get(
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
_CONTRIBUTION_LOCK = threading.Lock()
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
        r = _ext_session.post(
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
        return False  # fail closed on network error — block suspicious requests


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
    with _CONTRIBUTION_LOCK:
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
        return jsonify({"ok": False, "error": "Anti-bot check failed. Please reload the page."}), 400

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
        r = _sb_session.post(
            f"{supabase_url}/rest/v1/contributions",
            json=payload, headers=headers, timeout=8,
        )
        if r.status_code not in (200, 201):
            return jsonify({"ok": False, "error": "Save error"}), 500
        saved = r.json()
        contrib_id = saved[0]["id"] if saved else "?"
    except Exception as e:
        return jsonify({"ok": False, "error": "Database error"}), 500

    # ── Build approve/reject URLs (token HMAC, non espone BOT_API_KEY) ──
    base_url     = os.environ.get("RAILWAY_URL", "https://btcpredictor.io")
    approve_url  = f"{base_url}/approve-contribution/{contrib_id}?token={_make_contribution_token(contrib_id, 'approve')}"
    reject_url   = f"{base_url}/reject-contribution/{contrib_id}?token={_make_contribution_token(contrib_id, 'reject')}"
    owner_email  = os.environ.get("OWNER_EMAIL", "")

    # ── Telegram notification (best-effort) — usa BTC Sentinel (private alerts) ──
    telegram_token = os.environ.get("TELEGRAM_PRIVATE_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    telegram_owner = os.environ.get("TELEGRAM_OWNER_ID", "")
    if telegram_token and telegram_owner:
        try:
            msg = (
                f"📥 *New contribution \\#{contrib_id}*\n\n"
                f"*Role*: {_CONTRIBUTION_ROLES.get(role, role)}\n\n"
                f"*Insight*:\n_{insight[:300]}_\n\n"
                f"[✅ Approve]({approve_url}) · [❌ Reject]({reject_url})"
            )
            _tg_session.post(
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
        _n8n_session.post(
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

    return jsonify({"ok": True, "message": "Contribution received — will be published after review. Thank you!"})


@app.route("/contribute", methods=["POST"])
def contribute_alias():
    """Alias for /submit-contribution — used by investors.html."""
    return submit_contribution()


@app.route("/public-contributions", methods=["GET"])
def public_contributions():
    """Return approved contributions — role + insight + month/year only. Zero personal data."""
    supabase_url, supabase_key = _sb_config()
    if not supabase_url or not supabase_key:
        return jsonify([])
    try:
        r = _sb_session.get(
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


@app.route("/pine-script", methods=["GET"])
def pine_script_ghost_bets():
    """Generate a ready-to-paste Pine Script v5 indicator with all ghost bets as overlay.
    Query params: limit (default 500), min_conf (default 0).
    Returns plain text Pine Script code.
    """
    limit    = min(int(request.args.get("limit", 500)), 1000)
    min_conf = float(request.args.get("min_conf", 0))

    supabase_url, supabase_key = _sb_config()
    if not supabase_url or not supabase_key:
        return "// Error: Supabase not configured\n", 500

    try:
        r = _sb_session.get(
            f"{supabase_url}/rest/v1/btc_predictions"
            f"?bet_taken=eq.false&ghost_exit_price=not.is.null"
            f"&order=created_at.asc&limit={limit}"
            f"&select=created_at,direction,confidence,ghost_correct",
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        ghosts = [g for g in r.json() if float(g.get("confidence") or 0) >= min_conf]
    except Exception as e:
        return f"// Error fetching data: {e}\n", 500

    def to_ms(iso):
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)

    ts_arr   = ", ".join(str(to_ms(g["created_at"])) for g in ghosts)
    dir_arr  = ", ".join("1" if g["direction"] == "UP" else "-1" for g in ghosts)
    conf_arr = ", ".join(str(int(float(g.get("confidence", 0)) * 100)) for g in ghosts)
    ok_arr   = ", ".join("1" if g.get("ghost_correct") else "0" for g in ghosts)

    import datetime as _dt2
    generated_at = _dt2.datetime.now(_dt2.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    code = f"""//@version=5
// ─────────────────────────────────────────────────────────────────────────────
// BTC Predictor — Ghost Bets Overlay  |  {len(ghosts)} segnali  |  {generated_at}
// Segnali valutati ma NON eseguiti dal bot (ACE skip, XGB skip, dead hour, ecc.)
// Teal  = direzione corretta  |  Rosso = direzione sbagliata
// ▲ = UP signal  |  ▼ = DOWN signal
// Aggiorna: https://web-production-e27d0.up.railway.app/pine-script
// ─────────────────────────────────────────────────────────────────────────────
indicator("Ghost Bets — BTC Predictor", overlay=true, max_labels_count=500)

var int[]  _ts   = array.from({ts_arr})
var int[]  _dir  = array.from({dir_arr})
var int[]  _conf = array.from({conf_arr})
var int[]  _ok   = array.from({ok_arr})

show_wins   = input.bool(true,  "Mostra WIN",         group="Filtri")
show_losses = input.bool(true,  "Mostra LOSS",        group="Filtri")
show_up     = input.bool(true,  "Mostra UP",          group="Filtri")
show_down   = input.bool(true,  "Mostra DOWN",        group="Filtri")
min_conf    = input.int(50,     "Confidenza min %",   minval=0, maxval=100, group="Filtri")

tf_ms = timeframe.in_seconds() * 1000

for i = 0 to array.size(_ts) - 1
    ts   = array.get(_ts,   i)
    dir  = array.get(_dir,  i)
    conf = array.get(_conf, i)
    win  = array.get(_ok,   i)

    if ts >= time and ts < time + tf_ms and conf >= min_conf
        is_up  = dir == 1
        is_win = win == 1

        if (is_win and not show_wins) or (not is_win and not show_losses)
            continue
        if (is_up and not show_up) or (not is_up and not show_down)
            continue

        col  = is_win ? color.new(color.teal, 15) : color.new(color.red, 15)
        txt  = (is_up ? "▲" : "▼") + " " + str.tostring(conf) + "%"
        stl  = is_up ? label.style_label_down : label.style_label_up
        ypos = is_up ? high * 1.0006 : low * 0.9994

        label.new(bar_index, ypos, txt,
                  color=col, textcolor=color.white,
                  style=stl, size=size.small)
"""
    return code, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/pine-script/sync", methods=["POST"])
def pine_script_sync():
    """Update GitHub Gist with fresh ghost bets Pine Script. Called by n8n every 6h."""
    err = _check_api_key()
    if err:
        return err

    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gist_id  = os.environ.get("GIST_ID", "")
    if not gh_token or not gist_id:
        return jsonify({"error": "GITHUB_TOKEN or GIST_ID not configured"}), 500

    supabase_url, supabase_key = _sb_config()
    try:
        r = _sb_session.get(
            f"{supabase_url}/rest/v1/btc_predictions"
            "?bet_taken=eq.false&ghost_exit_price=not.is.null"
            "&order=created_at.asc&limit=1000"
            "&select=created_at,direction,confidence,ghost_correct",
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        ghosts = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    def _to_ms(iso):
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)

    ts_arr   = ", ".join(str(_to_ms(g["created_at"])) for g in ghosts)
    dir_arr  = ", ".join("1" if g["direction"] == "UP" else "-1" for g in ghosts)
    conf_arr = ", ".join(str(int(float(g.get("confidence", 0)) * 100)) for g in ghosts)
    ok_arr   = ", ".join("1" if g.get("ghost_correct") else "0" for g in ghosts)
    wins     = sum(1 for g in ghosts if g.get("ghost_correct"))
    wr       = round(wins / len(ghosts) * 100, 1) if ghosts else 0
    now_str  = _dt.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    pine = f"""//@version=5
// BTC Predictor — Ghost Bets Overlay | {len(ghosts)} segnali | Ghost WR: {wr}% | {now_str}
// Teal=corretto Rosso=sbagliato | Aggiornato ogni 6h via n8n
indicator("Ghost Bets — BTC Predictor", overlay=true, max_labels_count=500)
var int[]  _ts   = array.from({ts_arr})
var int[]  _dir  = array.from({dir_arr})
var int[]  _conf = array.from({conf_arr})
var int[]  _ok   = array.from({ok_arr})
show_wins=input.bool(true,"WIN",group="Filtri"),show_losses=input.bool(true,"LOSS",group="Filtri")
show_up=input.bool(true,"UP",group="Filtri"),show_down=input.bool(true,"DOWN",group="Filtri")
min_conf=input.int(50,"Conf min %",minval=0,maxval=100,group="Filtri")
tf_ms=timeframe.in_seconds()*1000
for i=0 to array.size(_ts)-1
    ts=array.get(_ts,i),dir=array.get(_dir,i),conf=array.get(_conf,i),win=array.get(_ok,i)
    if ts>=time and ts<time+tf_ms and conf>=min_conf
        is_up=dir==1,is_win=win==1
        if (is_win and not show_wins) or (not is_win and not show_losses) or (is_up and not show_up) or (not is_up and not show_down)
            continue
        label.new(bar_index,is_up?high*1.0006:low*0.9994,(is_up?"▲":"▼")+" "+str.tostring(conf)+"%",color=is_win?color.new(color.teal,15):color.new(color.red,15),textcolor=color.white,style=is_up?label.style_label_down:label.style_label_up,size=size.small)"""

    try:
        gh_r = _ext_session.patch(
            f"https://api.github.com/gists/{gist_id}",
            json={"files": {"ghost_bets_overlay.pine": {"content": pine}}},
            headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
            timeout=10,
        )
        gh_r.raise_for_status()
        gist_data = gh_r.json()
        raw_url  = gist_data["files"]["ghost_bets_overlay.pine"]["raw_url"]
        gist_url = gist_data["html_url"]
    except Exception as e:
        return jsonify({"error": f"GitHub update failed: {e}"}), 500

    _push_cockpit_log("pine_script", "success", "Ghost bets Pine Script aggiornato",
                      f"{len(ghosts)} ghost bets | WR {wr}%")
    return jsonify({"updated_at": now_str, "count": len(ghosts), "ghost_wr": wr,
                    "gist_url": gist_url, "raw_url": raw_url})


@app.route("/pine-script/page")
def pine_script_page():
    """Webpage with always-fresh Pine Script + one-click copy."""
    supabase_url, supabase_key = _sb_config()
    try:
        r = _sb_session.get(
            f"{supabase_url}/rest/v1/btc_predictions"
            "?bet_taken=eq.false&ghost_exit_price=not.is.null"
            "&order=created_at.asc&limit=1000"
            "&select=created_at,direction,confidence,ghost_correct",
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=10,
        )
        ghosts = r.json()
    except Exception:
        ghosts = []

    def _to_ms(iso):
        dt = _dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)

    ts_arr   = ", ".join(str(_to_ms(g["created_at"])) for g in ghosts)
    dir_arr  = ", ".join("1" if g["direction"] == "UP" else "-1" for g in ghosts)
    conf_arr = ", ".join(str(int(float(g.get("confidence", 0)) * 100)) for g in ghosts)
    ok_arr   = ", ".join("1" if g.get("ghost_correct") else "0" for g in ghosts)
    wins     = sum(1 for g in ghosts if g.get("ghost_correct"))
    wr       = round(wins / len(ghosts) * 100, 1) if ghosts else 0
    now_str  = _dt.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    gist_id  = os.environ.get("GIST_ID", "")
    gist_url = f"https://gist.github.com/mattiacalastri/{gist_id}" if gist_id else "#"
    wr_color = "#3fb950" if wr >= 50 else "#f85149"

    pine = f"""//@version=5
// BTC Predictor — Ghost Bets Overlay | {len(ghosts)} segnali | Ghost WR: {wr}% | {now_str}
// Teal=corretto  Rosso=sbagliato  |  ▲=UP  ▼=DOWN
indicator("Ghost Bets — BTC Predictor", overlay=true, max_labels_count=500)
var int[]  _ts   = array.from({ts_arr})
var int[]  _dir  = array.from({dir_arr})
var int[]  _conf = array.from({conf_arr})
var int[]  _ok   = array.from({ok_arr})
show_wins=input.bool(true,"WIN",group="Filtri"),show_losses=input.bool(true,"LOSS",group="Filtri")
show_up=input.bool(true,"UP",group="Filtri"),show_down=input.bool(true,"DOWN",group="Filtri")
min_conf=input.int(50,"Conf min %",minval=0,maxval=100,group="Filtri")
tf_ms=timeframe.in_seconds()*1000
for i=0 to array.size(_ts)-1
    ts=array.get(_ts,i),dir=array.get(_dir,i),conf=array.get(_conf,i),win=array.get(_ok,i)
    if ts>=time and ts<time+tf_ms and conf>=min_conf
        is_up=dir==1,is_win=win==1
        if (is_win and not show_wins) or (not is_win and not show_losses) or (is_up and not show_up) or (not is_up and not show_down)
            continue
        label.new(bar_index,is_up?high*1.0006:low*0.9994,(is_up?"▲":"▼")+" "+str.tostring(conf)+"%",color=is_win?color.new(color.teal,15):color.new(color.red,15),textcolor=color.white,style=is_up?label.style_label_down:label.style_label_up,size=size.small)"""

    from flask import Response as _Resp
    html = f"""<!DOCTYPE html><html lang="it"><head><meta charset="UTF-8">
<title>Ghost Bets Pine Script</title>
<style>*{{box-sizing:border-box;margin:0;padding:0}}body{{background:#0d1117;color:#e6edf3;font-family:'SF Mono',monospace;padding:24px}}
h1{{font-size:18px;color:#58a6ff;margin-bottom:10px}}.stats{{display:flex;gap:24px;margin-bottom:14px;font-size:13px;color:#8b949e}}
.stats span{{color:#e6edf3;font-weight:600}}textarea{{width:100%;height:calc(100vh - 170px);background:#161b22;color:#c9d1d9;
border:1px solid #30363d;border-radius:6px;padding:14px;font-size:11.5px;line-height:1.5;resize:none;outline:none}}
.row{{display:flex;gap:12px;margin-top:10px;align-items:center}}button{{padding:9px 18px;border:none;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600}}
.cp{{background:#238636;color:#fff}}.cp:hover{{background:#2ea043}}.cp.ok{{background:#1f6feb}}
.gh{{background:#21262d;color:#58a6ff;border:1px solid #30363d}}.gh:hover{{background:#30363d}}
small{{color:#6e7681;font-size:11px}}</style></head><body>
<h1>Ghost Bets Overlay — Pine Script v5</h1>
<div class="stats">
  <div>Segnali: <span>{len(ghosts)}</span></div>
  <div>Ghost WR: <span style="color:{wr_color}">{wr}%</span></div>
  <div>Generato: <span>{now_str}</span></div>
</div>
<textarea id="p" readonly>{pine}</textarea>
<div class="row">
  <button class="cp" onclick="cp()">Copia codice</button>
  <a href="{gist_url}" target="_blank"><button class="gh">Gist GitHub</button></a>
  <small>Pine Script Editor → New → Cmd+A → Cmd+V → Add to chart</small>
</div>
<script>function cp(){{const t=document.getElementById('p');t.select();
navigator.clipboard.writeText(t.value).then(()=>{{const b=document.querySelector('.cp');
b.textContent='Copiato ✓';b.classList.add('ok');setTimeout(()=>{{b.textContent='Copia codice';b.classList.remove('ok')}},2000)}});}}</script>
</body></html>"""
    return _Resp(html, mimetype="text/html")


@app.route("/approve-contribution/<int:contrib_id>", methods=["GET"])
def approve_contribution(contrib_id):
    """Owner-only: approve a contribution. Called via link in Telegram."""
    token = request.args.get("token", "")
    if not _valid_contribution_token(token, contrib_id, "approve"):
        return jsonify({"error": "Unauthorized"}), 401
    supabase_url, supabase_key = _sb_config()
    try:
        r = _sb_session.patch(
            f"{supabase_url}/rest/v1/contributions?id=eq.{contrib_id}",
            json={"approved": True},
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}",
                     "Content-Type": "application/json"},
            timeout=8,
        )
        if r.ok:
            return jsonify({"ok": True, "message": f"Contribution #{contrib_id} approved and published."})
        return jsonify({"ok": False, "error": "Approval error"}), 500
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
        r = _sb_session.delete(
            f"{supabase_url}/rest/v1/contributions?id=eq.{contrib_id}",
            headers={"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"},
            timeout=8,
        )
        if r.ok:
            return jsonify({"ok": True, "message": f"Contribution #{contrib_id} rejected and removed."})
        return jsonify({"ok": False, "error": "Rejection error"}), 500
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
            txt_ts = open(report_path, encoding="utf-8").read()
            m_ts = _re.search(
                r"Generated:\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})",
                txt_ts,
            )
            if m_ts:
                last_retrain_ts = _dt.datetime.strptime(
                    m_ts.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=_dt.timezone.utc)
        except Exception:
            pass
    if last_retrain_ts is None and os.path.exists(model_path):
        mtime = os.path.getmtime(model_path)
        last_retrain_ts = _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc)
    if last_retrain_ts is not None:
        last_retrain_iso = last_retrain_ts.strftime("%Y-%m-%d %H:%M UTC")

    # Parse accuracy from xgb_report.txt
    direction_acc = None
    train_n = None
    if os.path.exists(report_path):
        try:
            txt = open(report_path, encoding="utf-8").read()
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
            r = _sb_session.get(
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

    # Next scheduled retrain: daily at 03:00 UTC via n8n → /auto-retrain
    now_utc = _dt.datetime.now(_dt.timezone.utc)
    next_retrain_dt = now_utc.replace(hour=3, minute=0, second=0, microsecond=0)
    if now_utc.hour >= 3:
        next_retrain_dt += _dt.timedelta(days=1)
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
        last_cal_iso = _dt.datetime.fromtimestamp(_force_retrain_last, tz=_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        elapsed = _dt.datetime.now(_dt.timezone.utc).timestamp() - _force_retrain_last
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
        "confidence_threshold": float(os.environ.get("CONF_THRESHOLD", "0.62")),
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
        res = _sb_session.get(url, headers={
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
        threshold = float(os.environ.get("CONF_THRESHOLD", "0.62"))
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
    Read most recent row from trading_stats table on Supabase
    and return data as JSON.
    """
    try:
        supabase_url, supabase_key = _sb_config()

        if not supabase_url or not supabase_key:
            return jsonify({"error": "Supabase credentials not configured"}), 500

        url = f"{supabase_url}/rest/v1/trading_stats?select=*&limit=1"
        res = _sb_session.get(url, headers={
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
    """Fetch ForexFactory calendar with 1h cache.
    Returns {"data": [...], "fetch_failed": bool}.
    fetch_failed=True if network failed and no cache available.
    """
    global _macro_cache
    now_ts = time.time()
    if _macro_cache["data"] is not None and (now_ts - _macro_cache["ts"]) < _MACRO_CACHE_TTL:
        return {"data": _macro_cache["data"], "fetch_failed": False}
    try:
        r = _ext_session.get(_MACRO_CALENDAR_URL, timeout=5)
        if r.ok:
            data = r.json()
            with _CACHE_LOCK:
                _macro_cache = {"data": data, "ts": now_ts}
            return {"data": data, "fetch_failed": False}
    except Exception:
        pass
    # Return stale cache if available (better than nothing)
    cached = _macro_cache["data"]
    return {"data": cached or [], "fetch_failed": cached is None}


@app.route("/macro-guard", methods=["GET"])
def macro_guard():
    """Check if high-impact USD macro events are coming in the next 2h.

    Response:
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

_POLYGON_RPC_FALLBACKS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc-mainnet.maticvigil.com",
]


def _get_web3_contract():
    """Return (w3, contract, account) or raise RuntimeError if not configured.
    Tries primary RPC from env, then falls back through public RPCs."""
    try:
        from web3 import Web3
        from web3.middleware import geth_poa_middleware
    except ImportError:
        raise RuntimeError("web3 not installed")

    private_key = os.environ.get("POLYGON_PRIVATE_KEY", "")
    contract_address = os.environ.get("POLYGON_CONTRACT_ADDRESS", "")
    if not private_key or not contract_address:
        raise RuntimeError("POLYGON_PRIVATE_KEY o POLYGON_CONTRACT_ADDRESS non configurati")

    primary_rpc = os.environ.get("POLYGON_RPC_URL", "")
    rpc_list = ([primary_rpc] if primary_rpc else []) + _POLYGON_RPC_FALLBACKS

    w3 = None
    for rpc_url in rpc_list:
        try:
            candidate = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            candidate.middleware_onion.inject(geth_poa_middleware, layer=0)
            if candidate.is_connected():
                w3 = candidate
                break
        except Exception:
            continue

    if w3 is None:
        w3 = Web3(Web3.HTTPProvider(rpc_list[0], request_kwargs={"timeout": 10}))
        w3.middleware_onion.inject(geth_poa_middleware, layer=0)

    account = w3.eth.account.from_key(private_key)
    contract = w3.eth.contract(
        address=Web3.to_checksum_address(contract_address),
        abi=_BTCBOT_AUDIT_ABI
    )
    return w3, contract, account


_onchain_nonce_lock = threading.Lock()


_NONCE_ERRORS = ("replacement transaction underpriced", "nonce too low", "already known")


def _send_onchain_tx(w3, account, tx_built, label="", max_retries=3):
    """Sign, send and wait for receipt. Fail-open: returns tx_hex or None on failure.
    Receipt wait reduced to 8s to stay within Railway's 30s request timeout.
    The tx is already broadcast after send_raw_transaction — receipt is just confirmation.
    Nonce is refreshed inside the lock to prevent race conditions."""
    import time as _time
    tx_hex = None
    for attempt in range(max_retries):
        try:
            with _onchain_nonce_lock:
                tx_built["nonce"] = w3.eth.get_transaction_count(account.address, "pending")
                signed = account.sign_transaction(tx_built)
                tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            tx_hex = tx_hash.hex()
            break
        except Exception as e:
            err_lower = str(e).lower()
            if attempt < max_retries - 1 and any(ne in err_lower for ne in _NONCE_ERRORS):
                app.logger.warning(f"[ONCHAIN] {label} nonce conflict (attempt {attempt + 1}/{max_retries}), retrying...")
                _time.sleep(2)
                continue
            app.logger.error(f"[ONCHAIN] {label} sign/send FAILED: {type(e).__name__}: {e}")
            return None
    try:
        w3.eth.wait_for_transaction_receipt(tx_hash, timeout=8)
    except Exception as e:
        app.logger.warning(f"[ONCHAIN] {label} receipt timeout for tx {tx_hex} (tx already broadcast): {e}")
    return tx_hex


@app.route("/commit-prediction", methods=["POST"])
def commit_prediction():
    """
    Commit prediction hash to Polygon.
    Body JSON: { bet_id, direction, confidence, entry_price, bet_size, timestamp }
    Saves onchain_commit_hash + onchain_commit_tx to Supabase.
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

        tx = contract.functions.commit(bet_id, commit_hash).build_transaction({
            "from": account.address,
            "nonce": 0,  # refreshed inside _send_onchain_tx under lock
            "gas": 120_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "chainId": 137,
        })
        tx_hex = _send_onchain_tx(w3, account, tx, label=f"commit #{bet_id}")
        if tx_hex is None:
            return jsonify({"ok": False, "error": "onchain_tx_failed", "detail": "sign/send failed — check logs"}), 502

        commit_hash_hex = commit_hash.hex()

        app.logger.info(f"[ONCHAIN] commit bet #{bet_id} → tx {tx_hex}")

        # Aggiorna Supabase — errore non critico (tx già inviata on-chain)
        sb_warning = None
        try:
            _supabase_update(bet_id, {
                "onchain_commit_hash": commit_hash_hex,
                "onchain_commit_tx": tx_hex,
            })
            # Verify the update actually persisted
            sb_url, sb_key = _sb_config()
            _vr = _sb_session.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}&select=onchain_commit_tx",
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                timeout=5,
            )
            _rows = _vr.json() if _vr.ok else []
            if not _rows or not _rows[0].get("onchain_commit_tx"):
                sb_warning = "tx sent but Supabase verification failed — onchain_commit_tx still NULL"
                app.logger.error(f"[ONCHAIN] bet #{bet_id}: {sb_warning}")
                _push_cockpit_log("app", "error", f"Commit hash NULL for bet #{bet_id}",
                                  sb_warning, {"bet_id": bet_id, "tx": tx_hex})
        except Exception as sb_err:
            sb_warning = "tx sent but Supabase update failed"
            app.logger.error(f"[ONCHAIN] Supabase update failed for bet #{bet_id}: {sb_err}")
            _push_cockpit_log("app", "error", f"Commit Supabase write failed #{bet_id}",
                              str(sb_err), {"bet_id": bet_id, "tx": tx_hex})

        resp = {"ok": True, "commit_hash": commit_hash_hex, "tx": tx_hex}
        if sb_warning:
            resp["warning"] = sb_warning
        return jsonify(resp)

    except Exception as e:
        app.logger.error(f"[ONCHAIN] commit_prediction error: {type(e).__name__}: {e}")
        _push_cockpit_log("app", "error", "On-chain commit failed", str(e), {"bet_id": bet_id})
        return jsonify({"ok": False, "error": "internal_error"}), 500


@app.route("/resolve-prediction", methods=["POST"])
def resolve_prediction():
    """
    Resolve bet outcome hash on Polygon.
    Body JSON: { bet_id, exit_price, pnl_usd, won, close_timestamp }
    Saves onchain_resolve_hash + onchain_resolve_tx to Supabase.
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

        tx = contract.functions.resolve(bet_id, resolve_hash, won).build_transaction({
            "from": account.address,
            "nonce": 0,  # refreshed inside _send_onchain_tx under lock
            "gas": 120_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "chainId": 137,
        })
        tx_hex = _send_onchain_tx(w3, account, tx, label=f"resolve #{bet_id}")
        if tx_hex is None:
            return jsonify({"ok": False, "error": "onchain_tx_failed", "detail": "sign/send failed — check logs"}), 502

        resolve_hash_hex = resolve_hash.hex()

        app.logger.info(f"[ONCHAIN] resolve bet #{bet_id} won={won} → tx {tx_hex}")

        # Aggiorna Supabase — errore non critico (tx già inviata on-chain)
        sb_warning = None
        try:
            _supabase_update(bet_id, {
                "onchain_resolve_hash": resolve_hash_hex,
                "onchain_resolve_tx": tx_hex,
            })
            # Verify the update actually persisted
            sb_url, sb_key = _sb_config()
            _vr = _sb_session.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}&select=onchain_resolve_tx",
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                timeout=5,
            )
            _rows = _vr.json() if _vr.ok else []
            if not _rows or not _rows[0].get("onchain_resolve_tx"):
                sb_warning = "tx sent but Supabase verification failed — onchain_resolve_tx still NULL"
                app.logger.error(f"[ONCHAIN] bet #{bet_id}: {sb_warning}")
                _push_cockpit_log("app", "error", f"Resolve hash NULL for bet #{bet_id}",
                                  sb_warning, {"bet_id": bet_id, "tx": tx_hex})
        except Exception as sb_err:
            sb_warning = "tx sent but Supabase update failed"
            app.logger.error(f"[ONCHAIN] Supabase update failed for bet #{bet_id}: {sb_err}")
            _push_cockpit_log("app", "error", f"Resolve Supabase write failed #{bet_id}",
                              str(sb_err), {"bet_id": bet_id, "tx": tx_hex})

        resp = {"ok": True, "resolve_hash": resolve_hash_hex, "tx": tx_hex}
        if sb_warning:
            resp["warning"] = sb_warning
        return jsonify(resp)

    except Exception as e:
        app.logger.error(f"[ONCHAIN] resolve_prediction error: {type(e).__name__}: {e}")
        _push_cockpit_log("app", "error", "On-chain resolve failed", str(e), {"bet_id": bet_id})
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
        tx = contract.functions.commit(onchain_id, commit_hash).build_transaction({
            "from": account.address,
            "nonce": 0,  # refreshed inside _send_onchain_tx under lock
            "gas": 120_000, "gasPrice": w3.to_wei("30", "gwei"), "chainId": 137,
        })
        tx_hex = _send_onchain_tx(w3, account, tx, label=f"inputs id={onchain_id}")
        if tx_hex is None:
            return jsonify({"ok": False, "error": "onchain_tx_failed", "detail": "sign/send failed — check logs"}), 502

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
        tx = contract.functions.commit(onchain_id, commit_hash).build_transaction({
            "from": account.address,
            "nonce": 0,  # refreshed inside _send_onchain_tx under lock
            "gas": 120_000, "gasPrice": w3.to_wei("30", "gwei"), "chainId": 137,
        })
        tx_hex = _send_onchain_tx(w3, account, tx, label=f"fill bet #{bet_id}")
        if tx_hex is None:
            return jsonify({"ok": False, "error": "onchain_tx_failed", "detail": "sign/send failed — check logs"}), 502

        app.logger.info(f"[ONCHAIN] fill bet #{bet_id} price={fill_price} → tx {tx_hex}")

        try:
            _supabase_update(bet_id, {"onchain_fill_tx": tx_hex})
        except Exception as sb_err:
            app.logger.error(f"[ONCHAIN] Supabase update failed: {sb_err}")
            return jsonify({"ok": True, "tx": tx_hex, "warning": "tx sent but Supabase update failed"})

        return jsonify({"ok": True, "tx": tx_hex, "onchain_id": onchain_id})

    except Exception as e:
        app.logger.error(f"[ONCHAIN] commit_fill error: {type(e).__name__}: {e}")
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
        tx = contract.functions.commit(onchain_id, commit_hash).build_transaction({
            "from": account.address,
            "nonce": 0,  # refreshed inside _send_onchain_tx under lock
            "gas": 120_000, "gasPrice": w3.to_wei("30", "gwei"), "chainId": 137,
        })
        tx_hex = _send_onchain_tx(w3, account, tx, label=f"stops bet #{bet_id}")
        if tx_hex is None:
            return jsonify({"ok": False, "error": "onchain_tx_failed", "detail": "sign/send failed — check logs"}), 502

        app.logger.info(f"[ONCHAIN] stops bet #{bet_id} sl={sl_price} tp={tp_price} → tx {tx_hex}")

        try:
            _supabase_update(bet_id, {"onchain_stops_tx": tx_hex})
        except Exception as sb_err:
            app.logger.error(f"[ONCHAIN] Supabase update failed: {sb_err}")
            return jsonify({"ok": True, "tx": tx_hex, "warning": "tx sent but Supabase update failed"})

        return jsonify({"ok": True, "tx": tx_hex, "onchain_id": onchain_id})

    except Exception as e:
        app.logger.error(f"[ONCHAIN] commit_stops error: {type(e).__name__}: {e}")
        return jsonify({"ok": False, "error": "internal_error"}), 500


def _supabase_update(bet_id: int, fields: dict, *, only_if_unresolved: bool = False):
    """Helper: aggiorna una riga Supabase per bet_id.
    only_if_unresolved: adds &correct=is.null guard (optimistic locking)."""
    sb_url, sb_key = _sb_config()
    url = f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}"
    if only_if_unresolved:
        url += "&correct=is.null"
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    r = _sb_session.patch(url, json=fields, headers=headers, timeout=10)
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
            ts_nfc = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
    except Exception:
        ts_nfc = int(_dt.datetime.now(_dt.timezone.utc).timestamp())

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
        resp = _sb_session.post(
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
        if resp.ok and resp.json():
            news_id = resp.json()[0]["id"]
        elif not resp.ok:
            app.logger.warning(f"[NEWS-FC] Supabase insert failed: {resp.status_code}")
    except Exception as e:
        app.logger.warning(f"[NEWS-FC] Supabase insert error: {e}")

    onchain_id = 50_000_000 + (news_id or 0)

    # Commit su Polygon
    try:
        w3, contract, account = _get_web3_contract()
        commit_hash_bytes = hashlib.sha256(raw_nfc).digest()  # 32 bytes
        tx = contract.functions.commit(onchain_id, commit_hash_bytes).build_transaction({
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address, "pending"),
            "gas": 120_000,
            "gasPrice": w3.to_wei("30", "gwei"),
            "chainId": 137,
        })
        tx_hex = _send_onchain_tx(w3, account, tx, label=f"news_fc id={news_id}")
        if tx_hex is None:
            return jsonify({"ok": False, "error": "onchain_tx_failed", "detail": "sign/send failed — check logs"}), 502
        polygonscan_url = f"https://polygonscan.com/tx/{tx_hex}"
        app.logger.info(f"[NEWS-FC] id={news_id} onchain_id={onchain_id} → tx {tx_hex}")

        # Aggiorna Supabase con tx
        if news_id:
            try:
                _sb_session.patch(
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
                _sb_session.patch(
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
    img_path = _os.path.join(_os.path.dirname(__file__), "static", "og-image.png")
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
        "# AI crawlers — explicit allow\n"
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "User-agent: ClaudeBot\n"
        "Allow: /\n"
        "User-agent: PerplexityBot\n"
        "Allow: /\n"
        "User-agent: Applebot-Extended\n"
        "Allow: /\n"
        "User-agent: Google-Extended\n"
        "Allow: /\n"
        "User-agent: Bytespider\n"
        "Allow: /\n"
        "User-agent: CCBot\n"
        "Allow: /\n"
        "User-agent: cohere-ai\n"
        "Allow: /\n"
        "\n"
        "Sitemap: https://btcpredictor.io/sitemap.xml\n"
        "\n"
        "# AI agent discovery (non-standard, informational)\n"
        "# LLMs: https://btcpredictor.io/llms.txt\n"
        "# AgentProfile: https://btcpredictor.io/agent.json\n"
        "# AgentGuide: https://btcpredictor.io/AGENTS.md\n"
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
        ("https://btcpredictor.io/investors",              "monthly", "0.9", "2026-03-01"),
        ("https://btcpredictor.io/council",                "monthly", "0.8", "2026-03-01"),
        ("https://btcpredictor.io/audit",                  "weekly",  "0.9", today),
        ("https://btcpredictor.io/contributors",           "weekly",  "0.7", "2026-03-01"),
        ("https://btcpredictor.io/xgboost-spiegato",       "monthly", "0.8", "2026-02-27"),
        ("https://btcpredictor.io/aureo",                  "monthly", "0.7", "2026-03-01"),
        ("https://btcpredictor.io/legal",                  "monthly", "0.3", "2026-03-01"),
        ("https://btcpredictor.io/support",                "weekly",  "0.8", today),
        ("https://btcpredictor.io/privacy",                "monthly", "0.3", "2026-03-01"),
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
  <div class="meta">BTC Predictor · btcpredictor.io · Last updated: 2026-03-01</div>

  <h2>1. LEGAL NOTICE (AVVISO LEGALE)</h2>
  <p>This website and its services are operated by <strong>Mattia Calastri</strong>, founder of BTC Predictor (hereinafter "we", "us", "the operator").<br>
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
    <li><strong>Google reCAPTCHA v3</strong> — Provider: Google LLC. Purpose: invisible anti-bot protection on all public forms. Collects: IP address, browser/device fingerprint, interaction data. Loaded on pages with forms. <a href="https://policies.google.com/privacy" target="_blank" rel="noopener">Google Privacy Policy ↗</a></li>
    <li><strong>Cloudflare Turnstile</strong> — Provider: Cloudflare, Inc. Purpose: invisible CAPTCHA fallback on the Satoshi widget. Collects: IP address, device data. <a href="https://www.cloudflare.com/privacypolicy/" target="_blank" rel="noopener">Cloudflare Privacy Policy ↗</a></li>
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
  <p>The source code of BTC Predictor is released under the <strong>MIT License</strong>. You are free to use, modify, and distribute it subject to the license terms. The "BTC Predictor" name and associated branding remain the property of the operator.</p>

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
    html = _read_page("home.html")
    if _GOOGLE_SITE_VERIFICATION:
        meta = f'<meta name="google-site-verification" content="{_GOOGLE_SITE_VERIFICATION}">'
        html = html.replace("</head>", meta + "\n</head>", 1)
    return html, 200, {"Content-Type": "text/html"}


@app.route("/manifesto", methods=["GET"])
def manifesto():
    return _read_page("manifesto.html"), 200, {"Content-Type": "text/html"}


@app.route("/prevedibilita-perfetta", methods=["GET"])
def prevedibilita():
    return _read_page("prevedibilita.html"), 200, {"Content-Type": "text/html"}


@app.route("/investors", methods=["GET"])
def investors():
    return _read_page("investors.html"), 200, {"Content-Type": "text/html"}


@app.route("/aureo", methods=["GET"])
def aureo():
    return _read_page("aureo.html"), 200, {"Content-Type": "text/html"}


@app.route("/contributors", methods=["GET"])
def contributors():
    return _read_page("contributors.html"), 200, {"Content-Type": "text/html"}


@app.route("/council", methods=["GET"])
def council():
    return _read_page("council.html"), 200, {"Content-Type": "text/html"}


@app.route("/support", methods=["GET"])
def support():
    return _read_page("support.html"), 200, {"Content-Type": "text/html"}


_EMAIL_RE = re.compile(r'^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$')

@app.route("/satoshi-lead", methods=["POST"])
def satoshi_lead():
    """Save email collected from Satoshi widget to Supabase leads."""
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
                ts_resp = _ext_session.post(
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
        resp = _sb_session.post(
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
    return _read_page("xgboost.html"), 200, {"Content-Type": "text/html"}


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
    """Aggregate RSS crypto news — cache 10 min, NO auth required."""
    import xml.etree.ElementTree as ET
    import email.utils

    global _news_cache
    now = time.time()
    if _news_cache["data"] is not None and now - _news_cache["ts"] < 600:
        return jsonify({"items": _news_cache["data"], "cached": True})

    items = []
    for source, url in _NEWS_FEEDS:
        try:
            resp = _sb_session.get(url, timeout=6,
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
    Uses select=* for RLS compatibility (same pattern as /signals).
    """

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
        r = _sb_session.get(url, headers={
            "apikey": sb_key,
            "Authorization": f"Bearer {sb_key}",
        }, timeout=8)
        if not r.ok:
            # Fallback: query senza colonne on-chain per diagnostica
            url_fb = (f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                      f"?select=id,direction,confidence,correct,pnl_usd,created_at"
                      f"&bet_taken=eq.true&order=id.desc&limit=30")
            r_fb = _sb_session.get(url_fb, headers={
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
        integrity_score = 0.0

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
    # Marketing ops merged into /cockpit — redirect permanently
    from flask import redirect
    return redirect("/cockpit", code=302)


@app.route("/marketing-stats", methods=["GET"])
def marketing_stats():
    """Public/marketing data — NO auth required."""
    import re as _re

    result = {}

    # ── 1. Telegram member count ──────────────────────────────────
    # getChatMemberCount funziona con username pubblico @BTCPredictorBot
    # anche se il bot non è admin del canale (confermato 2026-02-28)
    try:
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if tg_token:
            r = _tg_session.get(
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
        published_in_site = wallet_addr[:12] in _read_page("index.html")
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
        idx = _read_page("index.html")
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
            txt = open(report_path, encoding="utf-8").read()
            m = _re.search(r"Generated:\s*(\d{4}-\d{2}-\d{2})", txt)
            if m:
                last_retrain = m.group(1)
        except Exception:
            pass
    if not last_retrain and os.path.exists(model_path):
        try:
            mtime = os.path.getmtime(model_path)
            last_retrain = _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            pass
    result["last_retrain"] = last_retrain

    # ── 5. Bet stats for retrain window ──────────────────────────
    try:
        sb_url, sb_key = _sb_config()
        _headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}", "Prefer": "count=exact"}
        r_clean = _sb_session.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}",
            headers=_headers,
            params={"bet_taken": "eq.true", "correct": "not.is.null", "select": "id", "limit": "0"},
            timeout=5,
        )
        clean_bets = int(r_clean.headers.get("content-range", "*/0").split("/")[-1])
        r_wins = _sb_session.get(
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
    return _read_page("privacy.html"), 200, {"Content-Type": "text/html"}


@app.route("/audit", methods=["GET"])
def audit_page():
    return _read_page("audit.html"), 200, {"Content-Type": "text/html"}


@app.route("/api/audit", methods=["GET"])
def api_audit():
    """Public audit ledger API — returns trade history with on-chain proof."""
    sb_url, sb_key = _sb_config()
    if not sb_url or not sb_key:
        return jsonify({"error": "no_supabase"}), 500

    try:
        page = max(1, int(request.args.get("page", 1)))
    except (ValueError, TypeError):
        page = 1
    try:
        limit = max(1, min(int(request.args.get("limit", 50)), 200))
    except (ValueError, TypeError):
        limit = 50

    direction = request.args.get("direction", "").strip().upper()
    correct = request.args.get("correct", "").strip().lower()
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    # Sanitize date params to prevent PostgREST injection (YYYY-MM-DD only)
    import re as _re_audit
    _date_re = _re_audit.compile(r"^\d{4}-\d{2}-\d{2}$")
    if date_from and not _date_re.match(date_from):
        date_from = ""
    if date_to and not _date_re.match(date_to):
        date_to = ""

    offset = (page - 1) * limit

    select_cols = (
        "id,direction,confidence,correct,pnl_usd,created_at,"
        "signal_price,exit_price,onchain_commit_tx,onchain_resolve_tx"
    )
    url = (
        f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
        f"?select={select_cols}"
        f"&bet_taken=eq.true"
        f"&order=id.desc"
        f"&limit={limit}&offset={offset}"
    )

    if direction in ("UP", "DOWN"):
        url += f"&direction=eq.{direction}"
    if correct == "true":
        url += "&correct=eq.true"
    elif correct == "false":
        url += "&correct=eq.false"
    if date_from:
        url += f"&created_at=gte.{date_from}T00:00:00Z"
    if date_to:
        url += f"&created_at=lte.{date_to}T23:59:59Z"

    sb_headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Prefer": "count=exact",
    }
    try:
        res = _sb_session.get(url, headers=sb_headers, timeout=10)
        if not res.ok:
            return jsonify({"error": f"supabase_{res.status_code}"}), 502

        data = res.json()
        total_count = 0
        cr = res.headers.get("Content-Range", "")
        if "/" in cr:
            try:
                total_count = int(cr.split("/")[1])
            except (ValueError, IndexError):
                total_count = len(data)
        else:
            total_count = len(data)

        stats = {}
        try:
            count_headers = {**sb_headers, "Prefer": "count=exact"}
            stat_base = f"{sb_url}/rest/v1/{SUPABASE_TABLE}?select=id&bet_taken=eq.true"
            if direction in ("UP", "DOWN"):
                stat_base += f"&direction=eq.{direction}"
            if date_from:
                stat_base += f"&created_at=gte.{date_from}T00:00:00Z"
            if date_to:
                stat_base += f"&created_at=lte.{date_to}T23:59:59Z"

            r_total = _sb_session.get(stat_base + "&limit=0", headers=count_headers, timeout=5)
            stat_total = int(r_total.headers.get("content-range", "*/0").split("/")[-1])

            r_wins = _sb_session.get(stat_base + "&correct=eq.true&limit=0", headers=count_headers, timeout=5)
            stat_wins = int(r_wins.headers.get("content-range", "*/0").split("/")[-1])

            r_closed = _sb_session.get(stat_base + "&correct=not.is.null&limit=0", headers=count_headers, timeout=5)
            stat_closed = int(r_closed.headers.get("content-range", "*/0").split("/")[-1])

            r_sample = _sb_session.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                f"?select=confidence,onchain_commit_tx&bet_taken=eq.true&order=id.desc&limit=2000",
                headers=sb_headers, timeout=8,
            )
            sample = r_sample.json() if r_sample.ok else []
            confs = [r["confidence"] for r in sample if r.get("confidence") is not None]
            onchain_count = sum(1 for r in sample if r.get("onchain_commit_tx"))

            stats = {
                "total": stat_total,
                "win_rate": round(100.0 * stat_wins / stat_closed, 1) if stat_closed > 0 else None,
                "avg_confidence": round(sum(confs) / len(confs), 4) if confs else None,
                "onchain_rate": round(100.0 * onchain_count / len(sample), 1) if sample else None,
            }
        except Exception:
            stats = {}

        return jsonify({
            "data": data,
            "total_count": total_count,
            "page": page,
            "limit": limit,
            "stats": stats,
        })
    except Exception as e:
        app.logger.exception("api_audit error")
        return jsonify({"error": "internal_error"}), 500


_CACHE_BUST = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "1")[:8]

@app.route("/dashboard", methods=["GET"])
def dashboard():
    # Use the actual request host so API calls always go same-origin.
    # This avoids CORS issues and DNS propagation problems when accessed
    # via a custom domain (e.g. btcpredictor.io vs railway.app).
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    railway_url = f"{scheme}://{request.host}"
    html = _read_page("index.html")
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
    return _read_page("404.html"), 404, {"Content-Type": "text/html"}


# ── COCKPIT — Private Command Center ────────────────────────────────────────
# Secure dashboard for Mattia to monitor AI agents, bot status, and system health.
# Auth: stateless via X-Cockpit-Token header (no Flask sessions needed).
# Token set via COCKPIT_TOKEN env var (separate from BOT_API_KEY).

_COCKPIT_TOKEN = os.environ.get("COCKPIT_TOKEN", "")
_cockpit_rl = {}  # rate limiting for cockpit auth
_COCKPIT_RL_LOCK = threading.Lock()


def _check_cockpit_auth():
    """Verify cockpit token from header or httpOnly cookie. Returns None if ok, error response if not."""
    if not _COCKPIT_TOKEN:
        return jsonify({"error": "cockpit_disabled", "msg": "COCKPIT_TOKEN not configured"}), 503
    token = request.headers.get("X-Cockpit-Token", "") or request.cookies.get("cockpit_session", "")
    if not token or not _hmac.compare_digest(token, _COCKPIT_TOKEN):
        return jsonify({"error": "forbidden"}), 403
    return None


@app.route("/cockpit", methods=["GET"])
def cockpit_page():
    """Serve the cockpit HTML dashboard."""
    if not _COCKPIT_TOKEN:
        return "Cockpit disabled (COCKPIT_TOKEN not set)", 503
    try:
        html = _read_page("cockpit.html")
        read_key = os.environ.get("READ_API_KEY", "")
        html = html.replace("</head>", f'<script>window.__MKT_API_KEY__={json.dumps(read_key)};</script>\n</head>', 1)
        return html, 200, {"Content-Type": "text/html"}
    except FileNotFoundError:
        return "cockpit.html not found", 404


@app.route("/cockpit/api/auth", methods=["POST"])
def cockpit_auth():
    """Validate cockpit token. Rate limited: max 5 attempts per minute per IP."""
    ip = request.remote_addr or "unknown"
    now = time.time()
    with _COCKPIT_RL_LOCK:
        # Rate limit cleanup — purge all stale IPs to prevent memory leak
        stale_ips = [k for k, v in _cockpit_rl.items() if all(now - t >= 60 for t in v)]
        for k in stale_ips:
            del _cockpit_rl[k]
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
        from flask import make_response as _mk
        resp = _mk(jsonify({"status": "ok"}), 200)
        resp.set_cookie(
            "cockpit_session", _COCKPIT_TOKEN,
            httponly=True, secure=True, samesite="Strict", max_age=86400,
        )
        return resp
    else:
        app.logger.warning("[COCKPIT] Auth failed from %s", ip)
        return jsonify({"error": "forbidden"}), 403


@app.route("/cockpit/api/logout", methods=["POST"])
def cockpit_logout():
    """Clear cockpit session cookie."""
    from flask import make_response as _mk
    resp = _mk(jsonify({"status": "ok"}), 200)
    resp.delete_cookie("cockpit_session", httponly=True, secure=True, samesite="Strict")
    return resp


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
            resp = _sb_session.get(
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

    # Fallback: if Supabase returned nothing, read from local integration_report.json
    if not agents:
        try:
            report_path = os.path.join(os.path.dirname(__file__), "scripts", "results", "integration_report.json")
            if os.path.exists(report_path):
                with open(report_path, "r") as f:
                    report = json.load(f)
                for cid, clone in report.get("clones", {}).items():
                    agents.append({
                        "clone_id": cid,
                        "name": clone.get("name", cid),
                        "role": clone.get("role", ""),
                        "status": clone.get("status", "done"),
                        "model": clone.get("model", ""),
                        "current_task": clone.get("last_message", "")[:100],
                        "last_message": clone.get("last_message", ""),
                        "thought": "",
                        "cost_usd": float(clone.get("cost_usd", 0)),
                        "max_budget": 8.0,
                        "elapsed_sec": float(clone.get("elapsed_sec", 0)),
                        "tasks": [],
                        "next_action": "",
                        "next_action_time": "",
                        "result_summary": clone.get("result_text", "")[:200],
                        "notes": f"Batch: {report.get('timestamp', '')[:10]}",
                        "priority": False,
                    })
        except Exception as fe:
            app.logger.warning("[COCKPIT] Fallback report read failed: %s", fe)

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
        "version": VERSION,
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
                key=os.environ.get("KRAKEN_FUTURES_API_KEY", ""),
                secret=os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
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
                    flex = _wallets.get("accounts", {}).get("flex", _wallets.get("multiCollateral", {}))
                    me = flex.get("marginEquity") or flex.get("pv") or flex.get("portfolioValue") or 0
                    overview["wallet_equity"] = float(me)
            except Exception:
                pass
        except Exception:
            overview["position_detail"] = "Kraken API unavailable"

        # Today's predictions (expanded select for pnl + ghost + latest)
        today_str = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
        resp = _kraken_session.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}?select=id,correct,confidence,direction,pnl_usd,bet_taken,created_at,tx_hash"
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
        resp2 = _sb_session.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}?select=correct,pnl_usd"
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
            resp3 = _sb_session.get(
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

    # Run anomaly detection (throttled internally to 1/min)
    _check_anomalies()

    return jsonify(overview), 200


@app.route("/cockpit/api/bot-toggle", methods=["POST"])
def cockpit_bot_toggle():
    """Toggle bot paused state. No body needed — it's a toggle."""
    global _BOT_PAUSED, _BOT_PAUSED_REFRESHED_AT
    err = _check_cockpit_auth()
    if err:
        return err
    _refresh_bot_paused()
    # If currently paused and trying to resume, enforce CB cooldown
    if _BOT_PAUSED:
        elapsed = time.time() - _CB_TRIPPED_AT
        if _CB_TRIPPED_AT > 0 and elapsed < _CB_COOLDOWN_SEC:
            remaining = int((_CB_COOLDOWN_SEC - elapsed) / 60)
            app.logger.warning(f"[COCKPIT] Resume blocked — CB cooldown {remaining}m remaining")
            _push_cockpit_log("app", "warning", "Resume blocked (cockpit)",
                              f"Circuit-breaker cooldown: {remaining}m remaining (30m required)")
            return jsonify({
                "paused": True,
                "error": "cooldown_active",
                "message": f"Cooldown attivo — riprova tra {remaining} minuti",
                "cooldown_remaining_min": remaining,
            }), 429
    _BOT_PAUSED = not _BOT_PAUSED
    _BOT_PAUSED_REFRESHED_AT = time.time()
    _save_bot_paused(_BOT_PAUSED)
    app.logger.info("[COCKPIT] Bot toggled → paused=%s", _BOT_PAUSED)
    _push_cockpit_log("app", "warning" if _BOT_PAUSED else "success",
                       "Bot PAUSED" if _BOT_PAUSED else "Bot RESUMED",
                       f"Toggled via cockpit (paused={_BOT_PAUSED})")
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
        resp = _sb_session.patch(url, json=patch_body, headers=headers, timeout=5)
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
        resp = _sb_session.patch(
            f"{sb_url}/rest/v1/cockpit_events?clone_id=eq.{clone_id}",
            json=patch_body, headers=headers, timeout=5,
        )
        if not resp.ok:
            return jsonify({"error": "supabase_error", "detail": resp.text}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    return jsonify({"ok": True}), 200


@app.route("/cockpit/api/log", methods=["GET"])
def cockpit_log():
    """Return system-wide event log from cockpit_log table."""
    err = _check_cockpit_auth()
    if err:
        return err
    logs = []
    try:
        sb_url, sb_key = _sb_config()
        if sb_url and sb_key:
            headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
            params = {
                "select": "id,ts,source,level,title,message,metadata",
                "order": "ts.desc",
                "limit": "100",
            }
            # Optional level filter
            level = request.args.get("level")
            if level and level in _LOG_VALID_LEVELS:
                params["level"] = f"eq.{level}"
            # Optional source filter
            source = request.args.get("source")
            if source and re.fullmatch(r'[a-z0-9_-]{1,50}', source):
                params["source"] = f"eq.{source}"
            resp = _sb_session.get(
                f"{sb_url}/rest/v1/cockpit_log",
                headers=headers, params=params, timeout=5,
            )
            if resp.ok:
                logs = resp.json()
    except Exception as e:
        app.logger.warning("[COCKPIT] Log fetch error: %s", e)
    return jsonify({"logs": logs}), 200


@app.route("/cockpit/api/log/ingest", methods=["POST"])
def cockpit_log_ingest():
    """Webhook endpoint for external systems (n8n, Sentry) to push events.

    Accepts: {"source": "n8n", "level": "error", "title": "...", "message": "...", "metadata": {}}
    Auth: same COCKPIT_TOKEN via X-Cockpit-Token header.
    """
    err = _check_cockpit_auth()
    if err:
        return err
    data = request.get_json(force=True) or {}
    source = str(data.get("source", "external"))[:50]
    level = str(data.get("level", "info"))
    if level not in _LOG_VALID_LEVELS:
        level = "info"
    title = str(data.get("title", ""))[:120]
    message = str(data.get("message", ""))[:2000]
    metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}

    _push_cockpit_log(source, level, title, message, metadata)
    return jsonify({"ok": True}), 200


# ── Anomaly Detection (lightweight, runs on cockpit overview refresh) ───────
_LAST_ANOMALY_CHECK = 0
_LAST_CONFIDENCE_VALUES: list = []
_ANOMALY_LOCK = threading.Lock()


def _check_anomalies():
    """Lightweight anomaly checks. Called from cockpit overview, max once per 60s."""
    global _LAST_ANOMALY_CHECK, _LAST_CONFIDENCE_VALUES
    now = time.time()
    with _ANOMALY_LOCK:
        if now - _LAST_ANOMALY_CHECK < 60:
            return
        _LAST_ANOMALY_CHECK = now

    try:
        sb_url, sb_key = _sb_config()
        if not sb_url or not sb_key:
            return
        headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}

        # Check 1: Stuck confidence — last 5 predictions have identical confidence
        resp = _sb_session.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}?select=confidence,created_at"
            "&order=created_at.desc&limit=5",
            headers=headers, timeout=5,
        )
        if resp.ok:
            preds = resp.json()
            confs = [p.get("confidence") for p in preds if p.get("confidence") is not None]
            if len(confs) >= 5 and len(set(confs)) == 1:
                # All 5 identical → anomaly
                stuck_val = confs[0]
                if confs != _LAST_CONFIDENCE_VALUES:
                    _push_cockpit_log("anomaly", "critical",
                                      f"Stuck confidence: {stuck_val}",
                                      f"Last 5 predictions all have confidence={stuck_val}. "
                                      "Possible model or data pipeline bug.",
                                      {"confidence": stuck_val, "count": 5})
            _LAST_CONFIDENCE_VALUES = confs

        # Check 2: No predictions in last 2 hours (during expected active period)
        two_hours_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)).isoformat()
        resp2 = _sb_session.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}?select=id&created_at=gte.{two_hours_ago}&limit=1",
            headers=headers, timeout=5,
        )
        if resp2.ok and len(resp2.json()) == 0:
            hour_utc = _dt.datetime.now(_dt.timezone.utc).hour
            # Only alert during expected trading hours (6-22 UTC)
            if 6 <= hour_utc <= 22:
                _push_cockpit_log("anomaly", "warning",
                                  "No predictions in 2h",
                                  "No new predictions in the last 2 hours during active trading window. "
                                  "Check n8n workflow or data feeds.",
                                  {"last_check_utc": two_hours_ago})

    except Exception as e:
        app.logger.warning("[COCKPIT] Anomaly check error: %s", e)


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
            resp = _sb_session.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                f"?select=id,direction,confidence,reason,created_at,ghost_correct,signal_price"
                f"&bet_taken=eq.false&order=created_at.desc&limit=10",
                headers=headers, timeout=5,
            )
            if resp.ok:
                ghosts = resp.json()
    except Exception as e:
        app.logger.warning("[COCKPIT] Ghosts error: %s", e)
    return jsonify({"ghosts": ghosts}), 200


# ── FIXER VERIFY (Second Check post-fix) ────────────────────────────────────

@app.route("/fixer-verify", methods=["POST"])
def fixer_verify():
    """Second check after autonomous fixer applies a fix.
    Called by n8n wf13 post-fix step. Runs health + Sentry + cockpit_log checks.

    Body JSON: {
        error_title: str,      # original error title
        fix_description: str,  # what the fixer did
        fix_source: str,       # "wf13" / "manual"
    }
    Returns: {ok, checks: {health, sentry_quiet, cockpit_clean}, verdict}
    """
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    error_title = str(data.get("error_title", "unknown"))[:200]
    checks = {}

    # 1. Health check — call internal function directly (avoids localhost networking issues on Railway)
    try:
        with app.test_request_context("/health"):
            h_response = health()
            h_data = h_response.get_json() if hasattr(h_response, 'get_json') else {}
        checks["health"] = {
            "ok": h_data.get("status") == "ok",
            "bot_paused": h_data.get("bot_paused", True),
            "supabase_ok": h_data.get("supabase_ok"),
            "wallet_equity": h_data.get("wallet_equity"),
            "version": h_data.get("version"),
        }
    except Exception as e:
        checks["health"] = {"ok": False, "error": str(e)[:120]}

    # 2. Cockpit log — check no new critical/error in last 5 min
    try:
        sb_url, sb_key = _sb_config()
        five_min_ago = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5)).isoformat()
        r = _sb_session.get(
            f"{sb_url}/rest/v1/cockpit_log"
            f"?select=level,title,created_at&level=in.(critical,error)"
            f"&created_at=gte.{five_min_ago}&order=created_at.desc&limit=5",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            timeout=5,
        )
        recent_errors = r.json() if r.ok else []
        checks["cockpit_clean"] = {
            "ok": len(recent_errors) == 0,
            "recent_errors": len(recent_errors),
            "latest": recent_errors[0]["title"] if recent_errors else None,
        }
    except Exception as e:
        checks["cockpit_clean"] = {"ok": False, "error": str(e)[:120]}

    # 3. Sentry quiet — check via cockpit_log source="sentry" in last 5 min
    try:
        r2 = _sb_session.get(
            f"{sb_url}/rest/v1/cockpit_log"
            f"?select=title,created_at&source=eq.sentry"
            f"&created_at=gte.{five_min_ago}&order=created_at.desc&limit=3",
            headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
            timeout=5,
        )
        sentry_hits = r2.json() if r2.ok else []
        checks["sentry_quiet"] = {
            "ok": len(sentry_hits) == 0,
            "recent_hits": len(sentry_hits),
        }
    except Exception as e:
        checks["sentry_quiet"] = {"ok": False, "error": str(e)[:120]}

    # Verdict
    all_ok = all(c.get("ok", False) for c in checks.values())
    verdict = "resolved" if all_ok else "partial" if checks.get("health", {}).get("ok") else "failed"

    _push_cockpit_log("fixer", "success" if all_ok else "warning",
                      f"Second check: {verdict}",
                      f"Error: {error_title}",
                      {"checks": checks, "verdict": verdict})

    return jsonify({"ok": all_ok, "checks": checks, "verdict": verdict})


# ── INCIDENT REPORT (Gold Standard PDF via Sentinel) ────────────────────────

@app.route("/incident-report", methods=["POST"])
def incident_report():
    """Generate a brief Gold Standard PDF incident report and send via BTC Sentinel.

    Body JSON: {
        error_title: str,
        error_detail: str,       # Sentry error message
        error_source: str,       # "ai_predict", "place_bet", etc.
        severity: str,           # "P0" / "P1" / "P2"
        ai_analysis: str,        # AI analysis from wf00/wf10
        fix_description: str,    # what the fixer did
        fix_files: [str],        # files/workflows modified
        verify_result: {         # output from /fixer-verify
            ok: bool, checks: {}, verdict: str
        }
    }
    Returns: {ok, pdf_sent, message_id?}
    """
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}

    error_title = str(data.get("error_title", "Unknown Error"))[:200]
    error_detail = str(data.get("error_detail", ""))[:500]
    error_source = str(data.get("error_source", "unknown"))[:50]
    severity = str(data.get("severity", "P2"))[:3]
    ai_analysis = str(data.get("ai_analysis", ""))[:1000]
    fix_description = str(data.get("fix_description", ""))[:1000]
    fix_files = data.get("fix_files", [])
    if not isinstance(fix_files, list):
        fix_files = []
    fix_files = [str(f)[:120] for f in fix_files[:10]]
    verify = data.get("verify_result", {})
    verdict = str(verify.get("verdict", "unknown"))
    checks = verify.get("checks", {})

    now = _dt.datetime.now(_dt.timezone.utc)
    ts_display = now.strftime("%Y-%m-%d %H:%M UTC")

    # Severity colors
    sev_colors = {"P0": "#ff4757", "P1": "#ffb347", "P2": "#4dabf7"}
    sev_color = sev_colors.get(severity, "#4dabf7")
    verdict_colors = {"resolved": "#51cf66", "partial": "#ffb347", "failed": "#ff4757"}
    verdict_color = verdict_colors.get(verdict, "#8899b4")
    verdict_label = {"resolved": "RISOLTO", "partial": "PARZIALE", "failed": "FALLITO"}.get(verdict, verdict.upper())

    # Health data
    h = checks.get("health", {})
    cockpit = checks.get("cockpit_clean", {})
    sentry = checks.get("sentry_quiet", {})

    # Build files list HTML
    files_html = ""
    for f in fix_files:
        files_html += f'<div class="file-item">{f}</div>\n'
    if not files_html:
        files_html = '<div class="file-item" style="color:#8899b4;">Nessun file modificato</div>'

    html = f"""<!DOCTYPE html>
<html lang="it">
<head><meta charset="UTF-8">
<style>
@page {{ margin: 0; size: A4; }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{
    width: 100%; background: #0a0e1a; color: #e8ecf4;
    font-family: -apple-system, 'SF Pro Display', system-ui, sans-serif;
    -webkit-print-color-adjust: exact; print-color-adjust: exact;
}}
@media print {{
    html, body {{ background: #0a0e1a; margin: 0; padding: 0; }}
}}
.page {{ padding: 2.5rem; min-height: 100vh; }}

/* Cover */
.cover {{ display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; }}
.shield {{ width: 80px; height: 80px; margin-bottom: 1.5rem; }}
.cover h1 {{ font-size: 1.8rem; color: #00d4aa; margin-bottom: 0.5rem; }}
.cover .subtitle {{ color: #8899b4; font-size: 1rem; margin-bottom: 2rem; }}
.meta-cards {{ display: flex; gap: 1rem; flex-wrap: wrap; justify-content: center; }}
.meta-card {{
    background: #1a2035; border-radius: 12px; padding: 1rem 1.5rem;
    min-width: 120px; text-align: center;
}}
.meta-card .label {{ font-size: 0.7rem; text-transform: uppercase; color: #8899b4; letter-spacing: 1px; }}
.meta-card .value {{ font-size: 1.3rem; font-weight: 700; margin-top: 0.3rem; }}

/* Body */
.section {{ margin-bottom: 2rem; }}
.section-header {{
    display: flex; align-items: center; gap: 0.8rem;
    border-bottom: 1px solid #1a2035; padding-bottom: 0.8rem; margin-bottom: 1.2rem;
}}
.section-header h2 {{ font-size: 1.1rem; color: #e8ecf4; }}
.section-icon {{
    width: 32px; height: 32px; border-radius: 8px; display: flex;
    align-items: center; justify-content: center; font-size: 1rem;
}}
.card {{
    background: #1a2035; border-radius: 10px; padding: 1.2rem;
    margin-bottom: 1rem; border-left: 3px solid #00d4aa;
}}
.card.error {{ border-left-color: {sev_color}; }}
.card.fix {{ border-left-color: #00d4aa; }}
.card.verdict {{ border-left-color: {verdict_color}; }}
.card-title {{ font-size: 0.7rem; text-transform: uppercase; color: #8899b4; letter-spacing: 1px; margin-bottom: 0.5rem; }}
.card-body {{ font-size: 0.9rem; line-height: 1.5; }}
.file-item {{
    font-family: 'SF Mono', monospace; font-size: 0.8rem;
    background: #111827; padding: 0.4rem 0.8rem; border-radius: 6px;
    margin-bottom: 0.3rem; color: #b197fc;
}}
.check-row {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 0.6rem 0; border-bottom: 1px solid #111827;
}}
.check-label {{ color: #8899b4; font-size: 0.85rem; }}
.check-status {{ font-weight: 700; font-size: 0.85rem; }}
.check-ok {{ color: #51cf66; }}
.check-fail {{ color: #ff4757; }}
.verdict-box {{
    text-align: center; padding: 1.5rem; background: #1a2035;
    border-radius: 12px; border: 2px solid {verdict_color};
}}
.verdict-label {{ font-size: 2rem; font-weight: 800; color: {verdict_color}; }}
.verdict-sub {{ color: #8899b4; margin-top: 0.5rem; font-size: 0.85rem; }}
.footer {{ text-align: center; color: #8899b4; font-size: 0.7rem; margin-top: 2rem; padding-top: 1rem; border-top: 1px solid #1a2035; }}
</style></head>
<body>

<!-- PAGE 1: COVER -->
<div class="page cover">
    <svg class="shield" viewBox="0 0 80 80" fill="none">
        <path d="M40 5L10 20v20c0 16.57 12.83 32.08 30 36 17.17-3.92 30-19.43 30-36V20L40 5z"
              fill="#1a2035" stroke="#00d4aa" stroke-width="2"/>
        <path d="M35 42l-6-6 2.8-2.8L35 36.4l13.2-13.2L51 26 35 42z" fill="#00d4aa"/>
    </svg>
    <h1>BTC Sentinel — Incident Report</h1>
    <div class="subtitle">Autonomous Fixer Pipeline — Investigate → Plan → Execute → Verify → Report</div>
    <div class="meta-cards">
        <div class="meta-card">
            <div class="label">Date</div>
            <div class="value" style="font-size:1rem;">{ts_display}</div>
        </div>
        <div class="meta-card">
            <div class="label">Severity</div>
            <div class="value" style="color:{sev_color};">{severity}</div>
        </div>
        <div class="meta-card">
            <div class="label">Verdict</div>
            <div class="value" style="color:{verdict_color};">{verdict_label}</div>
        </div>
        <div class="meta-card">
            <div class="label">Source</div>
            <div class="value" style="font-size:0.9rem;">{error_source}</div>
        </div>
    </div>
</div>

<!-- PAGE 2: BODY -->
<div class="page" style="page-break-before:always;">

    <div class="section">
        <div class="section-header">
            <div class="section-icon" style="background:{sev_color}20;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="{sev_color}" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            </div>
            <h2>Original Error</h2>
        </div>
        <div class="card error">
            <div class="card-title">Title</div>
            <div class="card-body" style="font-weight:600;">{error_title}</div>
        </div>
        <div class="card error">
            <div class="card-title">Detail</div>
            <div class="card-body">{error_detail or 'No additional details'}</div>
        </div>
        <div class="card error">
            <div class="card-title">AI Analysis</div>
            <div class="card-body">{ai_analysis or 'Analysis not available'}</div>
        </div>
    </div>

    <div class="section">
        <div class="section-header">
            <div class="section-icon" style="background:#00d4aa20;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#00d4aa" stroke-width="2"><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z"/></svg>
            </div>
            <h2>Applied Fix</h2>
        </div>
        <div class="card fix">
            <div class="card-title">Description</div>
            <div class="card-body">{fix_description or 'No fix recorded'}</div>
        </div>
        <div class="card fix">
            <div class="card-title">Modified Files / Workflows</div>
            <div class="card-body">{files_html}</div>
        </div>
    </div>

    <div class="section">
        <div class="section-header">
            <div class="section-icon" style="background:#4dabf720;">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#4dabf7" stroke-width="2"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/></svg>
            </div>
            <h2>Second Check — Verification</h2>
        </div>
        <div class="card" style="border-left-color:#4dabf7;">
            <div class="check-row">
                <span class="check-label">Health Endpoint</span>
                <span class="check-status {'check-ok' if h.get('ok') else 'check-fail'}">{'PASS' if h.get('ok') else 'FAIL'}</span>
            </div>
            <div class="check-row">
                <span class="check-label">Bot Paused</span>
                <span class="check-status" style="color:{'#ffb347' if h.get('bot_paused') else '#51cf66'}">{'Yes' if h.get('bot_paused') else 'No'}</span>
            </div>
            <div class="check-row">
                <span class="check-label">Supabase</span>
                <span class="check-status {'check-ok' if h.get('supabase_ok') else 'check-fail'}">{'PASS' if h.get('supabase_ok') else 'FAIL'}</span>
            </div>
            <div class="check-row">
                <span class="check-label">Cockpit (no errors 5min)</span>
                <span class="check-status {'check-ok' if cockpit.get('ok') else 'check-fail'}">{'PASS — clean' if cockpit.get('ok') else f"FAIL — {cockpit.get('recent_errors', '?')} errors"}</span>
            </div>
            <div class="check-row">
                <span class="check-label">Sentry (quiet 5min)</span>
                <span class="check-status {'check-ok' if sentry.get('ok') else 'check-fail'}">{'PASS — quiet' if sentry.get('ok') else f"FAIL — {sentry.get('recent_hits', '?')} hit"}</span>
            </div>
            <div class="check-row">
                <span class="check-label">Wallet Equity</span>
                <span class="check-status" style="color:#00d4aa;">${h.get('wallet_equity', 'N/A')}</span>
            </div>
        </div>
    </div>

    <div class="verdict-box">
        <div class="verdict-label">{verdict_label}</div>
        <div class="verdict-sub">
            {'All checks passed. The fix resolved the issue.' if verdict == 'resolved'
             else 'Partial fix — some checks failed. Monitor closely.' if verdict == 'partial'
             else 'Fix unsuccessful — manual intervention required.'}
        </div>
    </div>

    <div class="footer">
        BTC Sentinel — Autonomous Fixer v{VERSION} — {ts_display}<br>
        Pipeline: Investigate → Plan → Execute → Verify → Report
    </div>
</div>

</body></html>"""

    # Write HTML → PDF → send via Sentinel
    base_dir = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(base_dir, exist_ok=True)
    safe_title = re.sub(r'[^\w\s-]', '', error_title)[:40].strip().replace(' ', '_')
    filename = f"Incident_{severity}_{now.strftime('%Y%m%d_%H%M')}_{safe_title}"
    html_path = os.path.join(base_dir, f"{filename}.html")
    pdf_path = os.path.join(base_dir, f"{filename}.pdf")

    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html)

        # PDF generation — try xhtml2pdf, fallback to sending HTML as document
        send_path = html_path  # default: send HTML
        send_name = f"{filename}.html"
        try:
            from xhtml2pdf import pisa
            with open(html_path, "r", encoding="utf-8") as src, open(pdf_path, "wb") as dst:
                pisa_status = pisa.CreatePDF(src.read(), dest=dst)
            if not pisa_status.err and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
                send_path = pdf_path
                send_name = f"{filename}.pdf"
                if os.path.exists(html_path):
                    os.remove(html_path)
        except ImportError:
            app.logger.info("[INCIDENT-REPORT] xhtml2pdf not available, sending HTML")
        except Exception as pdf_err:
            app.logger.warning("[INCIDENT-REPORT] PDF generation failed: %s, sending HTML", pdf_err)

    except Exception as e:
        for p in (html_path, pdf_path):
            if os.path.exists(p):
                os.remove(p)
        return jsonify({"ok": False, "error": f"Report error: {str(e)[:120]}"}), 500

    # Send via BTC Sentinel (sendDocument to owner DM)
    tg_token = os.environ.get("TELEGRAM_PRIVATE_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_owner = os.environ.get("TELEGRAM_OWNER_ID", "")
    message_id = None

    if tg_token and tg_owner:
        try:
            caption = (
                f"{'🔴' if severity == 'P0' else '🟡' if severity == 'P1' else '🔵'} "
                f"<b>Incident Report — {severity}</b>\n\n"
                f"<b>Error:</b> {error_title[:100]}\n"
                f"<b>Fix:</b> {fix_description[:100]}\n"
                f"<b>Verdict:</b> <b>{verdict_label}</b>\n\n"
                f"{'✅ All checks OK' if verdict == 'resolved' else '⚠️ Manual verification recommended'}"
            )
            mime = "application/pdf" if send_name.endswith(".pdf") else "text/html"
            with open(send_path, "rb") as doc_file:
                resp = _tg_session.post(
                    f"https://api.telegram.org/bot{tg_token}/sendDocument",
                    data={
                        "chat_id": tg_owner,
                        "caption": caption[:1024],
                        "parse_mode": "HTML",
                    },
                    files={"document": (send_name, doc_file, mime)},
                    timeout=30,
                )
            result = resp.json()
            if result.get("ok"):
                message_id = result["result"]["message_id"]
        except Exception as e:
            app.logger.warning("[INCIDENT-REPORT] Telegram send failed: %s", e)

    # Cleanup sent file
    for p in (html_path, pdf_path):
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass

    _push_cockpit_log("sentinel", "info",
                      f"Incident report sent: {severity} — {verdict}",
                      error_title,
                      {"file": send_name, "tg_sent": message_id is not None, "verdict": verdict})

    return jsonify({
        "ok": True,
        "file_sent": send_name,
        "pdf_sent": message_id is not None,
        "message_id": message_id,
        "verdict": verdict,
    })


# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
