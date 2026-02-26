import os
import json
import time
import pickle
import requests
from flask import Flask, request, jsonify, redirect
from kraken.futures import Trade, User

app = Flask(__name__)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# ── XGBoost direction model (caricato una volta all'avvio) ────────────────────
_XGB_MODEL = None
_XGB_FEATURE_COLS = [
    "confidence", "fear_greed_value", "rsi14", "technical_score",
    "hour_sin", "hour_cos",  # encoding ciclico — allineato a train_xgboost.py
    "ema_trend_up", "technical_bias_bullish", "signal_technical_buy",
    "signal_sentiment_pos", "signal_fg_fear", "signal_volume_high",
]

def _load_xgb_model():
    global _XGB_MODEL
    model_path = os.path.join(os.path.dirname(__file__), "models", "xgb_direction.pkl")
    if os.path.exists(model_path):
        with open(model_path, "rb") as f:
            _XGB_MODEL = pickle.load(f)
        print(f"[XGB] Model loaded from {model_path}")
    else:
        print(f"[XGB] Model not found at {model_path} — /predict-xgb will return agree=True")

_load_xgb_model()

# ── XGBoost correctness model (caricato una volta all'avvio) ─────────────────
_xgb_correctness = None
try:
    _corr_path = os.path.join(os.path.dirname(__file__), "models", "xgb_correctness.pkl")
    with open(_corr_path, "rb") as f:
        _xgb_correctness = pickle.load(f)
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
# Default conservativo: solo ore con n>=8 e WR<45% confermato da dati storici
DEAD_HOURS_UTC: set = {12, 22}

def refresh_calibration():
    """Aggiorna CONF_CALIBRATION da WR reale Supabase per bucket di confidence."""
    global CONF_CALIBRATION
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
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
        return {"ok": False, "error": str(e)}

def refresh_dead_hours():
    """Aggiorna DEAD_HOURS_UTC: ore con WR < 45% e almeno 8 bet. Ora estratta da created_at."""
    global DEAD_HOURS_UTC
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
    if not sb_url or not sb_key:
        return {"ok": False, "error": "no_supabase_env"}
    try:
        import datetime
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
                h = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).hour
                hour_data[h].append(1 if c else 0)
            except Exception:
                continue
        dead, hour_stats = set(), {}
        for h, vals in sorted(hour_data.items()):
            wr = sum(vals) / len(vals) if vals else 0.5
            hour_stats[h] = {"wr": round(wr, 3), "n": len(vals)}
            if len(vals) >= 8 and wr < 0.45:
                dead.add(h)
        # fallback: se non ci sono ore con n>=8 e WR<45%, usa le due ore più stabili storicamente
        DEAD_HOURS_UTC = dead if dead else {12, 22}
        print(f"[CAL] Dead hours updated: {sorted(DEAD_HOURS_UTC)}")
        return {"ok": True, "dead_hours": sorted(DEAD_HOURS_UTC), "hour_stats": hour_stats}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# Refresh calibration all'avvio (non-blocking)
try:
    refresh_calibration()
    refresh_dead_hours()
except Exception:
    pass

API_KEY = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PF_XBTUSD")
KRAKEN_BASE = "https://futures.kraken.com"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")
SUPABASE_TABLE = os.environ.get("SUPABASE_TABLE", "btc_predictions")
_BOT_PAUSED = False  # runtime pause via /pause — non persiste al restart
_costs_cache = {"data": None, "ts": 0.0}


