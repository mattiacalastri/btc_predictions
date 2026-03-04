#!/usr/bin/env python3
"""Patch wf01B: fix notification system.

Problems:
  1. "Signal Message Format" fires ALWAYS (both noise and bet) and says
     "Trade aperto" BEFORE /place-bet is actually called. Misleading.
  2. User gets both "Signal" and "Noise" messages for the same signal.
  3. When /place-bet rejects (cooldown, ACE, regime), user thinks trade
     was opened but it wasn't.

Fix:
  1. Disconnect "Anti-Noise Filter" → "Signal Message Format" (stop sending
     the premature/misleading message)
  2. Add "Skip Message Format" code node connected to "Save Alert" → formats
     a message explaining WHY the trade was skipped by app.py
  3. Connect "Skip Message Format" → "Mattia → Signal" (reuse existing Telegram node)
  4. Update "BET Message Format" to include bet ID from Supabase row
"""
import ssl, urllib.request, json, os, certifi, uuid, copy
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ctx = ssl.create_default_context(cafile=certifi.where())
n8n_host = os.environ['N8N_HOST']
n8n_key = os.environ['N8N_API_KEY']

WF_ID = 'OMgFa9Min4qXRnhq'

# ── Fetch workflow ──────────────────────────────────────────────────────
url = f'https://{n8n_host}/api/v1/workflows/{WF_ID}'
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': n8n_key})
data = json.loads(urllib.request.urlopen(req, context=ctx, timeout=15).read())

changes = []
conns = data['connections']
nodes = data['nodes']

# ── 1. Disconnect "Anti-Noise Filter" → "Signal Message Format" ────────
anf_main = conns.get('Anti-Noise Filter', {}).get('main', [[]])
original_anf_targets = anf_main[0] if anf_main else []

# Keep all connections EXCEPT Signal Message Format
new_anf_targets = [c for c in original_anf_targets if c.get('node') != 'Signal Message Format']
if len(new_anf_targets) < len(original_anf_targets):
    anf_main[0] = new_anf_targets
    changes.append("Disconnected: Anti-Noise Filter → Signal Message Format")
else:
    print("  Signal Message Format already disconnected from Anti-Noise Filter")

# ── 2. Update "Signal Message Format" code to show skip reason ─────────
# This node will now be fed from "Save Alert" instead
SKIP_MESSAGE_CODE = r"""
const pos = $('Open Position').first()?.json ?? {};
const bot = $('BTC Prediction Bot').first()?.json?.output ?? {};
const anf = $('Anti-Noise Filter').first()?.json ?? {};

const isUp = (bot.direction ?? 'UP') === 'UP';
const dir = isUp ? '🟢' : '🔴';
const side = isUp ? 'UP' : 'DOWN';
const conf = Math.round((anf.confidence ?? bot.confidence ?? 0) * 100);
const time = $now.setZone('Europe/Rome').toFormat('HH:mm');

// Determine skip reason from /place-bet response
const status = pos.status ?? 'unknown';
const reason = pos.reason ?? pos.message ?? status;
let reasonEmoji = '⏩';
let reasonText = reason;

if (reason === 'cooldown') {
  reasonEmoji = '⏳';
  const remaining = pos.remaining_minutes ? `${pos.remaining_minutes}min` : '';
  reasonText = `Cooldown ${remaining}`;
} else if (reason === 'dead_hours') {
  reasonEmoji = '🌙';
  reasonText = 'Dead hours';
} else if (reason === 'paused') {
  reasonEmoji = '⏸️';
  reasonText = 'Bot in pausa';
} else if (reason === 'ace_below_threshold' || reason === 'ace_skip') {
  reasonEmoji = '🎯';
  reasonText = `ACE skip (${conf}% < threshold)`;
} else if (reason === 'regime_skip') {
  reasonEmoji = '📊';
  reasonText = 'Regime sfavorevole';
} else if (status === 'skipped') {
  reasonEmoji = '⏩';
  reasonText = reason;
}

const text = `${reasonEmoji} SKIP | ${time}\n${dir} ${side} · ${conf}%\n${reasonText}`;
return [{ json: { text, chatId: '368092324' } }];
""".strip()

for n in nodes:
    if n['name'] == 'Signal Message Format':
        n['parameters']['jsCode'] = SKIP_MESSAGE_CODE
        changes.append("Updated: Signal Message Format → now shows skip reason from /place-bet")
        break

# ── 3. Connect "Save Alert" → "Signal Message Format" ──────────────────
# Save Alert is the node that fires when /place-bet returns status !== "placed"
if 'Save Alert' not in conns:
    conns['Save Alert'] = {'main': [[]]}

sa_main = conns['Save Alert']['main']
# Check if already connected
already_connected = any(
    c.get('node') == 'Signal Message Format'
    for conn_list in sa_main
    for c in conn_list
)
if not already_connected:
    sa_main[0].append({"node": "Signal Message Format", "type": "main", "index": 0})
    changes.append("Connected: Save Alert → Signal Message Format")
else:
    print("  Save Alert already connected to Signal Message Format")

