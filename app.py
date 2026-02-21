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


# ─────────────────────────────────────────────────────────────────────────────
# Global error handler: evita HTML 500, restituisce JSON con errore reale
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
# Auth helper (per endpoint GET openpositions via requests)
# ─────────────────────────────────────────────────────────────────────────────
def kraken_auth_headers(endpoint_path: str, post_data: str = "") -> dict:
    nonce = get_kraken_nonce()
    message = post_data + nonce + endpoint_path
    sha256_hash = hashlib.sha256(message.encode("utf-8")).digest()
    secret_decoded = base64.b64decode(API_SECRET)
    sig = hmac.new(secret_decoded, sha256_hash, hashlib.sha512)
    authent = base64.b64encode(sig.digest()).decode()
    return {"APIKey": API_KEY, "Nonce": nonce, "Authent": authent}


# ─────────────────────────────────────────────────────────────────────────────
# SDK clients
# ─────────────────────────────────────────────────────────────────────────────
def get_trade_client():
    return Trade(key=API_KEY, secret=API_SECRET)

def get_user_client():
    return User(key=API_KEY, secret=API_SECRET)


# ─────────────────────────────────────────────────────────────────────────────
# Helper: crea ordine MARKET forzando GTC con il nome parametro giusto per lo SDK
# ─────────────────────────────────────────────────────────────────────────────
def create_order_with_tif(trade: Trade, *, orderType: str, symbol: str, side: str, size: float, reduceOnly: bool = False):
    """
    Prova diversi nomi di parametro per impostare time-in-force.
    Obiettivo: evitare che lo SDK mandi IOC di default.
    """
    base_kwargs = dict(
        orderType=orderType,
        symbol=symbol,
        side=side,
        size=size,
        reduceOnly=reduceOnly,
    )

    # tenta timeInForce (camelCase), poi snake_case, poi tif
    attempts = [
        ("timeInForce", "gtc"),
        ("time_in_force", "gtc"),
        ("tif", "gtc"),
    ]

    last_type_error = None
    for k, v in attempts:
        try:
            return trade.create_order(**base_kwargs, **{k: v})
        except TypeError as te:
            last_type_error = te
            continue

    # Se nessun parametro è supportato, falliamo con errore chiaro
    raise TypeError(
        "Impossibile impostare time-in-force su questo SDK. "
        "Ho provato: timeInForce, time_in_force, tif. Ultimo errore: "
        + str(last_type_error)
    )


# ─────────────────────────────────────────────────────────────────────────────
# Core: leggi posizione aperta (via REST v3 openpositions)
# ─────────────────────────────────────────────────────────────────────────────
def get_open_position(symbol: str):
    endpoint_path = "/derivatives/api/v3/openpositions"
    headers = kraken_auth_headers(endpoint_path)
    response = requests.get(KRAKEN_BASE + endpoint_path, headers=headers, timeout=10)
    data = response.json()

    for pos in data.get("openPositions", []):
        if pos.get("symbol", "").upper() == symbol.upper():
            size = float(pos.get("size", 0))
            if size == 0:
                return None
            side = "long" if size > 0 else "short"
            return {"side": side, "size": abs(size), "raw": pos}

    return None


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
        "version": "2.5.0",
    })


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG KEY
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/debug-key", methods=["GET"])
def debug_key():
    return jsonify({
        "key_prefix": API_KEY[:10] if API_KEY else "EMPTY",
        "key_length": len(API_KEY),
        "secret_length": len(API_SECRET),
    })


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG POSITIONS (raw, SDK)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/debug-positions", methods=["GET"])
def debug_positions():
    trade = get_trade_client()
    result = trade.request(method="GET", uri="/derivatives/api/v3/openpositions", auth=True)
    return jsonify(result)


# ─────────────────────────────────────────────────────────────────────────────
# DEBUG WALLET (raw)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/debug-wallet", methods=["GET"])
def debug_wallet():
    user = get_user_client()
    result = user.get_wallets()
    return jsonify(result)


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
# CLOSE POSITION (reduceOnly + GTC)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/close-position", methods=["POST"])
def close_position():
    data = request.get_json(force=True) or {}
    symbol = data.get("symbol", DEFAULT_SYMBOL)

    pos = get_open_position(symbol)
    if not pos:
        return jsonify({"status": "no_position", "message": "Nessuna posizione aperta, nulla da chiudere."})

    close_side = "sell" if pos["side"] == "long" else "buy"
    trade = get_trade_client()

    result = create_order_with_tif(
        trade,
        orderType="mkt",
        symbol=symbol,
        side=close_side,
        size=pos["size"],
        reduceOnly=True,
    )

    return jsonify({
        "status": "closed",
        "close_order_side": close_side,
        "size": pos["size"],
        "raw": result,
    })


# ─────────────────────────────────────────────────────────────────────────────
# PLACE BET (apre posizione reale: GTC)
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/place-bet", methods=["POST"])
def place_bet():
    data = request.get_json(force=True) or {}

    direction  = (data.get("direction") or "").upper()
    confidence = float(data.get("confidence", 0))
    symbol     = data.get("symbol", DEFAULT_SYMBOL)

    # size = contratti (es. BTC) sul perpetual. Mantieni il tuo default.
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

    # se già nella stessa direzione → skip (evita stacking)
    if pos and pos["side"] == desired_side:
        return jsonify({
            "status": "skipped",
            "reason": f"Posizione {pos['side']} già aperta nella stessa direzione.",
            "existing_position": pos,
        })

    trade = get_trade_client()

    # se opposta → chiudi prima (reduceOnly + GTC)
    if pos and pos["side"] != desired_side:
        close_side = "sell" if pos["side"] == "long" else "buy"
        create_order_with_tif(
            trade,
            orderType="mkt",
            symbol=symbol,
            side=close_side,
            size=pos["size"],
            reduceOnly=True,
        )
        time.sleep(0.6)

    # apri nuova posizione (GTC)
    order_side = "buy" if direction == "UP" else "sell"
    result = create_order_with_tif(
        trade,
        orderType="mkt",
        symbol=symbol,
        side=order_side,
        size=size,
        reduceOnly=False,
    )

    order_id = (result or {}).get("sendStatus", {}).get("order_id")
    time.sleep(0.6)
    confirmed = get_open_position(symbol)

    return jsonify({
        "status": "placed",
        "direction": direction,
        "confidence": confidence,
        "symbol": symbol,
        "side": order_side,
        "size": size,
        "order_id": order_id,
        "position_confirmed": bool(confirmed),
        "raw": result,
    })


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
