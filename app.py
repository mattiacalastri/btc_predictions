import os
import time
import base64
import hashlib
import hmac
from urllib.parse import urlencode, quote

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ---- ENV (accept multiple names to avoid crashes) ----
def _get_env(*names):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None

BASE_URL = (_get_env("KRAKEN_FUTURES_BASE_URL", "KRAKEN_FUTURES_BASE", "KRAKEN_BASE_URL") or "https://futures.kraken.com").rstrip("/")

API_KEY = _get_env("KRAKEN_FUTURES_API_KEY", "KRAKEN_API_KEY", "KRAKEN_FUTURES_KEY")
API_SECRET_B64 = _get_env("KRAKEN_FUTURES_API_SECRET", "KRAKEN_API_SECRET", "KRAKEN_FUTURES_SECRET")

DEFAULT_SYMBOL = os.getenv("KRAKEN_DEFAULT_SYMBOL", "PI_XBTUSD")

# Kraken Futures v3 base prefix
V3_PREFIX = "/derivatives/api/v3"

def _nonce_ms():
    return str(int(time.time() * 1000))

def _postdata(params):
    # build a stable x-www-form-urlencoded string
    items = [(k, str(v)) for k, v in sorted(params.items(), key=lambda x: x[0])]
    return urlencode(items, quote_via=quote, safe="")

def _authent(postdata, nonce, endpoint_path, api_secret_b64):
    # Authent = base64( HMAC_SHA512( base64decode(secret), SHA256(postData + nonce + endpointPath) ) )
    msg = (postdata + nonce + endpoint_path).encode("utf-8")
    sha = hashlib.sha256(msg).digest()
    secret = base64.b64decode(api_secret_b64)
    sig = hmac.new(secret, sha, hashlib.sha512).digest()
    return base64.b64encode(sig).decode("utf-8")

def _private_post(full_path, params):
    """
    full_path must be the EXACT path part used in the request, e.g.
    /derivatives/api/v3/sendorder
    """
    if not API_KEY or not API_SECRET_B64:
        return {
            "result": "error",
            "error": "missing_api_env",
            "hint": "Set KRAKEN_FUTURES_API_KEY and KRAKEN_FUTURES_API_SECRET in Railway variables."
        }, 500

    nonce = _nonce_ms()
    postdata = _postdata(params)
    authent = _authent(postdata, nonce, full_path, API_SECRET_B64)

    headers = {
        "APIKey": API_KEY,
        "Authent": authent,
        "Nonce": nonce,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    url = f"{BASE_URL}{full_path}"
    r = requests.post(url, data=postdata, headers=headers, timeout=20)

    try:
        return r.json(), r.status_code
    except Exception:
        return {"result": "error", "http_status": r.status_code, "raw": r.text}, r.status_code

def _private_get(full_path, params=None):
    if not API_KEY or not API_SECRET_B64:
        return {
            "result": "error",
            "error": "missing_api_env",
            "hint": "Set KRAKEN_FUTURES_API_KEY and KRAKEN_FUTURES_API_SECRET in Railway variables."
        }, 500

    params = params or {}
    nonce = _nonce_ms()
    # For GET, Kraken expects postData as the querystring formatted the same way
    postdata = _postdata(params) if params else ""
    authent = _authent(postdata, nonce, full_path, API_SECRET_B64)

    headers = {
        "APIKey": API_KEY,
        "Authent": authent,
        "Nonce": nonce,
    }

    url = f"{BASE_URL}{full_path}"
    r = requests.get(url, params=params, headers=headers, timeout=20)

    try:
        return r.json(), r.status_code
    except Exception:
        return {"result": "error", "http_status": r.status_code, "raw": r.text}, r.status_code

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "ts": int(time.time())})

@app.route("/auth-check", methods=["GET"])
def auth_check():
    # Official: GET /api-keys/v3/check under /derivatives/api/v3
    full_path = f"{V3_PREFIX}/api-keys/v3/check"
    payload, code = _private_get(full_path, params={})
    return jsonify(payload), code

@app.route("/place-bet", methods=["POST"])
def place_bet():
    data = request.get_json(force=True) or {}

    direction = (data.get("direction") or "").upper()
    confidence = float(data.get("confidence", 0))
    symbol = data.get("symbol", DEFAULT_SYMBOL)
    size = data.get("size", 1)

    try:
        size = float(size)
    except Exception:
        return jsonify({"status": "failed", "error": "invalid_size"}), 400

    if direction not in ("UP", "DOWN"):
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    side = "buy" if direction == "UP" else "sell"

    full_path = f"{V3_PREFIX}/sendorder"
    params = {
        "orderType": "mkt",
        "symbol": symbol,
        "side": side,
        "size": size,
    }

    payload, code = _private_post(full_path, params)

    ok = (code == 200) and isinstance(payload, dict) and payload.get("result") != "error"

    return jsonify({
        "status": "placed" if ok else "failed",
        "direction": direction,
        "confidence": confidence,
        "symbol": symbol,
        "side": side,
        "size": size,
        "raw": payload,
        "error": payload.get("error") if isinstance(payload, dict) else None
    }), (200 if ok else 400)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
