#!/usr/bin/env python3
"""Patch wf01B: add confidence recalibrator + bump LLM temperature."""
import ssl, urllib.request, json, os, certifi
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

# ── Patch 1: Add Confidence Recalibrator to Anti-Noise Filter ──
RECALIBRATOR_CODE = r"""
// ── CONFIDENCE RECALIBRATOR (P0 — fix LLM stuck at 0.55) ─────────────────
// Uses actual indicator data to compute algorithmic confidence.
// Only activates when LLM confidence is in the 0.54-0.56 dead zone.
if (confidence >= 0.54 && confidence <= 0.56 && direction && direction !== 'NO_BET') {
  const _techVal = Math.abs(Number(triggerData.technical_score?.value ?? 0));
  const _emaTrend = (triggerData.indicators?.ema_trend ?? 'NEUTRAL').toUpperCase();
  const _mtfCons = (triggerData.mtf?.consensus ?? 'MIXED').toUpperCase();
  const _takerSig = (triggerData.taker_signal ?? 'NEUTRAL').toUpperCase();
  const _volState = (triggerData.indicators?.volume_state ?? 'NORMAL').toUpperCase();
  const _rsi = Number(triggerData.indicators?.rsi14 ?? 50);
  const _dir = direction.toUpperCase();

  let _agree = 0;
  const _total = 7.0;  // max score

  // EMA trend alignment (weight 2)
  if ((_dir === 'UP' && _emaTrend === 'BULL') ||
      (_dir === 'DOWN' && _emaTrend === 'BEAR')) _agree += 2;
  else if (_emaTrend === 'NEUTRAL') _agree += 0.5;

  // MTF consensus (weight 1.5)
  if ((_dir === 'UP' && _mtfCons === 'BULL') ||
      (_dir === 'DOWN' && _mtfCons === 'BEAR')) _agree += 1.5;
  else if (_mtfCons === 'MIXED') _agree += 0.3;

  // Technical score strength (weight 2, proportional)
  const _techAligned =
    (_dir === 'UP' && Number(triggerData.technical_score?.value ?? 0) > 0) ||
    (_dir === 'DOWN' && Number(triggerData.technical_score?.value ?? 0) < 0);
  if (_techAligned) _agree += Math.min(2, _techVal / 3.5);

  // Taker flow (weight 1)
  if ((_dir === 'UP' && _takerSig === 'BUY') ||
      (_dir === 'DOWN' && _takerSig === 'SELL')) _agree += 1;

  // Volume confirmation (weight 0.5)
  if (_volState === 'SPIKE' || _volState === 'HIGH') _agree += 0.5;

  // RSI extreme confirmation (bonus)
  if ((_dir === 'DOWN' && _rsi < 30) || (_dir === 'UP' && _rsi > 70)) _agree += 0.5;

  // Map agreement ratio to confidence: 0→0.50, 0.5→0.62, 1.0→0.80
  const _ratio = _agree / _total;
  const _recal = parseFloat((0.50 + _ratio * 0.30).toFixed(2));
  const _oldConf = confidence;
  confidence = _recal;

  noiseReason = (noiseReason ? noiseReason + ' | ' : '') +
    `conf_recalibrated(llm=${_oldConf}→algo=${confidence},agree=${_agree.toFixed(1)}/${_total})`;
}
"""

for node in data['nodes']:
    if node.get('name') == 'Anti-Noise Filter':
        code = node['parameters']['jsCode']

        # Change const confidence to let confidence
        if 'const confidence =' in code:
            code = code.replace('const confidence =', 'let confidence =', 1)
            changes.append("Anti-Noise: const confidence → let confidence")

        # Insert recalibrator BEFORE the threshold logic
        marker = "// ── M-3 v2: Dynamic Confidence Threshold"
        if marker in code and 'CONFIDENCE RECALIBRATOR' not in code:
            code = code.replace(marker, RECALIBRATOR_CODE + "\n" + marker)
            changes.append("Anti-Noise: added confidence recalibrator")

        node['parameters']['jsCode'] = code
        break

# ── Patch 2: Bump LLM temperature 0.15 → 0.30 ──
for node in data['nodes']:
    if node.get('name') == 'OpenRouter Chat Model':
        opts = node['parameters'].get('options', {})
        old_temp = opts.get('temperature', '?')
        opts['temperature'] = 0.30
        node['parameters']['options'] = opts
        changes.append(f"LLM temperature: {old_temp} → 0.30")
        break

if not changes:
    print("No changes needed!")
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

print(f"Payload: {len(payload)} bytes")

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
