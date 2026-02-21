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
        total_fee = sum(float(f.get("fee", 0) or 0) for f in order_fills)
        fee_currency = order_fills[0].get("fee_currency", "USD") if order_fills else "USD"

        return jsonify({
            "order_id":     order_id,
            "total_fee":    total_fee,
            "fee_currency": fee_currency,
            "fills_found":  len(order_fills),
            "fills":        order_fills,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
