#!/usr/bin/env python3
"""CRITICAL: Fix 'If Confidence > 62' node.

The node compares $json.confidence >= $json.effective_threshold
but effective_threshold is computed in Anti-Noise Filter which runs AFTER
this node. So effective_threshold is undefined → all signals go to SKIP.

Fix: Change rightValue to fixed 0.50 so all signals pass through.
The Anti-Noise Filter handles real threshold filtering downstream.
"""
import ssl, urllib.request, json, os, certifi
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ctx = ssl.create_default_context(cafile=certifi.where())
n8n_host = os.environ['N8N_HOST']
n8n_key = os.environ['N8N_API_KEY']

url = f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq'
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': n8n_key})
data = json.loads(urllib.request.urlopen(req, context=ctx, timeout=15).read())

changes = []

for node in data['nodes']:
    if node.get('name') == 'If Confidence > 62':
        conds = node['parameters']['conditions']['conditions']
        for cond in conds:
            old_right = cond.get('rightValue', '?')
            # Change from dynamic expression to fixed 0.50
            cond['rightValue'] = 0.50
            # Ensure operator is gte (greater than or equal)
            if isinstance(cond.get('operator'), dict):
                cond['operator']['operation'] = 'gte'
            print(f"  rightValue: {old_right} → 0.50")
            print(f"  operator: gte")
            changes.append(f"If Confidence > 62: {old_right} → 0.50 (fixed)")
        break

if not changes:
    print("ERROR: Node not found!")
    exit(1)

for c in changes:
    print(f"  {c}")

# Save
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

save_req = urllib.request.Request(
    f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq',
    data=payload, method='PUT',
    headers={'X-N8N-API-KEY': n8n_key, 'Content-Type': 'application/json'}
)
try:
    resp = urllib.request.urlopen(save_req, context=ctx, timeout=30)
    result = json.loads(resp.read())
    print(f"\nSAVED! Updated: {result.get('updatedAt', '?')}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"Error {e.code}: {body[:500]}")
