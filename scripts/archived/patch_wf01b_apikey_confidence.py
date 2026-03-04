#!/usr/bin/env python3
"""
Patch wf01B: Fix wrong API key + confidence source (Session 79 continuation)

TWO CRITICAL BUGS FOUND:
1. All 6 HTTP nodes calling Railway used wrong API key
   instead of correct BOT_API_KEY → 403 Unauthorized on every call
2. "Open Position" node read confidence from LLM output instead of
   Anti-Noise Filter recalibrator → always sent 0.55 to /place-bet

These two bugs combined meant:
- No signal could EVER reach /place-bet (blocked by 403)
- Even if it did, the raw 0.55 would fail CONF_THRESHOLD (0.56)

Fix applied: 2 March 2026 ~08:10 UTC via n8n API
"""
from dotenv import load_dotenv
load_dotenv('/Users/mattiacalastri/btc_predictions/.env')
import os, json, urllib.request, ssl, certifi

ctx = ssl.create_default_context(cafile=certifi.where())
api_key = os.environ['N8N_API_KEY']
host = os.environ['N8N_HOST']

OLD_KEY = os.environ.get('BOT_API_KEY_OLD', '')  # vecchia chiave da sostituire nei nodi n8n
NEW_KEY = os.environ['BOT_API_KEY']  # chiave corrente — NEVER hardcode

WF_ID = "OMgFa9Min4qXRnhq"  # 01B_BTC_Prediction_Bot

url = f"https://{host}/api/v1/workflows/{WF_ID}"
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': api_key})
wf = json.loads(urllib.request.urlopen(req, context=ctx).read())

fixes = []

for node in wf.get('nodes', []):
    nname = node.get('name', '')
    params = node.get('parameters', {})
    params_str = json.dumps(params)

    # Fix 1: Replace wrong API key
    if OLD_KEY in params_str:
        params_str = params_str.replace(OLD_KEY, NEW_KEY)
        node['parameters'] = json.loads(params_str)
        fixes.append(f"API key: {nname}")

    # Fix 2: Confidence source in Open Position
    if nname == 'Open Position':
        jb = node['parameters'].get('jsonBody', '')
        old = "$('BTC Prediction Bot').item.json.output.confidence"
        new = "$('Anti-Noise Filter').item.json.confidence"
        if old in jb:
            node['parameters']['jsonBody'] = jb.replace(old, new)
            fixes.append(f"Confidence source: {nname}")

if not fixes:
    print("Nothing to fix — already patched")
    exit(0)

payload = json.dumps({
    "name": wf['name'],
    "nodes": wf['nodes'],
    "connections": wf['connections'],
    "settings": {"executionOrder": wf.get('settings', {}).get('executionOrder', 'v1')}
}).encode()

update_req = urllib.request.Request(url, data=payload, headers={
    'X-N8N-API-KEY': api_key, 'Content-Type': 'application/json'
}, method='PUT')
urllib.request.urlopen(update_req, context=ctx)

print(f"Applied {len(fixes)} fixes:")
for f in fixes:
    print(f"  - {f}")
