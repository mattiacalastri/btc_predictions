import os
import time
from flask import Flask, request, jsonify
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

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
        response = client.get_markets()
        data = response if isinstance(response, list) else response.get('data', [])
        btc = [
            m for m in data
            if ("bitcoin" in str(m.get('question', '')).lower() or "btc" in str(m.get('question', '')).lower())
            and ("above" in str(m.get('question', '')).lower() or "higher" in str(m.get('question', '')).lower() or "up" in str(m.get('question', '')).lower() or "over" in str(m.get('question', '')).lower())
            and m.get('active') and not m.get('closed')
        ]
        if not btc:
            return None
        btc.sort(key=lambda m: m.get('endDateIso', ''))
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
    token_id = market['tokens'][token_index]['token_id']

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
            "market": market.get('question', ''),
            "error": resp.get('errorMsg')
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/markets', methods=['GET'])
def list_markets():
    try:
        response = client.get_markets()
        data = response if isinstance(response, list) else response.get('data', [])
        btc = [
            {"question": m.get('question', ''), "active": m.get('active'), "end": m.get('endDateIso', '')}
            for m in data
            if "btc" in str(m.get('question', '')).lower() or "bitcoin" in str(m.get('question', '')).lower()
        ]
        return jsonify(btc)
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
