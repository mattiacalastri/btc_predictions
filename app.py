import os
import time
import requests
import json
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
        # I mercati Up/Down usano slug con timestamp unix — cerchiamo via CLOB direttamente
        # Calcoliamo il timestamp del prossimo slot 5min
        now = int(time.time())
        # Arrotondiamo al prossimo multiplo di 300 secondi
        next_slot = ((now // 300) + 1) * 300
        slug = f"btc-updown-5m-{next_slot}"

        resp = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug})
        data = resp.json()

        if data and len(data) > 0:
            return data[0]

        # Prova anche lo slot corrente
        current_slot = (now // 300) * 300
        slug2 = f"btc-updown-5m-{current_slot}"
        resp2 = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug2})
        data2 = resp2.json()

        if data2 and len(data2) > 0:
            return data2[0]

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
        now = int(time.time())
        results = []
        # Controlla gli ultimi 3 slot e i prossimi 3
        for offset in range(-3, 4):
            slot = ((now // 300) + offset) * 300
            slug = f"btc-updown-5m-{slot}"
            resp = requests.get(f"{GAMMA_HOST}/markets", params={"slug": slug})
            data = resp.json()
            if data and len(data) > 0:
                m = data[0]
                results.append({
                    "slug": slug,
                    "question": m.get('question'),
                    "active": m.get('active'),
                    "closed": m.get('closed'),
                    "endDate": m.get('endDate')
                })
        return jsonify(results if results else {"message": "nessun mercato trovato", "now": now})
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "timestamp": int(time.time())})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
