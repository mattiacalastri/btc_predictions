import os
import time
from flask import Flask, request, jsonify
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL

app = Flask(__name__)

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
PRIVATE_KEY = os.environ["PRIVATE_KEY"]
FUNDER = os.environ["FUNDER_ADDRESS"]

client = ClobClient(
    HOST,
    key=PRIVATE_KEY,
    chain_id=CHAIN_ID,
    signature_type=2,
    funder=FUNDER
)
client.set_api_creds(client.create_or_derive_api_creds())

def find_btc_market():
    try:
        markets = client.get_markets()
        btc = [
            m for m in markets.data
            if ("bitcoin" in m.question.lower() or "btc" in m.question.lower())
            and ("above" in m.question.lower() or "higher" in m.question.lower() or "up" in m.question.lower())
            and m.active and not m.closed
        ]
        if not btc:
            return None
        btc.sort(key=lambda m: m.end_date_iso)
        return btc[0]
    except Exception as e:
        print(f"Errore find_market: {e}")
        return None

@app.route('/place-bet', methods=['POST'])
def place_bet():
    data = request.json
    direction = data.get('direction')
    confidence = float(data.get('confidence', 0))
    stake_usdc = float(data.get('stake_usdc', 1))

    if direction not in ['UP', 'DOWN']:
        return jsonify({"error": "direction non valida"}), 400

    market = find_btc_market()
    if not market:
        return jsonify({"status": "skipped", "reason": "no_btc_market_found"})

    token_index = 0 if direction == 'UP' else 1
    token_id = market.tokens[token_index].token_id

    try:
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=stake_usdc,
            side=BUY
        )
        signed = client.create_market_order(order_args)
        resp = client.post_order(signed, OrderType.FOK)

        return jsonify({
            "status": "placed" if resp.get('success') else "failed",
            "order_id": resp.get('orderID'),
            "direction": direction,
            "confidence": confidence,
            "stake_usdc": stake_usdc,
            "market": market.question,
            "error": resp.get('errorMsg')
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)

@app.route('/markets', methods=['GET'])
def list_markets():
    try:
        markets = client.get_markets()
        btc = [
            {"question": m.question, "active": m.active, "end": m.end_date_iso}
            for m in markets.data
            if "btc" in m.question.lower() or "bitcoin" in m.question.lower()
        ]
        return jsonify(btc)
    except Exception as e:
        return jsonify({"error": str(e)})
