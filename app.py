import os
import time
import hmac
import hashlib
import base64
import requests
from urllib.parse import urlencode
from flask import Flask, request, jsonify
from kraken.futures import User  # lo usiamo solo per wallet/balance

app = Flask(__name__)

API_KEY        = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET     = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PF_XBTUSD")
KRAKEN_BASE    = "https://futures.kraken.com"


# ─────────────────────────────────────────────────────────────────────────────
# Error handler: mai HTML 500, solo JSON
# ─────────────────────────────────────────────────────────────────────────────
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({
        "status": "error",
        "error_type": e.__class__.__name__,
        "error": str(e),
    }), 500


# ─────────────────────────────────────────────────────────────────────────────
# Nonce helper (Railway-safe)
# ─────────────────────────────────────────────────────────────────────────────
def get_kraken_nonce() -> str:
    try:
        r = requests.get(KRAKEN_BASE + "/derivatives/api/v3/servertime", timeout=5)
        server_time = r.json().get("serverTime", "")
        from datetime import datetime, timezone
        dt = datetime.strptime(server_time, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        return str(int(dt.timestamp() * 1000))
    except Exception:
        return str(int(time.time() * 1000))


# ─────────────────────────────────────────────────────────────────────────────
# Auth helper (Kraken Futures v3)
# Authent = Base64( HMAC-SHA512( SHA256(postData + nonce + endpointPath), Base64Decode(secret) ) )
# ─────────────────────────────────────────────────────────────────────────────
def kraken_auth_headers(endpoint_path: str, post_data: str = "") -> dict:
    nonce = get_kraken_nonce()
    message = post_data + nonce + endpoint_path
    sha256_hash = hashlib.sha256(message.encode("utf-8")).digest()
    secret_decoded = base64.b64decode(API_SECRET)
    sig = hmac.new(secret_decoded, sha256_hash, hashlib.sha512)
    authent = base64.b64encode(sig.digest()).decode()
    return {"APIKey": API_KEY, "Nonce": nonce, "Authent": authent}


def get_user_client():
    return User(key=API_KEY, secret=API_SECRET)


# ─────────────────────────────────────────────────────────────────────────────
# Core: leggi posizione aperta
# ─────────────────────────────────────────────────────────────────────────────
def get_open_position(symbol: str):
    endpoint_path = "/derivatives/api/v3/openpositions"
    headers = kraken_auth_headers(endpoint_path)
    r = requests.get(KRAKEN_BASE + endpoint_path, headers=headers, timeout=10)
    data = r.json()

    for pos in data.get("openPositions", []):
        if pos.get("symbol", "").upper() == symbol.upper():
            size = float(pos.get("size", 0))
            if abs(size) < 1e-12:
                return None
            return {
                "side": "long" if size > 0 else "short",
                "size": abs(size),
                "raw": pos,
            }
    return None


# ─────────────────────────────────────────────────────────────────────────────
# REST v3: sendorder (qui possiamo forzare timeInForce=gtc)
# Docs: POST /sendorder :contentReference[oaicite:1]{index=1}
# ─────────────────────────────────────────────────────────────────────────────
def futures_sendorder(params: dict):
    """
    In Kraken Futures v3, i parametri per gli endpoint privati vengono firmati come
    stringa urlencoded (postData) e inviati come body form-urlencoded (affidabile).
    """
    endpoint_path = "/derivatives/api/v3/sendorder"

    # IMPORTANT: firma sul payload urlencoded
    post_data = urlencode(params)

    headers = kraken_auth_headers(endpoint_path, post_data)
    headers["Content-Type"] = "application/x-www-form-urlencoded"

    r = requests.post(
        KRAKEN_BASE + endpoint_path,
        headers=headers,
        data=post_data,
        timeout=15
    )

    # Kraken ritorna JSON sempre (in caso errori spesso 4xx con json)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, {"result": "error", "http": r.status_code, "raw": r.text}


# ─────────────────────────────────────────────────────────────────────────────
# HEALTH
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "kraken_nonce": get_kraken_nonce(),
        "symbol": DEFAULT_SYMBOL,
        "api_key_set": bool(API_KEY),
        "version": "2.6.0",
    })


