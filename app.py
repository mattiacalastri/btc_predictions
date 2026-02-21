import os
import time
import requests
from flask import Flask, request, jsonify
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

app = Flask(__name__)

HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"
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


def find_btc_5min_market():
    try:
        # Cerca eventi con slug che contiene "btc-updown-5m"
        resp = requests.get(
            f"{GAMMA_HOST}/events",
            params={
                "active": "true",
                "closed": "false",
                "limit": 20
            }
        )
        events = resp.json()

        for event in events:
            slug = event.get('slug', '')
            title = event.get('title', '')
            if 'btc' in slug.lower() and ('updown' in slug.lower() or 'up-down' in slug.lower() or '5m' in slug.lower()):
                markets = event.get('markets', [])
                for market in markets:
                    question = market.get('question', '').lower()
                    if 'up' in question or 'higher' in question:
                        return market
                # Se non trovato per question, prendi il primo mercato
                if markets:
                    return markets[0]

        return None
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

    market = find_btc_5min_market()
    if not market:
        return jsonify({"status": "skipped", "reason": "no_btc_5min_market_found"})

    clob_token_ids = market.get('clobTokenIds', '[]')
    if isinstance(clob_token_ids, str):
        import json
        clob_token_ids = json.loads(clob_token_ids)

    if len(clob_token_ids) < 2:
        return jsonify({"status": "skipped", "reason": "token_ids_not_found", "market_raw": market})

    token_index = 0 if direction == 'UP' else 1
    token_id = clob_token_ids[token_index]

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
            "token_id": token_id,
            "error": resp.get('errorMsg')
        })
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


@app.route('/markets', methods=['GET'])
def list_markets():
    try:
        resp = requests.get(
            f"{GAMMA_HOST}/events",
            params={"active": "true", "closed": "false", "limit": 20}
        )
        events = resp.json()
        btc = [
            {"slug": e.get('slug'), "title": e.get('title'), "markets_count": len(e.get('markets', []))}
            for e in events
            if 'btc' in e.get('slug', '').lower() or 'bitcoin' in e.get('title', '').lower()
        ]
        return jsonify({"btc_events": btc, "all_slugs": [e.get('slug') for e in events]})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
