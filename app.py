import os
import time
import pickle
import requests
from flask import Flask, request, jsonify
from kraken.futures import Trade, User

app = Flask(__name__)

# ── XGBoost direction model (caricato una volta all'avvio) ────────────────────
_XGB_MODEL = None
_XGB_FEATURE_COLS = [
    "confidence", "fear_greed_value", "rsi14", "technical_score", "hour_utc",
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

API_KEY = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PF_XBTUSD")
KRAKEN_BASE = "https://futures.kraken.com"
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() in ("true", "1", "yes")


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
            f"{supabase_url}/rest/v1/btc_predictions"
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

        fee = bet_size * exit_price * 0.00005
        pnl_net = round(pnl_gross - fee, 6)

        patch_url = f"{supabase_url}/rest/v1/btc_predictions?id=eq.{bet_id}"
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
                f"{sb_url}/rest/v1/btc_predictions"
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
        "version": "2.4.1",
        "dry_run": DRY_RUN,
        "capital": capital,
        "wallet_equity": wallet_equity,
        "base_size": base_size,
    })



# ── BALANCE ──────────────────────────────────────────────────────────────────

@app.route("/balance", methods=["GET"])
def balance():
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
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    try:
        pos = get_open_position(symbol)
        if pos:
            return jsonify({"status": "open", "symbol": symbol, **pos})
        return jsonify({"status": "flat", "symbol": symbol})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e), "symbol": symbol}), 500


# ── CLOSE POSITION ───────────────────────────────────────────────────────────

@app.route("/close-position", methods=["POST"])
def close_position():
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


# ── PLACE BET ────────────────────────────────────────────────────────────────

@app.route("/place-bet", methods=["POST"])
def place_bet():
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

    desired_side = "long" if direction == "UP" else "short"

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

        # NO stacking: se stessa direzione => skip (evita che aumenti la size)
        if pos and pos["side"] == desired_side:
            # Cerca la bet aperta su Supabase per mostrare quando è stata aperta
            existing_bet_info = {}
            try:
                sb_url = os.environ.get("SUPABASE_URL", "")
                sb_key = os.environ.get("SUPABASE_KEY", "")
                if sb_url and sb_key:
                    r = requests.get(
                        f"{sb_url}/rest/v1/btc_predictions"
                        "?select=id,created_at,direction,entry_fill_price"
                        "&bet_taken=eq.true&correct=is.null&order=id.desc&limit=1",
                        headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                        timeout=3,
                    )
                    if r.status_code == 200 and r.json():
                        row = r.json()[0]
                        existing_bet_info = {
                            "existing_bet_id": row.get("id"),
                            "existing_bet_created_at": row.get("created_at"),
                            "existing_bet_entry": row.get("entry_fill_price"),
                        }
            except Exception:
                pass
            return jsonify({
                "status": "skipped",
                "reason": f"Posizione {pos['side']} già aperta nella stessa direzione (no stacking).",
                "symbol": symbol,
                "existing_position": pos,
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

        url = f"{supabase_url}/rest/v1/btc_predictions?select=*&order=id.desc&limit={limit}"
        if days > 0:
            from datetime import datetime, timedelta, timezone
            since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            url += f"&created_at=gte.{since}"

        res = requests.get(url, headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }, timeout=10)

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
            f"{supabase_url}/rest/v1/btc_predictions"
            "?select=direction,confidence,correct,pnl_usd,created_at"
            "&bet_taken=eq.true&correct=not.is.null"
            "&order=id.desc&limit=50"
        )
        res = requests.get(url, headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }, timeout=5)
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
            f"[<0.62]→{_bucket_wr(0.50,0.62)} "
            f"[0.62-0.65]→{_bucket_wr(0.62,0.65)} "
            f"[≥0.65]→{_bucket_wr(0.65,1.01)}"
        )

        stats_text = (
            f"Last 10 bets: {w10}W/{l10}L ({wr10}%) | Streak: {streak_label} | Last5 PnL: {pnl5_str}\n"
            f"Hour {current_hour:02d}h UTC: {hour_wr}\n"
            f"Direction WR: {dir_stats}\n"
            f"Confidence calibration: {conf_stats}"
        )

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

        features = [[
            float(request.args.get("confidence", 0.62)),
            float(request.args.get("fear_greed_value", 50)),
            float(request.args.get("rsi14", 50)),
            float(request.args.get("technical_score", 0)),
            int(request.args.get("hour_utc", 12)),
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
    confidence = float(request.args.get("confidence", 0.62))

    try:
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")

        url = f"{supabase_url}/rest/v1/btc_predictions?select=correct,pnl_usd&bet_taken=eq.true&correct=not.is.null&order=id.desc&limit=10"
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

        # confidence scaling: 0.55→0.80x | 0.60→1.00x | 0.70→1.20x
        conf_mult = 0.8 + (confidence - 0.55) * (0.4 / 0.15)
        conf_mult = round(max(0.8, min(1.2, conf_mult)), 2)

        final_size = round(base_size * multiplier * conf_mult, 6)
        final_size = max(0.001, min(0.005, final_size))

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
        })

    except Exception as e:
        return jsonify({"size": base_size, "reason": "error", "error": str(e)})

# ── N8N STATUS (proxy) ───────────────────────────────────────────────────────

# ID fissi dei workflow BTC — evita paginazione su 100+ workflow nell'account
_BTC_WORKFLOW_IDS = [
    ("kaevyOIbHpm8vJmF", "01_BTC_Prediction_Bot"),
    ("vallzU6ceD5gPwSP",  "02_BTC_Trade_Checker"),
    ("KITZHsfVSMtVTpfx",  "03_BTC_Wallet_Checker"),
    ("eLmZ6d8t9slAx5pj",  "04_BTC_Talker"),
    ("xCwf53UGBq1SyP0c",  "05_BTC_Prediction_Verifier"),
    ("O2ilssVhSFs9jsMF",  "06_Nightly_Maintenance"),
]

@app.route("/n8n-status", methods=["GET"])
def n8n_status():
    """
    Proxy verso n8n API — richiede N8N_API_KEY env var su Railway.
    Carica direttamente i 6 workflow BTC per ID (evita paginazione su 100+ wf).
    """
    n8n_key = os.environ.get("N8N_API_KEY", "")
    n8n_url = os.environ.get("N8N_URL", "https://mattiacalastri.app.n8n.cloud")
    if not n8n_key:
        return jsonify({"status": "error", "error": "N8N_API_KEY not configured on Railway"}), 200

    headers = {"X-N8N-API-KEY": n8n_key}
    result = []
    try:
        for wf_id, wf_label in _BTC_WORKFLOW_IDS:
            wf_data = {"id": wf_id, "name": wf_label, "active": False}
            # Carica dettagli workflow (active/inactive)
            try:
                r = requests.get(f"{n8n_url}/api/v1/workflows/{wf_id}",
                                 headers=headers, timeout=5)
                if r.ok:
                    wf = r.json()
                    wf_data["name"]   = wf.get("name", wf_label)
                    wf_data["active"] = wf.get("active", False)
            except Exception:
                pass
            # Ultima execution
            try:
                ex_r = requests.get(
                    f"{n8n_url}/api/v1/executions?workflowId={wf_id}&limit=1",
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
            except Exception:
                pass
            result.append(wf_data)

        return jsonify({"status": "ok", "workflows": result, "ts": int(time.time())})

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)[:120]}), 200


# ── DASHBOARD ────────────────────────────────────────────────────────────────

@app.route("/dashboard", methods=["GET"])
def dashboard():
    with open("index.html", "r") as f:
        return f.read(), 200, {"Content-Type": "text/html"}

# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