def _check_api_key():
    """Verifica X-API-Key header se BOT_API_KEY env var è impostata.
    Retrocompatibile: se BOT_API_KEY non configurata, passa tutto.
    """
    bot_key = os.environ.get("BOT_API_KEY")
    if not bot_key:
        return None
    if request.headers.get("X-API-Key") != bot_key:
        return jsonify({"error": "Unauthorized"}), 401
    return None


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
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")
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
            correct = exit_price >= entry_price
        else:
            pnl_gross = (entry_price - exit_price) * bet_size
            correct = exit_price <= entry_price

        fee = bet_size * (entry_price + exit_price) * 0.00005  # entry + exit taker fee
        pnl_net = round(pnl_gross - fee, 6)

        patch_url = f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}"
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
    capital = float(os.environ.get("CAPITAL_USD", 100))

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
    try:
        sb_url = os.environ.get("SUPABASE_URL", "")
        sb_key = os.environ.get("SUPABASE_KEY", "")
        if sb_url and sb_key:
            r = requests.get(
                f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
                "?select=correct,pnl_usd&bet_taken=eq.true&correct=not.is.null&order=id.desc&limit=10",
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                timeout=3,
            )
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
                elif streak_type == True and streak >= 3: multiplier = 1.5 if 0.62 >= 0.65 else 1.2
                base_size = round(max(0.001, min(0.005, 0.002 * multiplier)), 6)
    except Exception:
        pass

    return jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "serverTime": get_kraken_servertime(),
        "symbol": DEFAULT_SYMBOL,
        "api_key_set": bool(API_KEY),
        "version": "2.5.1",
        "dry_run": DRY_RUN,
        "paused": _BOT_PAUSED,
        "bot_paused": bool(_BOT_PAUSED),
        "capital": capital,
        "capital_usd": float(os.environ.get("CAPITAL_USD", "100.0")),
        "wallet_equity": wallet_equity,
        "base_size": base_size,
        "confidence_threshold": float(os.environ.get("CONF_THRESHOLD", "0.65")),
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
        return jsonify({"status": "error", "error": str(e)}), 500


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
                    sb_url = os.environ.get("SUPABASE_URL", "")
                    sb_key = os.environ.get("SUPABASE_ANON_KEY", "")
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
        return jsonify({"status": "error", "error": str(e), "symbol": symbol}), 500


# ── CLOSE POSITION ───────────────────────────────────────────────────────────

@app.route("/close-position", methods=["POST"])
def close_position():
    err = _check_api_key()
    if err:
        return err
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
            return jsonify({
                "status": "no_position",
                "message": "Nessuna posizione aperta, nulla da chiudere.",
                "symbol": symbol
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

        return jsonify({
            "status": "closed" if (ok and after is None) else ("closing" if ok else "failed"),
            "symbol": symbol,
            "closed_side": pos["side"],
            "close_order_side": close_side,
            "size": size,
            "position_after": after,
            "raw": result,
        }), (200 if ok else 400)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "symbol": symbol}), 500


# ── BOT PAUSE / RESUME ───────────────────────────────────────────────────────

@app.route("/pause", methods=["POST"])
def pause_bot():
    err = _check_api_key()
    if err:
        return err
    global _BOT_PAUSED
    _BOT_PAUSED = True
    return jsonify({"paused": True, "message": "Bot in pausa — nessun nuovo trade"}), 200


@app.route("/resume", methods=["POST"])
def resume_bot():
    err = _check_api_key()
    if err:
        return err
    global _BOT_PAUSED
    _BOT_PAUSED = False
    return jsonify({"paused": False, "message": "Bot riattivato — trading ripreso"}), 200


# ── PLACE BET ────────────────────────────────────────────────────────────────

