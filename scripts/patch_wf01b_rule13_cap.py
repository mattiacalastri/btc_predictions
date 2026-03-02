#!/usr/bin/env python3
"""Patch wf01B: enforce Rule 13 hard caps in Anti-Noise Filter.

Signal #352 showed the LLM ignores the 0.55 hard cap from Rule 13 Scenario B.
This patch injects algorithmic enforcement AFTER the recalibrator and BEFORE
the dynamic threshold check, clamping confidence when Rule 13 conditions apply.
"""
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

# ── Patch: Rule 13 Hard Cap Enforcement in Anti-Noise Filter ──
RULE13_CODE = r"""
// ── RULE 13 HARD CAP ENFORCEMENT (fix signal #352) ──────────────────────
// L'LLM non è affidabile nel rispettare i propri hard cap.
// Enforcement algoritmico basato sui dati raw, non sul reasoning.
{
  const _oiSignal = (triggerData.oi_price_signal ?? '').toUpperCase();
  const _lsBias = (triggerData.ls_bias ?? '').toLowerCase();
  const _techScore = Number(triggerData.technical_score?.value ?? 5);
  const _reasoning = (reasoning ?? '').toUpperCase();

  const _isCapitulation = _oiSignal.includes('CAPITULATION_RISK');
  const _isCrowdLong = _lsBias.includes('crowd_long_contrarian');
  const _isRule13B_reasoning = _reasoning.includes('RULE 13 SCENARIO B');

  // Rule 13 Scenario B: CAPITULATION_RISK + crowd_long_contrarian + score < 4.0
  if ((_isCapitulation && _isCrowdLong && _techScore < 4.0) || _isRule13B_reasoning) {
    const _r13cap = 0.55;
    if (confidence > _r13cap) {
      const _oldConf = confidence;
      confidence = _r13cap;
      noiseReason = (noiseReason ? noiseReason + ' | ' : '') +
        `rule13b_enforced(${_oldConf}→${confidence})`;
    }
  }

  // Rule 13 Scenario A: CAPITULATION_RISK + crowd_long_contrarian + score >= 4.0
  if (_isCapitulation && _isCrowdLong && _techScore >= 4.0) {
    const _r13aCap = 0.59;
    if (confidence > _r13aCap) {
      const _oldConf = confidence;
      confidence = _r13aCap;
      noiseReason = (noiseReason ? noiseReason + ' | ' : '') +
        `rule13a_enforced(${_oldConf}→${confidence})`;
    }
  }

  // Rule 13B (Short Squeeze): STRONG_TREND_UP + crowd_short_contrarian
  const _isStrongUp = _oiSignal.includes('STRONG_TREND_UP');
  const _isCrowdShort = _lsBias.includes('crowd_short_contrarian');

  if (_isStrongUp && _isCrowdShort && _techScore < 4.0) {
    const _r13bCap = 0.55;
    if (confidence > _r13bCap) {
      const _oldConf = confidence;
      confidence = _r13bCap;
      noiseReason = (noiseReason ? noiseReason + ' | ' : '') +
        `rule13b_squeeze_enforced(${_oldConf}→${confidence})`;
    }
  }
}
"""

for node in data['nodes']:
    if node.get('name') == 'Anti-Noise Filter':
        code = node['parameters']['jsCode']

        # Insert Rule 13 enforcement AFTER recalibrator, BEFORE threshold check
        marker = "// ── M-3 v2: Dynamic Confidence Threshold"
        if marker in code and 'RULE 13 HARD CAP ENFORCEMENT' not in code:
            code = code.replace(marker, RULE13_CODE + "\n" + marker)
            changes.append("Anti-Noise: added Rule 13 hard cap enforcement")
        elif 'RULE 13 HARD CAP ENFORCEMENT' in code:
            print("Rule 13 enforcement already present — skipping.")

        node['parameters']['jsCode'] = code
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
