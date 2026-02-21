import os
import time
from flask import Flask, request, jsonify
from kraken.futures import Trade, User

app = Flask(__name__)

API_KEY = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PF_XBTUSD")


def get_trade_client():
    return Trade(key=API_KEY, secret=API_SECRET)


def get_user_client():
    return User(key=API_KEY, secret=API_SECRET)


def get_open_position(symbol: str) -> dict:
    """
    Legge la posizione aperta su un simbolo.
    Ritorna dict con 'side' ('long'/'short'), 'size' (float), o None se flat.
    """
    try:
        user = get_user_client()
        wallets = user.get_wallets()
        flex = wallets.get("accounts", {}).get("flex", {})
        positions = flex.get("positions", {})

        for key, pos in positions.items():
            if pos.get("symbol", "").upper() == symbol.upper():
                size = float(pos.get("quantity", 0))
                if size == 0:
                    return None
                side = "long" if pos.get("side", "long") == "long" else "short"
                return {"side": side, "size": abs(size)}
        return None
    except Exception:
        return None


# ── HEALTH ──────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "symbol": DEFAULT_SYMBOL,
        "api_key_set": bool(API_KEY),
    })


# ── DEBUG ────────────────────────────────────────────────────────────────────

@app.route("/debug-key", methods=["GET"])
def debug_key():
    return jsonify({
        "key_prefix": API_KEY[:10] if API_KEY else "EMPTY",
        "key_length": len(API_KEY),
        "secret_length": len(API_SECRET),
    })


# ── BALANCE ──────────────────────────────────────────────────────────────────

@app.route("/balance", methods=["GET"])
def balance():
    try:
        user = get_user_client()
        result = user.get_wallets()
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ── POSITION ─────────────────────────────────────────────────────────────────

@app.route("/position", methods=["GET"])
def position():
    """Endpoint per vedere la posizione aperta attuale."""
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    pos = get_open_position(symbol)
    if pos:
        return jsonify({"status": "open", "symbol": symbol, **pos})
    return jsonify({"status": "flat", "symbol": symbol})


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
                "message": "Nessuna posizione aperta, nulla da chiudere."
            })

        # Per chiudere: lato inverso rispetto alla posizione aperta
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
        return jsonify({
            "status": "closed" if ok else "failed",
            "closed_side": pos["side"],
            "close_order_side": close_side,
            "size": size,
            "raw": result,
        }), (200 if ok else 400)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


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

    # Controlla posizione aperta: se già nella stessa direzione, salta
    pos = get_open_position(symbol)
    desired_side = "long" if direction == "UP" else "short"

    if pos and pos["side"] == desired_side:
        return jsonify({
            "status": "skipped",
            "reason": f"Posizione {pos['side']} già aperta, stessa direzione. Nulla da fare.",
            "existing_position": pos,
        })

    # Se c'è una posizione nel lato opposto, la chiudiamo prima
    if pos and pos["side"] != desired_side:
        try:
            close_side = "sell" if pos["side"] == "long" else "buy"
            trade = get_trade_client()
            trade.create_order(
                orderType="mkt",
                symbol=symbol,
                side=close_side,
                size=pos["size"],
                reduceOnly=True,
            )
        except Exception as e:
            return jsonify({
                "status": "error",
                "error": f"Impossibile chiudere posizione esistente: {str(e)}"
            }), 500

    # Apri la nuova posizione
    order_side = "buy" if direction == "UP" else "sell"

    try:
        trade = get_trade_client()
        result = trade.create_order(
            orderType="mkt",
            symbol=symbol,
            side=order_side,
            size=size,
        )

        ok = result.get("result") == "success"
        return jsonify({
            "status": "placed" if ok else "failed",
            "direction": direction,
            "confidence": confidence,
            "symbol": symbol,
            "side": order_side,
            "size": size,
            "previous_position_closed": pos is not None,
            "raw": result,
        }), (200 if ok else 400)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/debug-wallet", methods=["GET"])
def debug_wallet():
    try:
        user = get_user_client()
        result = user.get_wallets()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── MAIN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