# ─────────────────────────────────────────────────────────────────────────────
# BALANCE
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/balance", methods=["GET"])
def balance():
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


# ─────────────────────────────────────────────────────────────────────────────
# POSITION
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/position", methods=["GET"])
def position():
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    pos = get_open_position(symbol)
    if pos:
        return jsonify({"status": "open", "symbol": symbol, **pos})
    return jsonify({"status": "flat", "symbol": symbol})


# ─────────────────────────────────────────────────────────────────────────────
# CLOSE POSITION (reduceOnly + timeInForce=gtc)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/close-position", methods=["POST"])
def close_position():
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    pos = get_open_position(symbol)
    if not pos:
        return jsonify({"status": "no_position", "message": "Nessuna posizione aperta, nulla da chiudere."})

    close_side = "sell" if pos["side"] == "long" else "buy"

    # sendorder params
    params = {
        "orderType": "mkt",
        "symbol": symbol,
        "side": close_side,
        "size": str(pos["size"]),
        "reduceOnly": "true",
        "timeInForce": "gtc",
    }

    http, result = futures_sendorder(params)
    ok = (result.get("result") == "success")

    return jsonify({
        "status": "closed" if ok else "failed",
        "http": http,
        "close_order_side": close_side,
        "size": pos["size"],
        "raw": result,
    }), (200 if ok else 400)


# ─────────────────────────────────────────────────────────────────────────────
# PLACE BET (apre posizione reale: timeInForce=gtc)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/place-bet", methods=["POST"])
def place_bet():
    data = request.get_json(force=True) or {}

    direction  = (data.get("direction") or "").upper()
    confidence = float(data.get("confidence", 0))
    symbol     = data.get("symbol", DEFAULT_SYMBOL)

    try:
        size = float(data.get("size", data.get("stake_usdc", 0.0001)))
        if size <= 0:
            size = 0.0001
    except Exception:
        return jsonify({"status": "failed", "error": "invalid_size"}), 400

    if direction not in ("UP", "DOWN"):
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    desired_side = "long" if direction == "UP" else "short"
    pos = get_open_position(symbol)

    # già stessa direzione -> skip (evita stacking)
    if pos and pos["side"] == desired_side:
        return jsonify({
            "status": "skipped",
            "reason": f"Posizione {pos['side']} già aperta nella stessa direzione.",
            "existing_position": pos,
        })

    # se opposta -> chiudi prima
    if pos and pos["side"] != desired_side:
        close_side = "sell" if pos["side"] == "long" else "buy"
        http_c, res_c = futures_sendorder({
            "orderType": "mkt",
            "symbol": symbol,
            "side": close_side,
            "size": str(pos["size"]),
            "reduceOnly": "true",
            "timeInForce": "gtc",
        })
        if res_c.get("result") != "success":
            return jsonify({
                "status": "error",
                "error": "failed_to_close_existing_position",
                "http": http_c,
                "raw": res_c,
            }), 500
        time.sleep(0.6)

    # apri nuova posizione
    order_side = "buy" if direction == "UP" else "sell"

    http_o, result = futures_sendorder({
        "orderType": "mkt",
        "symbol": symbol,
        "side": order_side,
        "size": str(size),
        "timeInForce": "gtc",
        # reduceOnly deve essere false/assente in apertura
    })

    ok = (result.get("result") == "success")
    order_id = result.get("sendStatus", {}).get("order_id")

    # verifica posizione (breve delay)
    time.sleep(0.6)
    confirmed = get_open_position(symbol)

    return jsonify({
        "status": "placed" if ok else "failed",
        "direction": direction,
        "confidence": confidence,
        "symbol": symbol,
        "side": order_side,
        "size": size,
        "order_id": order_id,
        "position_confirmed": bool(confirmed),
        "http": http_o,
        "raw": result,
    }), (200 if ok else 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
