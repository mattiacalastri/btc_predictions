import os
import time
import hmac
import hashlib
import base64
import requests
from flask import Flask, request, jsonify
from kraken.futures import Trade, User

app = Flask(__name__)

API_KEY        = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET     = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PF_XBTUSD")
KRAKEN_BASE    = "https://futures.kraken.com"


# ─────────────────────────────────────────────
# TIME SYNC (Railway-safe nonce)
# ─────────────────────────────────────────────

def get_kraken_nonce() -> str:
    try:
        r = requests.get(KRAKEN_BASE + "/derivatives/api/v3/servertime", timeout=5)
        server_time = r.json()["serverTime"]

        from datetime import datetime, timezone
        dt = datetime.strptime(server_time, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        return str(int(dt.timestamp() * 1000))

    except Exception:
        return str(int(time.time() * 1000))


def kraken_auth_headers(endpoint_path: str, post_data: str = "") -> dict:
    nonce = get_kraken_nonce()
    message = post_data + nonce + endpoint_path

    sha256_hash = hashlib.sha256(message.encode("utf-8")).digest()
    secret_decoded = base64.b64decode(API_SECRET)
    sig = hmac.new(secret_decoded, sha256_hash, hashlib.sha512)
    authent = base64.b64encode(sig.digest()).decode()

    return {"APIKey": API_KEY, "Nonce": nonce, "Authent": authent}


def get_trade_client():
    return Trade(key=API_KEY, secret=API_SECRET)


def get_user_client():
    return User(key=API_KEY, secret=API_SECRET)


# ─────────────────────────────────────────────
# POSITION READER (REAL POSITION ONLY)
# ─────────────────────────────────────────────

def get_open_position(symbol: str):
    endpoint = "/derivatives/api/v3/openpositions"
    headers = kraken_auth_headers(endpoint)

    r = requests.get(KRAKEN_BASE + endpoint, headers=headers, timeout=10)
    data = r.json()

    for pos in data.get("openPositions", []):
        if pos["symbol"].upper() == symbol.upper():
            size = float(pos["size"])
            if abs(size) < 1e-10:
                return None

            return {
                "side": "long" if size > 0 else "short",
                "size": abs(size),
                "raw": pos
            }

    return None


# ─────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────

@app.route("/health")
def health():
    return jsonify({"status": "ok", "symbol": DEFAULT_SYMBOL})


# ─────────────────────────────────────────────
# POSITION STATUS
# ─────────────────────────────────────────────

@app.route("/position")
def position():
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    pos = get_open_position(symbol)

    if pos:
        return jsonify({"status": "open", **pos})

    return jsonify({"status": "flat"})


# ─────────────────────────────────────────────
# FORCE CLOSE POSITION
# ─────────────────────────────────────────────

@app.route("/close-position", methods=["POST"])
def close_position():
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    pos = get_open_position(symbol)
    if not pos:
        return jsonify({"status": "no_position"})

    close_side = "sell" if pos["side"] == "long" else "buy"

    trade = get_trade_client()

    result = trade.create_order(
        orderType="mkt",
        symbol=symbol,
        side=close_side,
        size=pos["size"],
        reduceOnly=True,
        timeInForce="gtc"  # 🔥 CRITICAL FIX (NO IOC)
    )

    return jsonify({"status": "closed", "raw": result})


# ─────────────────────────────────────────────
# PLACE BET (REAL POSITION OPEN)
# ─────────────────────────────────────────────

@app.route("/place-bet", methods=["POST"])
def place_bet():
    data = request.get_json(force=True)

    direction = data["direction"].upper()
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    size = float(data.get("size", 0.0001))
    if size <= 0:
        return jsonify({"error": "invalid size"}), 400

    desired_side = "long" if direction == "UP" else "short"

    existing = get_open_position(symbol)

    trade = get_trade_client()

    # Close opposite first
    if existing and existing["side"] != desired_side:
        close_side = "sell" if existing["side"] == "long" else "buy"

        trade.create_order(
            orderType="mkt",
            symbol=symbol,
            side=close_side,
            size=existing["size"],
            reduceOnly=True,
            timeInForce="gtc"
        )

        time.sleep(0.5)

    # Already aligned
    if existing and existing["side"] == desired_side:
        return jsonify({"status": "already_in_position"})

    order_side = "buy" if direction == "UP" else "sell"

    result = trade.create_order(
        orderType="mkt",
        symbol=symbol,
        side=order_side,
        size=size,
        timeInForce="gtc"  # 🔥 THIS CREATES REAL POSITION
    )

    # verify position exists
    time.sleep(0.5)
    confirmed = get_open_position(symbol)

    return jsonify({
        "status": "placed",
        "position_confirmed": bool(confirmed),
        "raw": result
    })


# ─────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
