#!/usr/bin/env python3
"""Patch wf01B: move Anti-Noise Filter BEFORE Macro Guard.

Root cause (signal #352): Anti-Noise Filter runs after Macro Guard.
When Macro blocks a signal, Anti-Noise never executes, so Rule 13
hard caps and confidence recalibration are never applied. The raw
LLM confidence gets saved to Supabase uncapped.

Fix: restructure the flow so Anti-Noise runs first:
  BEFORE: If Conf>=0.50 → MacroGuard → [blocked?] → GetBetSize → AntiNoise
  AFTER:  If Conf>=0.50 → GetBetSize → AntiNoise → [noiseSkip?] → MacroGuard

Changes:
  1. 3 connection swaps (move Anti-Noise before Macro Guard)
  2. New "Save Macro Block" Supabase node on macro-blocked path
  3. Update Macro Message Format to read Anti-Noise Filter output
"""
import ssl, urllib.request, json, os, certifi, uuid
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ctx = ssl.create_default_context(cafile=certifi.where())
n8n_host = os.environ['N8N_HOST']
n8n_key = os.environ['N8N_API_KEY']

# Fetch workflow
url = f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq'
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': n8n_key})
data = json.loads(urllib.request.urlopen(req, context=ctx, timeout=15).read())

changes = []
conns = data['connections']

# ── 1. Swap connections ──────────────────────────────────────────────────

# 1a. "If Confidence > 62" TRUE branch: Fetch Macro Guard → GET Bet Size
if_conf = conns.get('If Confidence > 62', {}).get('main', [])
if if_conf and if_conf[0][0]['node'] == 'Fetch Macro Guard':
    if_conf[0] = [{"node": "GET Bet Size", "type": "main", "index": 0}]
    changes.append("Route: If Conf>=0.50 [TRUE] → GET Bet Size (was → Fetch Macro Guard)")
elif if_conf and if_conf[0][0]['node'] == 'GET Bet Size':
    print("  Already routed to GET Bet Size — skipping")
else:
    print(f"  WARNING: unexpected TRUE target: {if_conf[0][0]['node'] if if_conf else '?'}")

# 1b. "If" (noise_skip) TRUE branch: Prepare Commit Data → Fetch Macro Guard
if_noise = conns.get('If', {}).get('main', [])
if if_noise and if_noise[0][0]['node'] == 'Prepare Commit Data':
    if_noise[0] = [{"node": "Fetch Macro Guard", "type": "main", "index": 0}]
    changes.append("Route: If noise_skip [TRUE/proceed] → Fetch Macro Guard (was → Prepare Commit Data)")
elif if_noise and if_noise[0][0]['node'] == 'Fetch Macro Guard':
    print("  Already routed to Fetch Macro Guard — skipping")
else:
    print(f"  WARNING: unexpected TRUE target: {if_noise[0][0]['node'] if if_noise else '?'}")

# 1c. "Is Macro Blocked?" FALSE branch: GET Bet Size → Prepare Commit Data
is_macro = conns.get('Is Macro Blocked?', {}).get('main', [])
if len(is_macro) >= 2 and is_macro[1][0]['node'] == 'GET Bet Size':
    is_macro[1] = [{"node": "Prepare Commit Data", "type": "main", "index": 0}]
    changes.append("Route: Is Macro Blocked? [FALSE] → Prepare Commit Data (was → GET Bet Size)")
elif len(is_macro) >= 2 and is_macro[1][0]['node'] == 'Prepare Commit Data':
    print("  Already routed to Prepare Commit Data — skipping")
else:
    print(f"  WARNING: unexpected FALSE target: {is_macro[1][0]['node'] if len(is_macro) >= 2 else '?'}")

# ── 2. Add "Save Macro Block" Supabase node ─────────────────────────────

# Check if it already exists
has_save_macro = any(n.get('name') == 'Save Macro Block' for n in data['nodes'])