@app.route("/place-bet", methods=["POST"])
def place_bet():
    err = _check_api_key()
    if err:
        return err
    data = request.get_json(force=True) or {}
    direction = (data.get("direction") or "").upper()
    confidence = float(data.get("confidence", 0))
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    try:
        size = float(data.get("size", data.get("stake_usdc", 0.0001)))
        if size <= 0:
            size = 0.0001
    except Exception:
        return jsonify({"status": "failed", "error": "invalid_size"}), 400

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
    xgb_prob_up = 0.5  # default, aggiornato dal blocco XGB sotto
    if _XGB_MODEL is not None:
        try:
            import math as _math
            _h = current_hour_utc
            feat_row = [[
                confidence,
                float(data.get("fear_greed", data.get("fear_greed_value", 50))),
                float(data.get("rsi14", 50)),
                float(data.get("technical_score", 0)),
                _math.sin(2 * _math.pi * _h / 24),  # hour_sin
                _math.cos(2 * _math.pi * _h / 24),  # hour_cos
                float(data.get("ema_trend_up", 0)),
                float(data.get("technical_bias_bullish", data.get("technical_bias", 0))),
                float(data.get("signal_technical_buy", data.get("signal_technical", 0))),
                float(data.get("signal_sentiment_pos", data.get("signal_sentiment", 0))),
                float(data.get("signal_fg_fear", data.get("signal_fg", 0))),
                float(data.get("signal_volume_high", data.get("signal_volume", 0))),
            ]]
            prob = _XGB_MODEL.predict_proba(feat_row)[0]  # [P(DOWN), P(UP)]
            xgb_prob_up = float(prob[1])  # salva in scope per pyramid check
            xgb_direction = "UP" if prob[1] > 0.5 else "DOWN"
            if xgb_direction != direction:
                return jsonify({
                    "status": "skipped",
                    "reason": "xgb_disagree",
                    "llm_direction": direction,
                    "xgb_direction": xgb_direction,
                    "xgb_prob_up": round(float(prob[1]), 3),
                    "message": f"XGB predicts {xgb_direction}, LLM predicts {direction}. Skipping for safety.",
                }), 200
        except Exception as e:
            app.logger.warning(f"[XGB] Check failed: {e}")

    desired_side = "long" if direction == "UP" else "short"

    if _BOT_PAUSED:
        return jsonify({
            "status": "paused",
            "message": "Bot in pausa — nessun nuovo trade aperto",
            "direction": direction,
            "confidence": confidence,
        }), 200

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
                sb_url = os.environ.get("SUPABASE_URL", "")
                sb_key = os.environ.get("SUPABASE_KEY", "")
                if sb_url and sb_key:
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
                        _sb_url = os.environ.get("SUPABASE_URL", "")
                        _sb_key = os.environ.get("SUPABASE_KEY", "")
                        try:
                            requests.patch(
                                f"{_sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}",
                                json={"pyramid_count": 1, "bet_size": round(current_pos_size + pyramid_size, 4)},
                                headers={
                                    "apikey": _sb_key,
                                    "Authorization": f"Bearer {_sb_key}",
                                    "Content-Type": "application/json",
                                    "Prefer": "return=minimal",
                                },
                                timeout=5,
                            )
                        except Exception:
                            pass
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

        # se opposta => chiudi prima e attendi flat, poi aggiorna Supabase
        if pos and pos["side"] != desired_side:
            close_side = "sell" if pos["side"] == "long" else "buy"
            trade.create_order(
                orderType="mkt",
                symbol=symbol,
                side=close_side,
                size=pos["size"],
                reduceOnly=True,
            )
            wait_for_position(symbol, want_open=False, retries=15, sleep_s=0.35)
            exit_price_at_close = _get_mark_price(symbol) or float(pos.get("price") or 0)
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
        if ok and confirmed_pos:
            try:
                sl_pct = float(data.get("sl_pct", 1.2))
                tp_pct = float(data.get("tp_pct", sl_pct * 2))  # default 2× SL
                entry_price = float(confirmed_pos.get("price") or 0) or _get_mark_price(symbol)
                if entry_price > 0:
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
            "sl_order_id": sl_order_id,
            "sl_price":    sl_price,
            "tp_price":    tp_price,
            "rr_ratio":    rr_ratio,
            "raw": result,
        }), (200 if ok else 400)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

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
        return jsonify({"error": str(e)}), 500


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

        TAKER_RATE = 0.00005  # 0.005% Kraken Futures taker fee
        total_fee = sum(
            float(f.get("fee", 0) or 0) or
            (float(f.get("size", 0)) * float(f.get("price", 0)) * TAKER_RATE)
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
        return jsonify({"error": str(e)}), 500

# ── ACCOUNT SUMMARY (tutto in uno) ───────────────────────────────────────────

@app.route("/account-summary", methods=["GET"])
def account_summary():
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    try:
        trade = get_trade_client()
        user  = get_user_client()

        # ── 1. WALLET ────────────────────────────────────────────────────────
        wallets = user.get_wallets()
        flex = wallets.get("accounts", {}).get("flex", {})

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

        # ── TICKERS (chiamata unica per sezioni 2 e 3) ──────────────────────
        all_tickers = []
        try:
            all_tickers = trade.request(
                method="GET",
                uri="/derivatives/api/v3/tickers",
                auth=False
            ).get("tickers", [])
        except Exception:
            pass

        # ── 2. POSIZIONE APERTA ──────────────────────────────────────────────
        pos = get_open_position(symbol)

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
        open_orders = []
        try:
            orders_raw = trade.request(
                method="GET",
                uri="/derivatives/api/v3/openorders",
                auth=True
            ).get("openOrders", []) or []
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
        except Exception:
            pass

        # ── 5. ULTIMI 5 FILL (P&L realizzato recente) ────────────────────────
        recent_fills = []
        realized_pnl_recent = 0.0
        try:
            fills_raw = trade.request(
                method="GET",
                uri="/derivatives/api/v3/fills",
                auth=True
            ).get("fills", []) or []
            symbol_fills = [
                f for f in fills_raw
                if (f.get("symbol") or "").upper() == symbol.upper()
            ][:5]  # ultimi 5
            TAKER_RATE = 0.00005  # Kraken Futures taker fee 0.005%
            for f in symbol_fills:
                # Kraken fills don't return 'fee' or 'pnl' fields — calculate fee manually
                size_f  = float(f.get("size",  0) or 0)
                price_f = float(f.get("price", 0) or 0)
                fee_raw = float(f.get("fee",   0) or 0)
                fee = fee_raw if fee_raw > 0 else round(size_f * price_f * TAKER_RATE, 6)
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
        })

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

