import os
import time
from flask import Flask, request, jsonify
from kraken.futures import Trade, User

app = Flask(__name__)

API_KEY = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PI_XBTUSD")


def get_trade_client():
    return Trade(key=API_KEY, secret=API_SECRET)


def get_user_client():
    return User(key=API_KEY, secret=API_SECRET)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "symbol": DEFAULT_SYMBOL,
        "api_key_set": bool(API_KEY),
    })


@app.route("/balance", methods=["GET"])
def balance():
    try:
        user = get_user_client()
        result = user.get_wallets()
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route("/place-bet", methods=["POST"])
def place_bet():
    data = request.get_json(force=True) or {}

    direction = (data.get("direction") or "").upper()
    confidence = float(data.get("confidence", 0))
    symbol = data.get("symbol", DEFAULT_SYMBOL)
    size = data.get("size", data.get("stake_usdc", 10))

    try:
        size = int(float(size))
        if size < 10:
            size = 10
    except Exception:
        return jsonify({"status": "failed", "error": "invalid_size"}), 400

    if direction not in ("UP", "DOWN"):
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    side = "buy" if direction == "UP" else "sell"

    try:
        trade = get_trade_client()
        result = trade.create_order(
            orderType="mkt",
            symbol=symbol,
            side=side,
            size=size,
        )
        ok = result.get("result") == "success"
        return jsonify({
            "status": "placed" if ok else "failed",
            "direction": direction,
            "confidence": confidence,
            "symbol": symbol,
            "side": side,
            "size": size,
            "raw": result,
        }), (200 if ok else 400)
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)

@app.route("/debug-key", methods=["GET"])
def debug_key():
    return jsonify({
        "key_prefix": API_KEY[:10] if API_KEY else "EMPTY",
        "key_length": len(API_KEY),
        "secret_length": len(API_SECRET),
    })