if not has_save_macro:
    # Position it near Macro Message Format (offset Y down)
    save_macro_node = {
        "id": str(uuid.uuid4()),
        "name": "Save Macro Block",
        "type": "n8n-nodes-base.supabase",
        "typeVersion": 1,
        "position": [-2008, 360],  # Below Macro Message Format (-2008, 208)
        "credentials": {
            "supabaseApi": {
                "id": "xaGS2AzVGYaV8WR8",
                "name": "Supabase account"
            }
        },
        "parameters": {
            "operation": "update",
            "tableId": "btc_predictions",
            "filters": {
                "conditions": [
                    {
                        "keyName": "id",
                        "condition": "eq",
                        "keyValue": "={{ $('Create a row').first().json.id }}"
                    }
                ]
            },
            "fieldsUi": {
                "fieldValues": [
                    {"fieldId": "bet_taken", "fieldValue": "=false"},
                    {"fieldId": "no_bet_reason", "fieldValue": "=macro_blocked"},
                    {"fieldId": "classification", "fieldValue": "=MACRO_BLOCK"},
                    {"fieldId": "source_updated_by", "fieldValue": "=wf01B_macro"},
                    {
                        "fieldId": "confidence",
                        "fieldValue": "={{ $('Anti-Noise Filter').first().json.confidence ?? null }}"
                    },
                    {
                        "fieldId": "noise_reason",
                        "fieldValue": "={{ $('Anti-Noise Filter').first().json.noise_reason ?? null }}"
                    }
                ]
            }
        }
    }
    data['nodes'].append(save_macro_node)
    changes.append("Added node: Save Macro Block (Supabase update on macro-blocked path)")

    # 3. Connect: Is Macro Blocked? [TRUE] → also Save Macro Block
    if is_macro and is_macro[0]:
        is_macro[0].append({"node": "Save Macro Block", "type": "main", "index": 0})
        changes.append("Route: Is Macro Blocked? [TRUE] → +Save Macro Block (parallel with Macro Message Format)")
else:
    print("  Save Macro Block node already exists — skipping")

# ── 4. Update Macro Message Format to read from Anti-Noise Filter ────────

for node in data['nodes']:
    if node.get('name') == 'Macro Message Format':
        code = node['parameters'].get('jsCode', '')
        if '$json.direction' in code and "Anti-Noise Filter" not in code:
            new_code = """const anf = $('Anti-Noise Filter').first()?.json ?? {};
const macroData = $json;
const dir = anf.direction === 'UP' ? '🟢' : '🔴';
const side = anf.direction === 'UP' ? 'UP' : 'DOWN';
const conf = Math.round((anf.confidence ?? 0) * 100);
const time = $now.setZone('Europe/Rome').toFormat('HH:mm');
const event = macroData.event?.title ?? 'Unknown event';
const mins = macroData.event?.minutes_away ?? '?';

const nr = anf.noise_reason ? `\\n📊 ${anf.noise_reason}` : '';

const text = `🌍 MACRO BLOCK | ${time}
${dir} ${side} · ${conf}%${nr}
🚫 ${event} in ${mins}min`;

return [{ json: { text, chatId: '368092324' } }];"""
            node['parameters']['jsCode'] = new_code
            changes.append("Updated: Macro Message Format reads from Anti-Noise Filter (was $json)")
        elif "Anti-Noise Filter" in code:
            print("  Macro Message Format already updated — skipping")
        break

# ── Summary & Save ───────────────────────────────────────────────────────

if not changes:
    print("No changes needed!")
    exit(1)

print(f"\n{len(changes)} changes:")
for c in changes:
    print(f"  ✅ {c}")

# Verify flow integrity
print("\n=== New flow verification ===")
c = data['connections']
def get_targets(node_name, branch=None):
    node_conns = c.get(node_name, {}).get('main', [])
    if branch is not None and len(node_conns) > branch:
        return [t['node'] for t in node_conns[branch]]
    return [[t['node'] for t in b] for b in node_conns]

flow_checks = [
    ("If Confidence > 62 [TRUE]", get_targets('If Confidence > 62', 0), ["GET Bet Size"]),
    ("If Confidence > 62 [FALSE]", get_targets('If Confidence > 62', 1), ["Save Skip"]),
    ("GET Bet Size →", get_targets('GET Bet Size', 0), ["Anti-Noise Filter"]),
    ("If noise_skip [TRUE/proceed]", get_targets('If', 0), ["Fetch Macro Guard"]),
    ("If noise_skip [FALSE/skip]", get_targets('If', 1), ["Save Noise"]),
    ("Is Macro Blocked? [TRUE]", get_targets('Is Macro Blocked?', 0), ["Macro Message Format", "Save Macro Block"]),
    ("Is Macro Blocked? [FALSE]", get_targets('Is Macro Blocked?', 1), ["Prepare Commit Data"]),
]

all_ok = True
for label, actual, expected in flow_checks:
    ok = sorted(actual) == sorted(expected)
    status = "✅" if ok else "❌"
    print(f"  {status} {label} → {actual}")
    if not ok:
        print(f"      EXPECTED: {expected}")
        all_ok = False

if not all_ok:
    print("\n❌ Flow verification FAILED — aborting!")
    exit(1)

print("\n✅ Flow verified — saving...")

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

print(f"Payload: {len(payload)} bytes")

save_req = urllib.request.Request(
    f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq',
    data=payload, method='PUT',
    headers={'X-N8N-API-KEY': n8n_key, 'Content-Type': 'application/json'}
)
try:
    resp = urllib.request.urlopen(save_req, context=ctx, timeout=30)
    result = json.loads(resp.read())
    print(f"\n🚀 SAVED! Updated: {result.get('updatedAt', '?')}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"Error {e.code}: {body[:500]}")
