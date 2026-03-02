#!/usr/bin/env python3
"""Update wf01B: fix confidence stuck at 0.55 + reduce blocked hours."""
import ssl, urllib.request, json, os, certifi
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ctx = ssl.create_default_context(cafile=certifi.where())
n8n_host = os.environ['N8N_HOST']
n8n_key = os.environ['N8N_API_KEY']

# Get workflow
url = f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq'
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': n8n_key})
data = json.loads(urllib.request.urlopen(req, context=ctx, timeout=15).read())

# Fix 1: Update LLM prompt
for node in data['nodes']:
    if node.get('name') == 'BTC Prediction Bot':
        prompt = node['parameters']['text']
        s = prompt.find('CONFIDENCE CALIBRATION RULES')
        e = prompt.find('\u2501\u2501\u2501 ANTI-BIAS CHECK')
        if s > 0 and e > s:
            new_cal = (
                "CONFIDENCE CALIBRATION RULES \u2501\u2501\u2501\n"
                "CONFIDENCE = how strong the signal is. Use the FULL range 0.50-0.90.\n\n"
                "1. RSI extreme values CONFIRM the trend:\n"
                "   - RSI < 25 + EMA BEAR = strong DOWN, confidence 0.65+\n"
                "   - RSI > 75 + EMA BULL = strong UP, confidence 0.65+\n"
                "   - RSI near 50 = neutral, use other indicators\n\n"
                "2. Confidence PROPORTIONAL to signal strength:\n"
                "   - 0.50-0.54 = genuinely conflicting signals\n"
                "   - 0.55-0.62 = mild directional signal\n"
                "   - 0.63-0.72 = clear signal, 3+ indicators agree\n"
                "   - 0.73-0.85 = strong convergence across all dimensions\n"
                "   - 0.86-0.90 = exceptional convergence (rare but valid)\n\n"
                "3. NEVER default to 0.55. Ask yourself: is this truly 50/50?\n"
                "   Most conditions have SOME directional bias - reflect it.\n\n"
                "4. force_no_bet does NOT cap confidence. Give honest estimate.\n\n"
            )
            new_prompt = prompt[:s - 4] + new_cal + prompt[e:]
            new_prompt = new_prompt.replace(
                '"confidence": <number 0.50\u20130.80>',
                '"confidence": <number 0.50\u20130.90>'
            )
            node['parameters']['text'] = new_prompt
            print(f"Prompt: {len(prompt)} -> {len(new_prompt)} chars")

# Fix 2: Update blocked hours
for node in data['nodes']:
    if node.get('name') == 'Anti-Noise Filter':
        code = node['parameters']['jsCode']
        new_code = code.replace(
            'const BLOCKED_HOURS_UTC = [0, 1, 5, 7, 10, 11];',
            'const BLOCKED_HOURS_UTC = [5, 10];'
        )
        if new_code != code:
            node['parameters']['jsCode'] = new_code
            print("Anti-Noise: blocked hours [0,1,5,7,10,11] -> [5,10]")

# Save — n8n requires name, nodes, connections, settings
# Strip settings to only allowed keys
allowed_settings = {}
for k in ('executionOrder', 'saveManualExecutions', 'callerPolicy',
          'errorWorkflow', 'timezone', 'saveExecutionProgress'):
    if k in data.get('settings', {}):
        allowed_settings[k] = data['settings'][k]

payload = json.dumps({
    'name': data['name'],
    'nodes': data['nodes'],
    'connections': data['connections'],
    'settings': allowed_settings,
}).encode()

print(f"Payload: {len(payload)} bytes")

save_req = urllib.request.Request(
    f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq',
    data=payload, method='PUT',
    headers={'X-N8N-API-KEY': n8n_key, 'Content-Type': 'application/json'}
)
try:
    resp = urllib.request.urlopen(save_req, context=ctx, timeout=30)
    result = json.loads(resp.read())
    print(f"SAVED! Updated: {result.get('updatedAt', '?')}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"Error {e.code}: {body[:500]}")
