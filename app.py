import os
import time
import base64
import hashlib
import hmac
from urllib.parse import urlencode, quote

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

def _get_env(*names):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None

BASE_URL = (_get_env("KRAKEN_FUTURES_BASE_URL") or "https://futures.kraken.com").rstrip("/")
API_KEY = _get_env("KRAKEN_FUTURES_API_KEY", "KRAKEN_API_KEY")
API_SECRET_RAW = _get_env("KRAKEN_FUTURES_API_SECRET", "KRAKEN_API_SECRET")
DEFAULT_SYMBOL = os.getenv("KRAKEN_DEFAULT_SYMBOL", "PI_XBTUSD")
V3_PREFIX = "/derivatives/api/v3"


def _nonce_ms():
    return str(int(time.time() * 1000))


def _postdata(params):
    items = sorted(params.items(), key=lambda x: x[0])
    return urlencode(items, quote_via=quote, safe="")


def _authent(postdata, nonce, endpoint_path):
    # 1. Concatenate postData + nonce + endpointPath
    # 2. SHA-256 hash the result
    # 3. Base64-decode the secret
    # 4. HMAC-SHA-512 using decoded secret on SHA-256 hash
    # 5. Base64-encode result
    msg = (postdata + nonce + endpoint_path).encode("utf-8")
    sha256_hash = hashlib.sha256(msg).digest()

    # Prova prima come base64, poi come stringa normale
    try:
        secret = base64.b64decode(API_SECRET_RAW)
    except Exception:
        secret = API_SECRET_RAW.encode("utf-8")

    sig = hmac.new(secret, sha256_hash, hashlib.sha512).digest()
    return base64.b64encode(sig).decode("utf-8")


def _private_post(full_path, params):
    if not API_KEY or not API_SECRET_RAW:
        return {"result": "error", "error": "missing_api_env"}, 500

    nonce = _nonce_ms()
    postdata = _postdata(params)
    authent = _authent(postdata, nonce, full_path)

    headers = {
        "APIKey": API_KEY,
        "Authent": authent,
        "Nonce": nonce,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    r = requests.post(f"{BASE_URL}{full_path}", data=postdata, headers=headers, timeout=20)
    try:
        return r.json(), r.status_code
    except Exception:
        return {"result": "error", "raw": r.text}, r.status_code


def _private_get(full_path):
    if not API_KEY or not API_SECRET_RAW:
        return {"result": "error", "error": "missing_api_env"}, 500

    nonce = _nonce_ms()
    # Per GET, postdata è stringa vuota
    postdata = ""
    authent = _authent(postdata, nonce, full_path)

    headers = {
        "APIKey": API_KEY,
        "Authent": authent,
        "Nonce": nonce,
    }

    r = requests.get(f"{BASE_URL}{full_path}", headers=headers, timeout=20)
    try:
        return r.json(), r.status_code
    except Exception:
        return {"result": "error", "raw": r.text}, r.status_code


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "symbol": DEFAULT_SYMBOL,
        "api_key_set": bool(API_KEY),
        "api_secret_set": bool(API_SECRET_RAW),
        "base_url": BASE_URL
    })


@app.route("/balance", methods=["GET"])
def balance():
    payload, code = _private_get(f"{V3_PREFIX}/accounts")
    return jsonify(payload), code


@app.route("/place-bet", methods=["POST"])
def place_bet():
    data = request.get_json(force=True) or {}

    direction = (data.get("direction") or "").upper()
    confidence = float(data.get("confidence", 0))
    symbol = data.get("symbol", DEFAULT_SYMBOL)
    size = data.get("size", data.get("stake_usdc", 1))

    try:
        size = int(float(size))
        if size < 1:
            size = 1
    except Exception:
        return jsonify({"status": "failed", "error": "invalid_size"}), 400

    if direction not in ("UP", "DOWN"):
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    side = "buy" if direction == "UP" else "sell"

    params = {
        "orderType": "mkt",
        "symbol": symbol,
        "side": side,
        "size": size,
    }

    payload, code = _private_post(f"{V3_PREFIX}/sendorder", params)
    ok = (code == 200) and isinstance(payload, dict) and payload.get("result") == "success"

    return jsonify({
        "status": "placed" if ok else "failed",
        "direction": direction,
        "confidence": confidence,
        "symbol": symbol,
        "side": side,
        "size": size,
        "raw": payload,
    }), (200 if ok else 400)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
