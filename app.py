import os
import time
import requests
from flask import Flask, request, jsonify
from kraken.futures import Trade, User

app = Flask(__name__)

API_KEY = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PF_XBTUSD")
KRAKEN_BASE = "https://futures.kraken.com"


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


# ── HEALTH ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "serverTime": get_kraken_servertime(),
        "symbol": DEFAULT_SYMBOL,
        "api_key_set": bool(API_KEY),
        "version": "2.4.1",
    })


# ── DEBUG KEY ────────────────────────────────────────────────────────────────

@app.route("/debug-key", methods=["GET"])
def debug_key():
    return jsonify({
        "key_prefix": API_KEY[:10] if API_KEY else "EMPTY",
        "key_length": len(API_KEY),
        "secret_length": len(API_SECRET),
    })


# ── DEBUG POSITIONS (raw) ────────────────────────────────────────────────────

@app.route("/debug-positions", methods=["GET"])
def debug_positions():
    try:
        trade = get_trade_client()
        result = trade.request(
            method="GET",
            uri="/derivatives/api/v3/openpositions",
            auth=True
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── DEBUG WALLET (raw) ───────────────────────────────────────────────────────

@app.route("/debug-wallet", methods=["GET"])
def debug_wallet():
    try:
        user = get_user_client()
        result = user.get_wallets()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


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

    try:
        pos = get_open_position(symbol)

        # NO stacking: se stessa direzione => skip (evita che aumenti la size)
        if pos and pos["side"] == desired_side:
            return jsonify({
                "status": "skipped",
                "reason": f"Posizione {pos['side']} già aperta nella stessa direzione (no stacking).",
                "symbol": symbol,
                "existing_position": pos,
                "confidence": confidence,
                "direction": direction,
                "no_stack": True,
            }), 200

        trade = get_trade_client()

        # se opposta => chiudi prima e attendi flat
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

        # apri nuova posizione
        order_side = "buy" if direction == "UP" else "sell"
        result = trade.create_order(
            orderType="mkt",
            symbol=symbol,
            side=order_side,
            size=size,
        )

        ok = result.get("result") == "success"
        order_id = result.get("sendStatus", {}).get("order_id")

        confirmed_pos = wait_for_position(symbol, want_open=True, retries=15, sleep_s=0.35)
        position_confirmed = confirmed_pos is not None

        return jsonify({
            "status": "placed" if ok else "failed",
            "direction": direction,
            "confidence": confidence,
            "symbol": symbol,
            "side": order_side,
            "size": size,
            "order_id": order_id,
            "position_confirmed": position_confirmed,
            "position": confirmed_pos,
            "previous_position_existed": pos is not None,
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

        # ── 2. POSIZIONE APERTA ──────────────────────────────────────────────
        pos = get_open_position(symbol)

        # P&L della posizione se aperta: (mark - entry) * size * direction
        position_pnl = None
        position_pnl_pct = None
        if pos:
            try:
                tickers = trade.request(
                    method="GET",
                    uri="/derivatives/api/v3/tickers",
                    auth=False
                ).get("tickers", [])
                ticker = next(
                    (t for t in tickers if (t.get("symbol") or "").upper() == symbol.upper()),
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
            tickers = trade.request(
                method="GET",
                uri="/derivatives/api/v3/tickers",
                auth=False
            ).get("tickers", [])
            ticker_btc = next(
                (t for t in tickers if (t.get("symbol") or "").upper() == "PF_XBTUSD"),
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
            for f in symbol_fills:
                fee = float(f.get("fee", 0) or 0)
                pnl_f = float(f.get("pnl", 0) or 0)
                realized_pnl_recent += pnl_f
                recent_fills.append({
                    "order_id":  f.get("order_id"),
                    "side":      f.get("side"),
                    "size":      f.get("size"),
                    "price":     f.get("price"),
                    "pnl":       pnl_f,
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
        limit = request.args.get("limit", 500)
        supabase_url = os.environ.get("SUPABASE_URL", "")
        supabase_key = os.environ.get("SUPABASE_KEY", "")

        if not supabase_url or not supabase_key:
            return jsonify({"error": "Supabase credentials not configured"}), 500

        url = f"{supabase_url}/rest/v1/btc_predictions?select=*&order=id.desc&limit={limit}"
        res = requests.get(url, headers={
            "apikey": supabase_key,
            "Authorization": f"Bearer {supabase_key}"
        }, timeout=10)

        return jsonify(res.json()), res.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
