import os
import time
import base64
import hmac
import hashlib
from urllib.parse import urlencode

import requests
from flask import Flask, request, jsonify


app = Flask(__name__)

BASE_URL = os.getenv("KRAKEN_FUTURES_BASE_URL", "https://futures.kraken.com").rstrip("/")
API_KEY = os.environ["KRAKEN_FUTURES_API_KEY"]
API_SECRET_B64 = os.environ["KRAKEN_FUTURES_API_SECRET"]

# Kraken Futures v3 (Derivatives)
SEND_ORDER_PATH = "/derivatives/api/v3/sendorder"
TICKERS_PATH = "/derivatives/api/v3/tickers"


def _nonce_ms() -> str:
    return str(int(time.time() * 1000))


def _build_postdata(params: dict) -> str:
    """
    Kraken derivatives expects a querystring-like body: k=v&k2=v2
    Important: hash the *url-encoded* form (new v3 flow). :contentReference[oaicite:1]{index=1}
    """
    # Keep ordering stable (helps debugging)
    return urlencode(sorted(params.items()), safe="", doseq=True)


def _authent(postdata: str, nonce: str, endpoint_path: str, api_secret_b64: str) -> str:
    """
    authent = base64( HMAC_SHA512( base64decode(secret), SHA256(postData + nonce + endpointPath) ) )
    :contentReference[oaicite:2]{index=2}
    """
    message = (postdata + nonce + endpoint_path).encode("utf-8")
    sha256_digest = hashlib.sha256(message).digest()
    secret = base64.b64decode(api_secret_b64)
    sig = hmac.new(secret, sha256_digest, hashlib.sha512).digest()
    return base64.b64encode(sig).decode("utf-8")


def _private_post(path: str, params: dict, timeout: int = 15) -> dict:
    nonce = _nonce_ms()
    postdata = _build_postdata(params)
    authent = _authent(postdata, nonce, path, API_SECRET_B64)

    headers = {
        "APIKey": API_KEY,
        "Nonce": nonce,
        "Authent": authent,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    url = f"{BASE_URL}{path}"
    r = requests.post(url, data=postdata, headers=headers, timeout=timeout)
    # Kraken derivatives returns JSON with {result: "success"|"error", ...}
    try:
        return r.json()
    except Exception:
        return {"result": "error", "http_status": r.status_code, "raw": r.text}


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ts": int(time.time())})


@app.route("/price", methods=["GET"])
def price():
    """
    Utility: current mark/last for PI_XBTUSD (no auth).
    """
    symbol = request.args.get("symbol", "PI_XBTUSD")
    url = f"{BASE_URL}{TICKERS_PATH}"
    r = requests.get(url, params={"symbol": symbol}, timeout=10)
    try:
        return jsonify(r.json())
    except Exception:
        return jsonify({"result": "error", "http_status": r.status_code, "raw": r.text}), 500


@app.route("/place-bet", methods=["POST"])
def place_bet():
    """
    Input (from n8n):
    {
      "direction": "UP"|"DOWN",
      "confidence": 0.0-1.0,
      "size": 1,
      "symbol": "PI_XBTUSD",
      "orderType": "mkt"
    }

    direction UP -> buy (LONG), DOWN -> sell (SHORT)
    """
    data = request.get_json(force=True) or {}

    direction = (data.get("direction") or "").upper()
    confidence = float(data.get("confidence", 0))
    symbol = data.get("symbol", "PI_XBTUSD")
    order_type = data.get("orderType", "mkt")  # mkt is supported :contentReference[oaicite:3]{index=3}

    # IMPORTANT: "size" in Kraken Futures is contract size (not USDT/USDC).
    # Start with a fixed small size (e.g. 1) until you're 100% sure about sizing.
    size = data.get("size", 1)
    try:
        size = float(size)
    except Exception:
        return jsonify({"status": "error", "error": "invalid_size"}), 400

    if direction not in {"UP", "DOWN"}:
        return jsonify({"status": "error", "error": "invalid_direction"}), 400

    side = "buy" if direction == "UP" else "sell"

    # Minimal params for a market order:
    # orderType=mkt&symbol=PI_XBTUSD&side=buy|sell&size=<n>
    params = {
        "orderType": order_type,
        "symbol": symbol,
        "side": side,
        "size": size,
    }

    resp = _private_post(SEND_ORDER_PATH, params)

    # Normalize response for your Telegram template
    if resp.get("result") == "success":
        send_status = resp.get("sendStatus", {}) or {}
        return jsonify({
            "status": "placed" if send_status.get("status") == "placed" else send_status.get("status", "ok"),
            "order_id": send_status.get("order_id") or send_status.get("orderId"),
            "direction": direction,
            "side": side,
            "confidence": confidence,
            "size": size,
            "symbol": symbol,
            "raw": resp,
        })

    return jsonify({
        "status": "failed",
        "direction": direction,
        "side": side,
        "confidence": confidence,
        "size": size,
        "symbol": symbol,
        "error": resp.get("error") or resp.get("errorMessage") or resp,
        "raw": resp,
    }), 400


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
