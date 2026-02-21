import os
import time
import hmac
import hashlib
import base64
import requests

from urllib.parse import urlencode, quote
from flask import Flask, request, jsonify
from kraken.futures import Trade, User

app = Flask(__name__)

API_KEY        = os.environ.get("KRAKEN_FUTURES_API_KEY", "")
API_SECRET_B64 = os.environ.get("KRAKEN_FUTURES_API_SECRET", "")
DEFAULT_SYMBOL = os.environ.get("KRAKEN_DEFAULT_SYMBOL", "PF_XBTUSD")
KRAKEN_BASE    = "https://futures.kraken.com"

# -----------------------------------------------------------------------------
# Nonce helper (Railway clock safety): use Kraken serverTime as source
# -----------------------------------------------------------------------------
def get_kraken_nonce() -> str:
    try:
        r = requests.get(f"{KRAKEN_BASE}/derivatives/api/v3/servertime", timeout=5)
        server_time = r.json().get("serverTime", "")
        # e.g. "2026-02-21T15:24:12.482Z"
        from datetime import datetime, timezone
        dt = datetime.strptime(server_time, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        return str(int(dt.timestamp() * 1000))
    except Exception:
        return str(int(time.time() * 1000))

# -----------------------------------------------------------------------------
# Auth helper (Kraken Derivatives v3):
# authent = Base64( HMAC-SHA512( SHA256(postData + nonce + endpointPath), Base64Decode(secret) ) )
# NOTE: postData MUST be url-encoded exactly as sent in request (per 2024/2025 guidance)
# -----------------------------------------------------------------------------
def _secret_decoded() -> bytes:
    if not API_SECRET_B64:
        return b""
    return base64.b64decode(API_SECRET_B64)

def build_post_data(params: dict) -> str:
    """
    Build x-www-form-urlencoded body using %20 for spaces (not '+') to match
    'url-encoded as it appears in the request' requirement.
    """
    if not params:
        return ""
    # quote_via=quote => spaces become %20
    return urlencode(params, doseq=True, quote_via=quote, safe="")

def kraken_auth_headers(endpoint_path: str, post_data: str = "") -> dict:
    nonce = get_kraken_nonce()
    message = (post_data or "") + nonce + endpoint_path
    sha256_hash = hashlib.sha256(message.encode("utf-8")).digest()
    sig = hmac.new(_secret_decoded(), sha256_hash, hashlib.sha512)
    authent = base64.b64encode(sig.digest()).decode()

    return {
        "APIKey": API_KEY,
        "Nonce": nonce,
        "Authent": authent,
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "n8n-kraken-bot/1.0",
    }

# -----------------------------------------------------------------------------
# SDK clients (used for wallet) - for trading we use raw REST to avoid SDK limits
# -----------------------------------------------------------------------------
def get_trade_client():
    return Trade(key=API_KEY, secret=API_SECRET_B64)

def get_user_client():
    return User(key=API_KEY, secret=API_SECRET_B64)

# -----------------------------------------------------------------------------
# Core: open positions (list) + single position helper
# -----------------------------------------------------------------------------
def get_open_positions() -> list:
    endpoint_path = "/derivatives/api/v3/openpositions"
    headers = kraken_auth_headers(endpoint_path, post_data="")
    r = requests.get(f"{KRAKEN_BASE}{endpoint_path}", headers=headers, timeout=10)
    data = r.json() if r.ok else {"result": "error", "http": r.status_code, "text": r.text}
    positions = []
    for pos in data.get("openPositions", []) or []:
        try:
            size = float(pos.get("size", 0) or 0)
        except Exception:
            size = 0.0
        if size == 0:
            continue
        positions.append({
            "symbol": (pos.get("symbol") or "").upper(),
            "side": (pos.get("side") or "").lower(),   # "long" / "short"
            "size": abs(size),
            "raw": pos,
        })
    return positions

def get_open_position(symbol: str) -> dict | None:
    symbol_u = (symbol or "").upper()
    for p in get_open_positions():
        if p["symbol"] == symbol_u:
            return {"side": p["side"], "size": p["size"], "symbol": p["symbol"]}
    return None

def auto_detect_position(preferred_symbol: str | None = None) -> dict | None:
    """
    If preferred_symbol not found, but there is exactly ONE open position, return it.
    This fixes the common PF vs PI mismatch problem.
    """
    if preferred_symbol:
        pos = get_open_position(preferred_symbol)
        if pos:
            return pos

    positions = get_open_positions()
    if len(positions) == 1:
        p = positions[0]
        return {"side": p["side"], "size": p["size"], "symbol": p["symbol"]}

    return None

# -----------------------------------------------------------------------------
# Core: send order (raw REST)
# -----------------------------------------------------------------------------
def send_order(symbol: str, side: str, size: float, order_type: str = "mkt", reduce_only: bool = False) -> dict:
    endpoint_path = "/derivatives/api/v3/sendorder"
    params = {
        "orderType": order_type,   # "mkt"
        "symbol": symbol,
        "side": side,              # "buy" / "sell"
        "size": str(size),
    }
    if reduce_only:
        params["reduceOnly"] = "true"

    post_data = build_post_data(params)
    headers = kraken_auth_headers(endpoint_path, post_data=post_data)

    r = requests.post(f"{KRAKEN_BASE}{endpoint_path}", data=post_data, headers=headers, timeout=15)
    try:
        data = r.json()
    except Exception:
        data = {"result": "error", "error": "non_json_response", "http": r.status_code, "text": r.text}

    data["_http_status"] = r.status_code
    return data

def wait_position_sync(symbol: str, max_wait_s: float = 3.0, step_s: float = 0.4) -> bool:
    """Poll openpositions a few times to let Kraken update."""
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        if get_open_position(symbol):
            return True
        time.sleep(step_s)
    return False

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "ts": int(time.time()),
        "kraken_nonce": get_kraken_nonce(),
        "symbol": DEFAULT_SYMBOL,
        "api_key_set": bool(API_KEY),
        "api_secret_set": bool(API_SECRET_B64),
        "version": "3.0.0",
    })

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
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/open-positions", methods=["GET"])
def open_positions():
    try:
        return jsonify({"status": "ok", "positions": get_open_positions()})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/position", methods=["GET"])