# ── 4. Update "BET Message Format" to include bet ID ───────────────────
BET_MESSAGE_CODE = r"""
const bot = $('BTC Prediction Bot').item.json.output;
const pos = $('Open Position').item.json;
const rowId = $('Create a row').first()?.json?.id ?? '';

if (pos.status === 'skipped') {
  return [{ json: { text: '⚠️ Posizione saltata — stack vuota', chatId: '368092324' } }];
}

const isUp = bot.direction === 'UP';
const dir = isUp ? '🟢' : '🔴';
const side = isUp ? 'LONG' : 'SHORT';
const conf = Math.round(bot.confidence * 100);
const time = $now.setZone('Europe/Rome').toFormat('HH:mm');

const entry = pos.fill_price ?? pos.position?.price ?? '—';
const sl = pos.sl_price ?? '—';
const tp = pos.tp_price ?? '—';
const size = pos.size ?? pos.position?.size ?? '—';
const rr = pos.rr_ratio ? ` · R:R ${pos.rr_ratio}` : '';
const drift = pos.price_drift_pct ? `\n📉 Drift: ${(pos.price_drift_pct * 100).toFixed(3)}%` : '';
const betId = rowId ? `\n🆔 #${rowId}` : '';

const text = `${dir} ${side} APERTO | ${time}
💵 $${Number(entry).toLocaleString('en', {maximumFractionDigits: 0})} · ${conf}%
🛑 SL $${Number(sl).toLocaleString('en', {maximumFractionDigits: 0})} · 🎯 TP $${Number(tp).toLocaleString('en', {maximumFractionDigits: 0})}${rr}
📐 ${size} BTC${drift}${betId}

🔗 btcpredictor.io/dashboard`;

return [{ json: { text, chatId: '368092324' } }];
""".strip()

for n in nodes:
    if n['name'] == 'BET Message Format':
        n['parameters']['jsCode'] = BET_MESSAGE_CODE
        changes.append("Updated: BET Message Format → includes bet ID, formatted prices, drift")
        break

# ── 5. Update "Noise Message Format" to show recalibrated confidence ────
NOISE_MESSAGE_CODE = r"""
const dir = $json.direction === 'UP' ? '🟢' : '🔴';
const side = $json.direction === 'UP' ? 'UP' : 'DOWN';
const rawConf = $('BTC Prediction Bot').first()?.json?.output?.confidence ?? 0;
const algoConf = $json.confidence ?? rawConf;
const conf = Math.round(algoConf * 100);
const time = $now.setZone('Europe/Rome').toFormat('HH:mm');

// Show clean reason
let reason = $json.noiseReason || 'filtro attivo';
// Simplify long reasons
if (reason.includes('dynamic_drift_skip')) {
  const m = reason.match(/([\d.]+)%/);
  reason = `drift ${m ? m[1] + '%' : ''} troppo alto`;
} else if (reason.includes('blocked_hour')) {
  const h = reason.match(/utc_(\d+)/);
  reason = `ora bloccata (${h ? h[1] + ':00' : ''} UTC)`;
} else if (reason.includes('rsi_neutral')) {
  reason = 'RSI zona neutra';
} else if (reason.includes('xgb_disagree')) {
  reason = 'XGB disaccordo';
}

const text = `🔇 NOISE | ${time}\n${dir} ${side} · ${conf}%\n❌ ${reason}`;
return [{ json: { text, chatId: '368092324' } }];
""".strip()

for n in nodes:
    if n['name'] == 'Noise Message Format':
        n['parameters']['jsCode'] = NOISE_MESSAGE_CODE
        changes.append("Updated: Noise Message Format → cleaner reasons, algo confidence")
        break

# ── Save workflow ───────────────────────────────────────────────────────
if not changes:
    print("No changes needed.")
    exit(0)

print(f"\n{'='*60}")
print(f"Changes to apply ({len(changes)}):")
for c in changes:
    print(f"  ✓ {c}")
print(f"{'='*60}\n")

# Deactivate first (n8n rejects PUT on active workflows)
print("Deactivating workflow...")
deact_req = urllib.request.Request(
    f"{url}/deactivate",
    data=b'',
    headers={'X-N8N-API-KEY': n8n_key, 'Content-Type': 'application/json'},
    method='POST',
)
try:
    urllib.request.urlopen(deact_req, context=ctx, timeout=15)
    print("  Deactivated.")
except Exception as e:
    print(f"  Deactivate warning: {e}")

# n8n PUT only accepts: name, nodes, connections, settings (minimal)
payload = {
    'name': data['name'],
    'nodes': data['nodes'],
    'connections': data['connections'],
    'settings': {
        'executionOrder': 'v1',
        'callerPolicy': 'workflowsFromSameOwner',
    },
}

# PUT the updated workflow
put_data = json.dumps(payload).encode()
put_req = urllib.request.Request(
    url,
    data=put_data,
    headers={
        'X-N8N-API-KEY': n8n_key,
        'Content-Type': 'application/json',
    },
    method='PUT',
)
resp = urllib.request.urlopen(put_req, context=ctx, timeout=30)
result = json.loads(resp.read())
print(f"Workflow updated: {result.get('name', '?')} (id={result.get('id', '?')})")

# Reactivate
print("Reactivating workflow...")
react_req = urllib.request.Request(
    f"{url}/activate",
    data=b'',
    headers={'X-N8N-API-KEY': n8n_key, 'Content-Type': 'application/json'},
    method='POST',
)
try:
    resp2 = urllib.request.urlopen(react_req, context=ctx, timeout=15)
    result2 = json.loads(resp2.read())
    print(f"  Active: {result2.get('active', '?')}")
except Exception as e:
    print(f"  Reactivate warning: {e}")
    print("  ⚠️ Toggle ON manually in n8n UI!")

print("\nDone.")
