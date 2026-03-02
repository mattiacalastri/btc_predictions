#!/usr/bin/env python3
"""Update wf01B prompt to fix confidence stuck at 0.55."""
import ssl, urllib.request, json, os, certifi
from dotenv import load_dotenv
load_dotenv()

ctx = ssl.create_default_context(cafile=certifi.where())
n8n_host = os.environ.get('N8N_HOST', '')
n8n_key = os.environ.get('N8N_API_KEY', '')

# Fetch workflow
url = f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq'
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': n8n_key})
data = json.loads(urllib.request.urlopen(req, context=ctx, timeout=15).read())

nodes = data.get('nodes', [])
changes = []

# ── Fix 1: Update LLM prompt confidence rules ──
for node in nodes:
    if node.get('name') == 'BTC Prediction Bot':
        prompt = node['parameters']['text']

        # Replace confidence calibration section
        OLD_CAL = (
            "\u2501\u2501\u2501 CONFIDENCE CALIBRATION RULES \u2501\u2501\u2501\n"
            "RSI ESTREMO NON ABBASSA LA CONFIDENCE \u2014 regole obbligatorie:\n"
            "\n"
            "1. RSI < 25 (ipervenduto estremo) + EMA BEAR trend \u2192 il segnale DOWN \u00e8 VALIDO.\n"
            "   L'RSI basso indica pressione di vendita prolungata, non un reversal imminente nel breve.\n"
            "   NON abbassare confidence solo perch\u00e9 RSI \u00e8 basso con EMA BEAR.\n"
            "\n"
            "2. RSI > 75 (ipercomprato estremo) + EMA BULL trend \u2192 il segnale UP \u00e8 VALIDO.\n"
            "   Stesso principio: non abbassare confidence con EMA BULL solo per RSI alto.\n"
            "\n"
            "3. RSI neutro (44\u201356) = mercato laterale/indeciso \u2192 abbassare confidence a 0.50-0.55.\n"
            "\n"
            "4. Regola d'oro: confidence PROPORZIONALE alla forza del segnale quando EMA, MTF consensus\n"
            "   e taker flow concordano, INDIPENDENTEMENTE dal valore assoluto di RSI.\n"
            "\n"
            "5. Range confidence:\n"
            "   \u2022 0.50-0.55 = segnali contradditori, bassa convinzione\n"
            "   \u2022 0.56-0.62 = segnale chiaro con qualche dissonanza\n"
            "   \u2022 0.63-0.70 = segnale forte e concorde su 3+ dimensioni\n"
            "   \u2022 0.71-0.80 = convergenza eccezionale di tutti gli indicatori (raro)"
        )

        NEW_CAL = (
            "\u2501\u2501\u2501 CONFIDENCE CALIBRATION RULES \u2501\u2501\u2501\n"
            "CONFIDENCE = how strong the signal is. Use the FULL range 0.50-0.90.\n"
            "\n"
            "1. RSI extreme values CONFIRM the trend, they do NOT lower confidence:\n"
            "   \u2022 RSI < 25 + EMA BEAR \u2192 strong DOWN signal, confidence should be HIGH (0.65+)\n"
            "   \u2022 RSI > 75 + EMA BULL \u2192 strong UP signal, confidence should be HIGH (0.65+)\n"
            "   \u2022 RSI near 50 \u2192 neutral, use other indicators to decide\n"
            "\n"
            "2. Confidence must be PROPORTIONAL to signal strength:\n"
            "   \u2022 0.50-0.54 = genuinely conflicting signals, no clear direction\n"
            "   \u2022 0.55-0.62 = mild directional signal, some conflicting indicators\n"
            "   \u2022 0.63-0.72 = clear signal supported by 3+ indicators (EMA + MTF + taker flow)\n"
            "   \u2022 0.73-0.85 = strong convergence across technicals, derivatives, AND sentiment\n"
            "   \u2022 0.86-0.90 = exceptional convergence with volume spike confirmation (rare but valid)\n"
            "\n"
            "3. NEVER default to 0.55. If you find yourself outputting 0.55, STOP and ask:\n"
            "   \"Is this genuinely a 50/50 signal, or am I being artificially conservative?\"\n"
            "   Most market conditions have SOME directional bias \u2014 reflect it.\n"
            "\n"
            "4. force_no_bet = true means technicals are mixed, but you STILL must provide your best\n"
            "   direction estimate with honest confidence. Do NOT auto-cap at 0.55."
        )

        new_prompt = prompt.replace(OLD_CAL, NEW_CAL)

        # Also update output format range
        new_prompt = new_prompt.replace(
            '"confidence": <number 0.50\u20130.80>',
            '"confidence": <number 0.50\u20130.90>'
        )

        if new_prompt != prompt:
            node['parameters']['text'] = new_prompt
            changes.append(f"Prompt updated ({len(prompt)} -> {len(new_prompt)} chars)")
        else:
            changes.append("WARNING: Prompt not changed (old section not found exactly)")
        break

# ── Fix 2: Update Anti-Noise Filter blocked hours ──
for node in nodes:
    if node.get('name') == 'Anti-Noise Filter':
        old_code = node['parameters']['jsCode']
        old_hours = 'const BLOCKED_HOURS_UTC = [0, 1, 5, 7, 10, 11];'
        new_hours = 'const BLOCKED_HOURS_UTC = [5, 10];  // Only hours with WR < 15% (real data: 5h=0%, 10h=10%)'
        new_code = old_code.replace(old_hours, new_hours)
        if new_code != old_code:
            node['parameters']['jsCode'] = new_code
            changes.append("Anti-Noise Filter: blocked hours [0,1,5,7,10,11] -> [5,10]")
        else:
            changes.append("WARNING: Anti-Noise blocked hours not changed")
        break

if not changes:
    print("No changes to make!")
    exit(1)

for c in changes:
    print(f"  {c}")

# Save workflow — n8n API needs only nodes + connections for update
payload = {
    "nodes": data["nodes"],
    "connections": data["connections"],
}
save_data = json.dumps(payload).encode()
save_req = urllib.request.Request(
    f'https://{n8n_host}/api/v1/workflows/OMgFa9Min4qXRnhq',
    data=save_data,
    method='PUT',
    headers={
        'X-N8N-API-KEY': n8n_key,
        'Content-Type': 'application/json'
    }
)
resp = urllib.request.urlopen(save_req, context=ctx, timeout=15)
result = json.loads(resp.read())
print(f"\nWorkflow saved! Updated at: {result.get('updatedAt', '?')}")