def position():
    symbol = request.args.get("symbol", DEFAULT_SYMBOL)
    try:
        pos = auto_detect_position(symbol)
        if pos:
            return jsonify({"status": "open", **pos})
        return jsonify({"status": "flat", "symbol": (symbol or DEFAULT_SYMBOL)})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/close-position", methods=["POST"])
def close_position():
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    try:
        pos = auto_detect_position(symbol)
        if not pos:
            return jsonify({
                "status": "no_position",
                "message": "Nessuna posizione aperta (sul symbol richiesto o unica posizione auto-detect).",
                "requested_symbol": symbol,
                "open_positions": get_open_positions(),
            })

        close_side = "sell" if pos["side"] == "long" else "buy"
        size = float(pos["size"])

        raw = send_order(
            symbol=pos["symbol"],
            side=close_side,
            size=size,
            order_type="mkt",
            reduce_only=True,
        )

        ok = raw.get("result") == "success"
        # let Kraken update
        wait_position_sync(pos["symbol"], max_wait_s=2.0, step_s=0.4)

        return jsonify({
            "status": "closed" if ok else "failed",
            "requested_symbol": symbol,
            "closed_symbol": pos["symbol"],
            "closed_side": pos["side"],
            "close_order_side": close_side,
            "size": size,
            "raw": raw,
        }), (200 if ok else 400)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route("/place-bet", methods=["POST"])
def place_bet():
    """
    Default behavior: NO-STACK.
    - If any position exists on the symbol -> close it first (even same direction), then open new.
    This prevents positions from accumulating into one net position.
    """
    data = request.get_json(force=True) or {}
    direction  = (data.get("direction") or "").upper()
    confidence = float(data.get("confidence", 0) or 0)
    symbol     = (data.get("symbol") or DEFAULT_SYMBOL).upper()

    # NO-STACK (default True)
    no_stack = bool(data.get("no_stack", True))

    try:
        size = float(data.get("size", data.get("stake_usdc", 0.0001)))
        if size <= 0:
            size = 0.0001
    except Exception:
        return jsonify({"status": "failed", "error": "invalid_size"}), 400

    if direction not in ("UP", "DOWN"):
        return jsonify({"status": "failed", "error": "invalid_direction"}), 400

    desired_side = "long" if direction == "UP" else "short"
    order_side = "buy" if direction == "UP" else "sell"

    try:
        existing = get_open_position(symbol)

        # If NO-STACK, close any existing position first (even same direction)
        if no_stack and existing:
            close_side = "sell" if existing["side"] == "long" else "buy"
            _ = send_order(
                symbol=symbol,
                side=close_side,
                size=float(existing["size"]),
                order_type="mkt",
                reduce_only=True,
            )
            # wait for sync
            time.sleep(0.6)

        # Open new position
        raw = send_order(
            symbol=symbol,
            side=order_side,
            size=size,
            order_type="mkt",
            reduce_only=False,
        )

        ok = raw.get("result") == "success"
        order_id = (raw.get("sendStatus") or {}).get("order_id")

        # Optional confirmation (helps debugging + n8n logic)
        position_confirmed = wait_position_sync(symbol, max_wait_s=3.0, step_s=0.4)

        return jsonify({
            "status": "placed" if ok else "failed",
            "direction": direction,
            "confidence": confidence,
            "symbol": symbol,
            "desired_position": desired_side,
            "side": order_side,
            "size": size,
            "order_id": order_id,
            "no_stack": no_stack,
            "previous_position_existed": existing is not None,
            "position_confirmed": position_confirmed,
            "raw": raw,
        }), (200 if ok else 400)

    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