# ── SIGNALS PROXY (Supabase) ─────────────────────────────────────────────────

@app.route("/signals", methods=["GET"])
def get_signals():
    try:
        try:
            limit = max(1, min(int(request.args.get("limit", 500)), 1000))
        except (ValueError, TypeError):
            limit = 500

        try:
            days = max(1, min(int(request.args.get("days", 0)), 365))
        except (ValueError, TypeError):
            days = 0

        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")

        if not supabase_url or not supabase_key:
            return jsonify({"error": "Supabase credentials not configured"}), 500

        url = f"{supabase_url}/rest/v1/{SUPABASE_TABLE}?select=*&order=id.desc&limit={limit}"
        if days > 0:
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            url += f"&created_at=gte.{since}"

        res = requests.get(url, headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }, timeout=10)

        if not res.ok:
            return jsonify({"error": f"Supabase HTTP {res.status_code}"}), 502
        return jsonify(res.json()), res.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── PERFORMANCE STATS ────────────────────────────────────────────────────────

@app.route("/performance-stats", methods=["GET"])
def performance_stats():
    """
    Calcola statistiche storiche live da Supabase e restituisce un testo
    compatto da iniettare nel prompt di Claude come contesto di calibrazione.
    """
    try:
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")
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
        return jsonify({"perf_stats_text": f"n/a ({str(e)[:60]})"})


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

        import math as _math2
        _h2 = int(request.args.get("hour_utc", 12))
        features = [[
            float(request.args.get("confidence", 0.62)),
            float(request.args.get("fear_greed_value", 50)),
            float(request.args.get("rsi14", 50)),
            float(request.args.get("technical_score", 0)),
            _math2.sin(2 * _math2.pi * _h2 / 24),  # hour_sin
            _math2.cos(2 * _math2.pi * _h2 / 24),  # hour_cos
            1 if "bullish" in ema_trend or "bull" in ema_trend else 0,
            1 if "bull" in tech_bias else 0,
            1 if sig_tech in ("buy", "bullish") else 0,
            1 if sig_sent in ("positive", "pos", "buy", "bullish") else 0,
            1 if sig_fg == "fear" else 0,
            1 if "high" in sig_vol else 0,
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
        return jsonify({"xgb_direction": None, "agree": True, "reason": str(e)})


# ── BET SIZING ───────────────────────────────────────────────────────────────

@app.route("/bet-sizing", methods=["GET"])
def bet_sizing():
    base_size = float(request.args.get("base_size", 0.002))
    confidence = float(request.args.get("confidence", 0.65))

    # Parametri aggiuntivi per XGBoost correctness model (opzionali, con default neutri)
    fear_greed  = float(request.args.get("fear_greed_value", 50))
    rsi14       = float(request.args.get("rsi14", 50))
    tech_score  = float(request.args.get("technical_score", 0))
    hour_utc    = int(request.args.get("hour_utc", time.gmtime().tm_hour))
    ema_trend   = request.args.get("ema_trend", "").lower()
    tech_bias   = request.args.get("technical_bias", "").lower()
    sig_tech    = request.args.get("signal_technical", "").lower()
    sig_sent    = request.args.get("signal_sentiment", "").lower()
    sig_fg      = request.args.get("signal_fear_greed", "").lower()
    sig_vol     = request.args.get("signal_volume", "").lower()

    ema_trend_up       = 1 if ("bullish" in ema_trend or "bull" in ema_trend or ema_trend == "up") else 0
    tech_bias_bullish  = 1 if "bull" in tech_bias else 0
    sig_tech_buy       = 1 if sig_tech in ("buy", "bullish") else 0
    sig_sent_pos       = 1 if sig_sent in ("positive", "pos", "buy", "bullish") else 0
    sig_fg_fear        = 1 if sig_fg == "fear" else 0
    sig_vol_high       = 1 if "high" in sig_vol else 0

    try:
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")

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
            if confidence >= 0.65:
                multiplier = 1.5
                reason = f"win_streak_{streak}_high_conf"
            else:
                multiplier = 1.2
                reason = f"win_streak_{streak}_low_conf"

        # asymmetry penalty: perdite medie >1.5× i guadagni medi
        if profit_factor < 0.67 and reason == "base":
            multiplier *= 0.75
            reason = "asymmetry_penalty"

        # confidence scaling: 0.65→1.00x | 0.75→1.20x (pivot alzato con nuova soglia)
        conf_mult = 1.0 + (confidence - 0.65) * (0.2 / 0.10)
        conf_mult = round(max(0.8, min(1.2, conf_mult)), 2)

        final_size = round(base_size * multiplier * conf_mult, 6)
        final_size = max(0.001, min(0.005, final_size))

        # P1.1 — XGBoost correctness penalty
        corr_prob = None
        corr_multiplier = 1.0
        if _xgb_correctness is not None:
            try:
                import math as _math3
                feat_row = [[
                    confidence, fear_greed,
                    rsi14, tech_score,
                    _math3.sin(2 * _math3.pi * hour_utc / 24),  # hour_sin
                    _math3.cos(2 * _math3.pi * hour_utc / 24),  # hour_cos
                    ema_trend_up,
                    tech_bias_bullish,
                    sig_tech_buy,
                    sig_sent_pos,
                    sig_fg_fear,
                    sig_vol_high,
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
        return jsonify({"size": base_size, "reason": "error", "error": str(e)})

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
    n8n_key = os.environ.get("N8N_API_KEY", "")
    n8n_url = os.environ.get("N8N_URL", "https://mattiacalastri.app.n8n.cloud")
    supabase_url = os.environ.get("SUPABASE_URL", "")
    supabase_key = os.environ.get("SUPABASE_KEY", "")

    if not n8n_key:
        return jsonify({"status": "error", "error": "N8N_API_KEY not configured"}), 503

    # 1. Cerca bet orfane in Supabase
    try:
        r = requests.get(
            f"{supabase_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,direction,created_at,entry_fill_price"
            "&bet_taken=eq.true&correct=is.null&entry_fill_price=not.is.null&order=id.desc",
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
    WF02_ID = "vallzU6ceD5gPwSP"
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
                    import datetime
                    age_min = (datetime.datetime.utcnow() -
                               datetime.datetime.fromisoformat(started.replace("Z",""))).total_seconds() / 60
                    if age_min < 40:
                        active_ids.add(ex.get("id"))  # execution id, non bet id
                except Exception:
                    pass
    except Exception:
        active_execs = []
        active_ids = set()

    # 3. Triggera wf02 via rescue webhook per bet orfane
    #    (wf02 ha ora un Webhook Rescue Trigger su /webhook/rescue-wf02)
    max_concurrent = 2  # evita flood: al massimo 2 rescue simultanei
    triggered_count = 0
    RESCUE_WEBHOOK_URL = f"{n8n_url}/webhook/rescue-wf02"
    for bet in orphaned:
        bet_id = bet.get("id")
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


@app.route("/costs", methods=["GET"])
def costs():
    """
    Breakdown costi reali + stimati delle piattaforme usate dal bot.
    Cache 10 minuti sulla parte n8n executions.
    """
    global _costs_cache
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}

    # ── 1. Kraken fees (reali da Supabase) ───────────────────────────────────
    kraken_fees_total = 0.0
    trade_count = 0
    try:
        url = (f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
               f"?select=fees_total&bet_taken=eq.true&correct=not.is.null")
        res = requests.get(url, headers=sb_headers, timeout=5)
        rows = res.json() if res.ok else []
        fees_list = [float(r["fees_total"]) for r in rows if r.get("fees_total") is not None]
        kraken_fees_total = round(sum(fees_list), 4)
        trade_count = len(rows)
    except Exception:
        pass

    avg_per_trade = round(kraken_fees_total / trade_count, 6) if trade_count > 0 else 0.0

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
            n8n_url_base = os.environ.get("N8N_URL", "https://mattiacalastri.app.n8n.cloud")
            if n8n_key:
                r = requests.get(
                    f"{n8n_url_base}/api/v1/executions?workflowId=kaevyOIbHpm8vJmF&limit=100",
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

    n8n_limit = int(os.environ.get("N8N_EXECUTION_LIMIT", 2500))
    n8n_pct = round(n8n_exec_est / n8n_limit * 100, 1) if n8n_limit > 0 else 0.0
    n8n_cost = 20.0  # piano Starter

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

    # ── 6. Railway (statico) ──────────────────────────────────────────────────
    railway_plan = os.environ.get("RAILWAY_PLAN", "hobby").lower()
    railway_cost = 5.0 if railway_plan == "hobby" else 0.0

    total = round(
        kraken_fees_total + 0.0 + n8n_cost + claude_api_cost + claude_code_cost + railway_cost,
        4
    )

    return jsonify({
        "kraken_fees": {
            "total_usd": kraken_fees_total,
            "trade_count": trade_count,
            "avg_per_trade": avg_per_trade,
        },
        "supabase": {
            "row_count": row_count,
            "plan": "free",
            "cost_usd": 0.0,
        },
        "n8n": {
            "executions_est": n8n_exec_est,
            "limit": n8n_limit,
            "pct_used": n8n_pct,
            "cost_usd": n8n_cost,
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
        "total_usd": total,
        "cached": cached,
    })


@app.route("/equity-history", methods=["GET"])
def equity_history():
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    capital_base = float(os.environ.get("CAPITAL", "100"))

    try:
        r = requests.get(
            f"{sb_url}/rest/v1/{SUPABASE_TABLE}"
            "?select=id,created_at,pnl_usd"
            "&bet_taken=eq.true&correct=not.is.null&pnl_usd=not.is.null"
            "&created_at=gte.2026-02-24T00:00:00Z"
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
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
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

    capital_base = float(os.environ.get("CAPITAL", "100"))
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
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
    sb_headers = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}"}
    n8n_key = os.environ.get("N8N_API_KEY", "")
    n8n_url_base = os.environ.get("N8N_URL", "https://mattiacalastri.app.n8n.cloud")

    wf02_active = False
    wf02_last_execution = None
    if n8n_key:
        try:
            r = requests.get(
                f"{n8n_url_base}/api/v1/executions?workflowId=vallzU6ceD5gPwSP&limit=5",
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


@app.route("/orphaned-bets", methods=["GET"])
def orphaned_bets():
    err = _check_api_key()
    if err:
        return err
    import datetime
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
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

    now = datetime.datetime.utcnow()
    result = []
    for row in rows:
        minutes_open = 0
        try:
            created = datetime.datetime.fromisoformat(row["created_at"].replace("Z", ""))
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
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
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

    fee_est = bet_size * (entry_price + exit_price) * 0.00005  # entry + exit taker fee
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
    n8n_url = os.environ.get("N8N_URL", "https://mattiacalastri.app.n8n.cloud")
    if not n8n_key:
        return jsonify({"status": "error", "error": "N8N_API_KEY not configured on Railway"}), 200

    # IDs fissi — più robusti dei tag che l'API n8n azzera ad ogni updateNode
    BTC_WORKFLOW_IDS = [
        "9oyKlb64lZIJfZYs",  # 00_Error_Notifier
        "CARzC6ABuXmz7NHr",  # 01A_BTC_AI_Inputs
        "kaevyOIbHpm8vJmF",  # 01B_BTC_Prediction_Bot
        "vallzU6ceD5gPwSP",  # 02_BTC_Trade_Checker
        "KITZHsfVSMtVTpfx",  # 03_BTC_Wallet_Checker
        "eLmZ6d8t9slAx5pj",  # 04_BTC_Talker
        "xCwf53UGBq1SyP0c",  # 05_BTC_Prediction_Verifier
        "O2ilssVhSFs9jsMF",  # 06_Nightly_Maintenance
        "Ei1eeVdA4ZYuc4o6",  # 07_BTC_Commander
        "Z78ywAmykIW73lDB",  # 08_BTC_Position_Monitor
        "KWtBSHht9kbvHovG",  # 09_BTC_Social_Media_Manager
    ]

    headers = {"X-N8N-API-KEY": n8n_key}
    result = []
    try:
        for wf_id in BTC_WORKFLOW_IDS:
            try:
                wf_r = requests.get(
                    f"{n8n_url}/api/v1/workflows/{wf_id}",
                    headers=headers, timeout=5
                )
                if not wf_r.ok:
                    continue
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
                result.append(wf_data)
            except Exception:
                pass

        return jsonify({"status": "ok", "workflows": result, "ts": int(time.time())})

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)[:120]}), 200




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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500


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
        return jsonify({"error": str(e)}), 500

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
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
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
        # Bot configuration & model status (for Training Tab in dashboard)
        "dead_hours": sorted(list(DEAD_HOURS_UTC)),
        "confidence_threshold": float(os.environ.get("CONF_THRESHOLD", "0.65")),
        "base_size_btc": float(os.environ.get("BASE_SIZE", "0.002")),
        "xgb_loaded": _XGB_MODEL is not None,
        "correctness_loaded": _xgb_correctness is not None,
        "model_path": model_path if os.path.exists(model_path) else None,
    })


# ── TRADING STATS ─────────────────────────────────────────────────────────────

@app.route("/trading-stats", methods=["GET"])
def trading_stats():
    """
    Legge la riga più recente dalla tabella trading_stats su Supabase
    e restituisce i dati in JSON.
    """
    try:
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")

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
        return jsonify({"error": str(e)}), 500

# ── MACRO GUARD ──────────────────────────────────────────────────────────────

# Cache in memoria: {"data": [...], "ts": float}
_macro_cache: dict = {"data": None, "ts": 0.0}
_MACRO_CACHE_TTL = 3600  # 1 ora
_MACRO_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def _fetch_macro_calendar() -> list:
    """Fetcha il calendario ForexFactory con cache 1h. Ritorna lista eventi o []."""
    global _macro_cache
    now_ts = time.time()
    if _macro_cache["data"] is not None and (now_ts - _macro_cache["ts"]) < _MACRO_CACHE_TTL:
        return _macro_cache["data"]
    try:
        r = requests.get(_MACRO_CALENDAR_URL, timeout=5)
        if r.ok:
            data = r.json()
            _macro_cache = {"data": data, "ts": now_ts}
            return data
    except Exception:
        pass
    # Ritorna cache scaduta se disponibile (meglio che niente)
    return _macro_cache["data"] or []


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

    import datetime

    try:
        events = _fetch_macro_calendar()
    except Exception:
        return jsonify({"blocked": False, "error": "calendar_unavailable"})

    if not events:
        return jsonify({"blocked": False, "error": "calendar_unavailable"})

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    window_end = now_utc + datetime.timedelta(hours=2)

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
            event_dt = datetime.datetime.fromisoformat(raw_date)
            # Normalizza a UTC
            event_dt_utc = event_dt.astimezone(datetime.timezone.utc)
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
        from web3.middleware import ExtraDataToPOAMiddleware
    except ImportError:
        raise RuntimeError("web3 non installato")

    private_key = os.environ.get("POLYGON_PRIVATE_KEY", "")
    contract_address = os.environ.get("POLYGON_CONTRACT_ADDRESS", "")
    if not private_key or not contract_address:
        raise RuntimeError("POLYGON_PRIVATE_KEY o POLYGON_CONTRACT_ADDRESS non configurati")

    rpc_url = os.environ.get("POLYGON_RPC_URL", "https://polygon-bor-rpc.publicnode.com")
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

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
        w3, contract, account = _get_web3_contract()

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
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
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
        return jsonify({"ok": False, "error": str(e)}), 500


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
        w3, contract, account = _get_web3_contract()

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
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
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
        return jsonify({"ok": False, "error": str(e)}), 500


def _supabase_update(bet_id: int, fields: dict):
    """Helper: aggiorna una riga Supabase per bet_id."""
    sb_url = os.environ.get("SUPABASE_URL", "")
    sb_key = os.environ.get("SUPABASE_KEY", "")
    url = f"{sb_url}/rest/v1/{SUPABASE_TABLE}?id=eq.{bet_id}"
    headers = {
        "apikey": sb_key,
        "Authorization": f"Bearer {sb_key}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }
    r = requests.patch(url, json=fields, headers=headers, timeout=10)
    r.raise_for_status()


# ── DASHBOARD ────────────────────────────────────────────────────────────────

@app.route("/llms.txt", methods=["GET"])
def llms_txt():
    """AI crawler context file (llms.txt standard)."""
    try:
        with open("static/llms.txt", "r") as f:
            content = f.read()
    except FileNotFoundError:
        content = "# BTC Predictor\nhttps://btcpredictor.io\n"
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

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
    )
    return content, 200, {"Content-Type": "text/plain; charset=utf-8"}

@app.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Basic sitemap for indexing."""
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url>\n'
        '    <loc>https://btcpredictor.io/dashboard</loc>\n'
        '    <changefreq>hourly</changefreq>\n'
        '    <priority>1.0</priority>\n'
        '  </url>\n'
        '  <url>\n'
        '    <loc>https://btcpredictor.io/legal</loc>\n'
        '    <changefreq>monthly</changefreq>\n'
        '    <priority>0.3</priority>\n'
        '  </url>\n'
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

  <h2>3. PRIVACY POLICY</h2>
  <p><strong>We do not collect, store, or process any personal data from visitors to this dashboard.</strong></p>
  <ul>
    <li><strong>No cookies</strong> are set by this website (no analytics, no tracking, no advertising cookies).</li>
    <li><strong>No registration or login</strong> is required to view the dashboard. No user accounts exist.</li>
    <li><strong>No third-party analytics</strong> scripts (Google Analytics, Meta Pixel, etc.) are loaded by this page.</li>
    <li>The dashboard fetches live data from our own backend API (Railway) and from on-chain public data (Polygon PoS). No personal data is transmitted in these requests.</li>
    <li>If you contact us by email at <a href="mailto:signal@btcpredictor.io">signal@btcpredictor.io</a>, your email address and message content will be stored only for the purpose of responding to your enquiry and will not be shared with third parties.</li>
  </ul>
  <p>This policy is compliant with EU GDPR requirements for websites that do not process personal data. For questions about data privacy, contact: <a href="mailto:signal@btcpredictor.io">signal@btcpredictor.io</a>.</p>

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
    return redirect("/dashboard", code=301)


@app.route("/dashboard", methods=["GET"])
def dashboard():
    # Use the actual request host so API calls always go same-origin.
    # This avoids CORS issues and DNS propagation problems when accessed
    # via a custom domain (e.g. btcpredictor.io vs railway.app).
    scheme = request.headers.get("X-Forwarded-Proto", "https")
    railway_url = f"{scheme}://{request.host}"
    with open("index.html", "r") as f:
        html = f.read()
    inject = f'<script>window.RAILWAY_URL = {json.dumps(railway_url)};</script>'
    html = html.replace("</head>", inject + "\n</head>", 1)
    return html, 200, {"Content-Type": "text/html"}

# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
