#!/usr/bin/env python3
"""CRITICAL FIX: Lower confidence gate + reactivate wf08.

Root cause: "If Confidence > 62" node blocks ALL signals because
LLM outputs 0.55. The Anti-Noise Filter (which has the recalibrator)
is AFTER this gate, so it never runs.

Fix: Lower the gate to >= 0.50 so all signals reach the Anti-Noise Filter,
which handles the real confidence-based filtering.
"""
import ssl, urllib.request, json, os, certifi
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ctx = ssl.create_default_context(cafile=certifi.where())
n8n_host = os.environ['N8N_HOST']
n8n_key = os.environ['N8N_API_KEY']

# ── Patch 1: Fix "If Confidence > 62" gate in wf01B ──
print("=== Patching wf01B confidence gate ===")
url = f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq'
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': n8n_key})
data = json.loads(urllib.request.urlopen(req, context=ctx, timeout=15).read())

changes = []

for node in data['nodes']:
    if node.get('name') == 'If Confidence > 62':
        params = node.get('parameters', {})
        conditions = params.get('conditions', {})

        print(f"  Current node params: {json.dumps(params, indent=2)[:500]}")

        # Find the condition that checks confidence > 0.62 and change it to >= 0.50
        # n8n If nodes use various condition formats depending on version
        found = False

        # Check v2 format (combinator)
        if 'options' in conditions:
            for cond_group in conditions.get('options', {}).get('conditions', []):
                for cond in cond_group.get('conditions', []):
                    val = cond.get('rightValue')
                    if val is not None and (val == 0.62 or val == '0.62' or str(val) == '0.62'):
                        print(f"  Found threshold: {val} → changing to 0.50")
                        cond['rightValue'] = 0.50
                        # Also change operator from greaterThan to greaterThanOrEqual
                        if cond.get('operator', {}).get('operation') == 'gt':
                            cond['operator']['operation'] = 'gte'
                        found = True

        # Check v1 format (conditions.number)
        if not found:
            for num_cond in conditions.get('number', []):
                val1 = num_cond.get('value1')
                val2 = num_cond.get('value2')
                op = num_cond.get('operation')
                if val2 is not None and (float(val2) == 0.62 or float(val2) == 62):
                    print(f"  Found v1 threshold: {val2} → changing to 0.50")
                    num_cond['value2'] = 0.50
                    if op == 'larger':
                        num_cond['operation'] = 'largerEqual'
                    found = True

        if not found:
            # Brute-force: search for 0.62 or 62 in the serialized params and replace
            params_str = json.dumps(params)
            if '0.62' in params_str:
                params_str = params_str.replace('0.62', '0.50')
                node['parameters'] = json.loads(params_str)
                found = True
                print("  Brute-force replaced 0.62 → 0.50 in params")
            elif '"62"' in params_str or ': 62' in params_str:
                params_str = params_str.replace('"62"', '"50"').replace(': 62', ': 50')
                node['parameters'] = json.loads(params_str)
                found = True
                print("  Brute-force replaced 62 → 50 in params")

        if found:
            changes.append("If Confidence > 62: threshold lowered to >= 0.50")
        else:
            print(f"  WARNING: Could not find threshold to change!")
            print(f"  Full params: {json.dumps(params, indent=2)}")
        break

if not changes:
    print("  WARNING: 'If Confidence > 62' node not found!")

# Save wf01B
if changes:
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
        print(f"  wf01B SAVED! Updated: {result.get('updatedAt', '?')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"  Error {e.code}: {body[:500]}")

# ── Patch 2: Reactivate wf08 (Position Monitor) ──
print("\n=== Reactivating wf08 (Position Monitor) ===")
wf08_id = 'Fjk7M3cOEcL1aAVf'

activate_req = urllib.request.Request(
    f'https://{n8n_host}/api/v1/workflows/{wf08_id}/activate',
    method='POST',
    headers={'X-N8N-API-KEY': n8n_key, 'Content-Type': 'application/json'}
)
try:
    resp = urllib.request.urlopen(activate_req, context=ctx, timeout=15)
    result = json.loads(resp.read())
    active = result.get('active', False)
    print(f"  wf08 active: {active}")
    if active:
        changes.append("wf08 Position Monitor: REACTIVATED")
    else:
        print("  WARNING: wf08 not activated!")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"  Error {e.code}: {body[:500]}")

print(f"\n=== Summary ===")
for c in changes:
    print(f"  [OK] {c}")
