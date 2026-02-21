import os
import time
import base64
import hashlib
import hmac
from urllib.parse import urlencode, quote

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

KRAKEN_BASE = os.getenv("KRAKEN_FUTURES_BASE", "https://futures.kraken.com").rstrip("/")
API_KEY = os.environ["KRAKEN_FUTURES_KEY"]
API_SECRET_B64 = os.environ["KRAKEN_FUTURES_SECRET"]
USE_NONCE = os.getenv("USE_NONCE", "1") == "1"

DEFAULT_SYMBOL = os.getenv("KRAKEN_DEFAULT_SYMBOL", "PI_XBTUSD")

# ---- Signing (Kraken Futures v3 /derivatives/*) ----
def _nonce_ms() -> str:
    return str(int(time.time() * 1000))

def build_postdata(params: dict) -> str:
    """
    Build x-www-form-urlencoded string with %20 for spaces (quote),
    matching Kraken's "url-encoded as it appears in the request" guidance.
    """
    # sort keys for deterministic ordering
    items = [(k, str(v)) for k, v in sorted(params.items(), key=lambda x: x[0])]
    return urlencode(items, quote_via=quote, safe="")

def sign_authent(postdata: str, endpoint_path: str, nonce: str) -> str:
    """
    Authent = base64( HMAC-SHA512( base64decode(secret), SHA256(postData + nonce + endpointPath) ) )
    """
    message = (postdata + nonce + endpoint_path).encode("utf-8")
    sha256_digest = hashlib.sha256(message).digest()
    secret = base64.b64decode(API_SECRET_B64)
    sig = hmac.new(secret, sha256_digest, hashlib.sha512).digest()
    return base64.b64encode(sig).decode().strip()

def kraken_v3_request(method: str, endpoint_path: str, params: dict | None = None):
    """
    Calls Kraken Futures v3 endpoint under /derivatives/api/v3
    Example endpoint_path: /derivatives/api/v3/sendorder
    """
    params = params or {}
    url = f"{KRAKEN_BASE}{endpoint_path}"

    nonce = _nonce_ms() if USE_NONCE else ""
    postdata = build_postdata(params) if params else ""

    authent = sign_authent(postdata, endpoint_path, nonce)

    headers = {
        "APIKey": API_KEY,
        "Authent": authent,
        "Content-Type": "application/x-www-form-urlencoded",
    }
    if USE_NONCE:
        headers["Nonce"] = nonce

    if method.upper() == "GET":
        # For GET: put params in query string AND sign the same query string
        resp = requests.get(url, params=params, headers=headers, timeout=20)
    else:
        # For POST: send body as the exact postdata string we signed
        resp = requests.post(url, data=postdata, headers=headers, timeout=20)

    return resp

# ---- Routes ----

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ts": int(time.time())})

@app.route("/auth-check", methods=["GET"])
def auth_check():
    """
    Quick way to validate your APIKey/Authent signing.
    """
    endpoint = "/derivatives/api/v3/api-keys/v3/check"
    r = kraken_v3_request("GET", endpoint, params={})
    try:
        return jsonify({"http": r.status_code, "raw": r.json()})
    except Exception:
        return jsonify({"http": r.status_code, "raw": r.text})

@app.route("/place-bet", methods=["POST"])
def place_bet():
    """
    Input from n8n:
    {
      "direction": "UP" | "DOWN",
      "confidence": 0..1,
      "stake_usdc": 1
    }

    We translate to Kraken Futures order:
    - symbol: PI_XBTUSD (default)
    - side: buy/sell
    - orderType: mkt
    - size: 1 (you can map stake->size later)
    """
    data = request.get_json(force=True) or {}
    direction = data.get("direction")
    confidence = float(data.get("confidence", 0))
    size = float(data.get("size", 1.0))  # allow override
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    if direction not in ["UP", "DOWN"]:
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    side = "buy" if direction == "UP" else "sell"

    # Kraken Futures sendorder endpoint
    endpoint = "/derivatives/api/v3/sendorder"

    # Minimal market order params (most common)
    # If your account requires "cliOrdId" you can add it.
    params = {
        "orderType": "mkt",
        "symbol": symbol,
        "side": side,
        "size": size,
    }

    r = kraken_v3_request("POST", endpoint, params=params)

    try:
        payload = r.json()
    except Exception:
        payload = {"result": "error", "error": "non_json_response", "raw": r.text}

    ok = (r.status_code == 200) and (payload.get("result") != "error")

    return jsonify({
        "status": "placed" if ok else "failed",
        "symbol": symbol,
        "side": side,
        "size": size,
        "confidence": confidence,
        "raw": payload,
        "error": payload.get("error") if isinstance(payload, dict) else None
    }), (200 if ok else 400)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
