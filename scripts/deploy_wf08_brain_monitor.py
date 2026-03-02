#!/usr/bin/env python3
"""Deploy wf08 Brain Monitor v1 to n8n.

Usage:
    source .env && python3 scripts/deploy_wf08_brain_monitor.py
"""
import json, os, sys, urllib.request, ssl, certifi

N8N_HOST = os.environ.get("N8N_HOST", "")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
WF_ID = "Fjk7M3cOEcL1aAVf"  # 08_BTC_Position_Monitor

if not N8N_HOST or not N8N_API_KEY:
    print("ERROR: N8N_HOST and N8N_API_KEY must be set")
    sys.exit(1)

BOT_API_KEY = "93a7a5c5fe15fb5d6e58dc407ff3d98911f225ceff85f83ae948bf4494225422"
BASE_URL = "https://web-production-e27d0.up.railway.app"
TELEGRAM_CRED_ID = "NRPGXzK8s6jsWl9f"
TELEGRAM_CRED_NAME = "Telegram account (BTC Predictor)"
SUPABASE_CRED_ID = "xaGS2AzVGYaV8WR8"
CHAT_ID_MATTIA = "368092324"

# ── Brain Narrator Code ─────────────────────────────────────────────────────
NARRATOR_CODE = r"""
const data = $input.first().json;
const state = $getWorkflowStaticData('global');
const now = new Date();

// Initialize persistent state
if (!state.lastRun) {
  state.lastRun = now.toISOString();
  state.lastEquity = data.equity;
  state.lastPosition = null;
  state.messagesSent = 0;
  state.lastMessageTime = 0;
  state.lastRoutineTime = 0;
}

const msSinceMsg = now.getTime() - (state.lastMessageTime || 0);
const msSinceRoutine = now.getTime() - (state.lastRoutineTime || 0);

// ── DETECT EVENTS ──────────────────────────────────────────────────────────
const events = [];

// Position change
const hadPos = !!state.lastPosition;
const hasPos = !!data.position;
if (!hadPos && hasPos) {
  const p = data.position;
  const side = (p.side || '?').toUpperCase();
  events.push({
    type: 'CRITICAL', emoji: '🎯',
    text: `NUOVA POSIZIONE: ${side} ${p.size} BTC @ $${Math.round(p.price).toLocaleString()}`
  });
} else if (hadPos && !hasPos) {
  events.push({ type: 'CRITICAL', emoji: '🏁', text: 'POSIZIONE CHIUSA' });
} else if (hasPos && hadPos) {
  const curSide = data.position.side;
  const prevSide = state.lastPosition.side;
  if (curSide !== prevSide) {
    events.push({
      type: 'CRITICAL', emoji: '🔄',
      text: `FLIP: ${prevSide.toUpperCase()} → ${curSide.toUpperCase()}`
    });
  }
}

// Equity change
const eqDelta = (data.equity || 0) - (state.lastEquity || data.equity || 0);
const eqPct = state.lastEquity ? ((eqDelta / state.lastEquity) * 100) : 0;
if (Math.abs(eqDelta) > 2) {
  events.push({
    type: 'ALERT',
    emoji: eqDelta > 0 ? '📈' : '📉',
    text: `Equity ${eqDelta > 0 ? '+' : ''}$${eqDelta.toFixed(2)} (${eqPct > 0 ? '+' : ''}${eqPct.toFixed(1)}%)`
  });
}

// Ghost evaluation
const gh = data.ghost || {};
if (gh.evaluated > 0) {
  events.push({
    type: 'INFO', emoji: '👻',
    text: `Ghost: ${gh.correct}/${gh.evaluated} corretti${gh.wr !== null ? ` (${gh.wr}% WR)` : ''} | ${gh.pending} in coda`
  });
}

// Direction bias extreme
const bias = data.direction_bias || {};
if (bias.total > 5) {
  const upPct = Math.round((bias.up / bias.total) * 100);
  if (upPct > 80) events.push({ type: 'INFO', emoji: '🐂', text: `Strong UP bias: ${upPct}% delle ultime ${bias.total} signals` });
  if (upPct < 20) events.push({ type: 'INFO', emoji: '🐻', text: `Strong DOWN bias: ${100 - upPct}% delle ultime ${bias.total} signals` });
}

// Macro alert
if (data.macro_events && data.macro_events.length > 0) {
  const m = data.macro_events[0];
  events.push({
    type: 'ALERT', emoji: '⚠️',
    text: `Macro: ${m.title} in ${m.minutes_away}min (${m.impact})`
  });
}

// System alerts
if (data.paused) events.push({ type: 'CRITICAL', emoji: '⏸️', text: 'BOT IN PAUSA!' });
if (data.btc_price === null) events.push({ type: 'ALERT', emoji: '🔴', text: 'Kraken API non risponde!' });

// ── DECIDE: SEND? ──────────────────────────────────────────────────────────
let shouldSend = false;
let category = 'WHISPER';

if (events.some(e => e.type === 'CRITICAL')) {
  shouldSend = true; category = 'CRITICAL';
} else if (events.some(e => e.type === 'ALERT')) {
  shouldSend = true; category = 'ALERT';
} else if (msSinceRoutine > 15 * 60 * 1000) {
  // Routine update ogni 15 min
  shouldSend = true; category = 'ROUTINE';
}

// ── BUILD MESSAGE ──────────────────────────────────────────────────────────
const lines = [];
const timeStr = now.toISOString().slice(11, 16);

// Header
if (category === 'CRITICAL') lines.push(`🚨 <b>BRAIN ALERT — ${timeStr} UTC</b>`);
else if (category === 'ALERT') lines.push(`⚡ <b>Bot Brain — ${timeStr} UTC</b>`);
else lines.push(`🧠 <b>Bot Brain — ${timeStr} UTC</b>`);
lines.push('');

// Price & Equity bar
const priceStr = data.btc_price ? `$${Math.round(data.btc_price).toLocaleString()}` : '???';
const eqStr = data.equity ? `$${data.equity.toFixed(2)}` : '???';
const eqSign = eqDelta > 0 ? '▲' : eqDelta < 0 ? '▼' : '■';
const eqDeltaStr = Math.abs(eqDelta) > 0.01 ? ` ${eqSign}${Math.abs(eqDelta).toFixed(2)}` : '';
lines.push(`₿ <b>${priceStr}</b> | 💰 ${eqStr}${eqDeltaStr}`);
lines.push('');

// Events
if (events.length > 0) {
  for (const e of events) {
    lines.push(`${e.emoji} ${e.text}`);
  }
  lines.push('');
}

// Position
if (data.position) {
  const p = data.position;
  const sideIcon = p.side === 'long' ? '🟢' : '🔴';
  lines.push(`${sideIcon} <b>${p.side.toUpperCase()}</b> ${p.size} BTC @ $${Math.round(p.price).toLocaleString()}`);
} else {
  lines.push('💤 Flat — nessuna posizione');
}
lines.push('');

// Signal intelligence
if (bias.total > 0) {
  const upBlocks = Math.round((bias.up / bias.total) * 10);
  const bar = '█'.repeat(upBlocks) + '░'.repeat(10 - upBlocks);
  lines.push(`📊 UP ${bar} DOWN (${bias.up}/${bias.down})`);
}
if (data.avg_confidence) {
  const streak = data.streak || {};
  lines.push(`🎯 Conf: ${data.avg_confidence} | Streak: ${streak.count || 0}× ${streak.direction || '?'}`);
}

// Performance
const perf = data.performance;
if (perf) {
  const pnlSign = perf.pnl_5 >= 0 ? '+' : '';
  lines.push(`📈 WR(10): ${perf.wr_10}% | PnL(5): ${pnlSign}$${perf.pnl_5}`);
}

// XGB & Model
const xgb = data.xgb_gate || {};
lines.push(`🤖 XGB: ${xgb.active ? '✅ ATTIVO' : `⏳ ${xgb.clean_bets}/${xgb.min_bets} bets`}`);

const message = lines.join('\n');

// ── UPDATE STATE ───────────────────────────────────────────────────────────
state.lastRun = now.toISOString();
state.lastEquity = data.equity;
state.lastPosition = data.position;
if (shouldSend) {
  state.messagesSent = (state.messagesSent || 0) + 1;
  state.lastMessageTime = now.getTime();
  if (category === 'ROUTINE') state.lastRoutineTime = now.getTime();
}

return [{
  json: {
    send: shouldSend,
    message,
    category,
    events_count: events.length,
    ts: now.toISOString(),
    run_count: state.messagesSent || 0
  }
}];
"""

# ── Workflow Definition ──────────────────────────────────────────────────────
workflow = {
    "name": "08_BTC_Brain_Monitor",
    "nodes": [
        {
            "parameters": {
                "rule": {
                    "interval": [{"field": "minutes", "minutesInterval": 3}]
                }
            },
            "id": "brain-trigger",
            "name": "Every 3 Minutes",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [0, 300],
        },
        {
            "parameters": {
                "method": "POST",
                "url": f"{BASE_URL}/ghost-evaluate",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {"name": "X-API-Key", "value": BOT_API_KEY}
                    ]
                },
                "options": {},
            },
            "id": "brain-ghost",
            "name": "Ghost Evaluate",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [260, 300],
            "continueOnFail": True,
        },
        {
            "parameters": {
                "method": "GET",
                "url": f"{BASE_URL}/brain-state",
                "options": {},
            },
            "id": "brain-fetch",
            "name": "Fetch Brain State",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [520, 300],
        },
        {
            "parameters": {
                "jsCode": NARRATOR_CODE.strip(),
            },
            "id": "brain-narrator",
            "name": "Brain Narrator",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [780, 300],
        },
        {
            "parameters": {
                "conditions": {
                    "options": {"caseSensitive": True, "leftValue": ""},
                    "conditions": [
                        {
                            "id": "brain-if-send",
                            "leftValue": "={{ $json.send }}",
                            "rightValue": True,
                            "operator": {
                                "type": "boolean",
                                "operation": "equals",
                                "singleValue": True,
                            },
                        }
                    ],
                    "combinator": "and",
                },
            },
            "id": "brain-if",
            "name": "Should Send?",
            "type": "n8n-nodes-base.if",
            "typeVersion": 2,
            "position": [1040, 300],
        },
        {
            "parameters": {
                "chatId": CHAT_ID_MATTIA,
                "text": "={{ $json.message }}",
                "additionalFields": {
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            },
            "id": "brain-telegram",
            "name": "Send to Mattia",
            "type": "n8n-nodes-base.telegram",
            "typeVersion": 1.2,
            "position": [1300, 200],
            "credentials": {
                "telegramApi": {
                    "id": TELEGRAM_CRED_ID,
                    "name": TELEGRAM_CRED_NAME,
                }
            },
        },
        {
            "parameters": {},
            "id": "brain-noop",
            "name": "Silent",
            "type": "n8n-nodes-base.noOp",
            "typeVersion": 1,
            "position": [1300, 400],
        },
    ],
    "connections": {
        "Every 3 Minutes": {
            "main": [[{"node": "Ghost Evaluate", "type": "main", "index": 0}]]
        },
        "Ghost Evaluate": {
            "main": [[{"node": "Fetch Brain State", "type": "main", "index": 0}]]
        },
        "Fetch Brain State": {
            "main": [[{"node": "Brain Narrator", "type": "main", "index": 0}]]
        },
        "Brain Narrator": {
            "main": [[{"node": "Should Send?", "type": "main", "index": 0}]]
        },
        "Should Send?": {
            "main": [
                [{"node": "Send to Mattia", "type": "main", "index": 0}],
                [{"node": "Silent", "type": "main", "index": 0}],
            ]
        },
    },
    "settings": {
        "executionOrder": "v1",
    },
    "pinData": {},
}


def deploy():
    url = f"https://{N8N_HOST}/api/v1/workflows/{WF_ID}"
    payload = json.dumps(workflow).encode("utf-8")

    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(
        url,
        data=payload,
        method="PUT",
        headers={
            "X-N8N-API-KEY": N8N_API_KEY,
            "Content-Type": "application/json",
        },
    )

    print(f"Deploying wf08 Brain Monitor to {url}...")
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            data = json.loads(resp.read())
            print(f"✅ Deployed: {data.get('name')} (active={data.get('active')})")
            print(f"   Nodes: {len(data.get('nodes', []))}")
            print(f"   Updated: {data.get('updatedAt')}")
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"❌ HTTP {e.code}: {body[:500]}")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


if __name__ == "__main__":
    ok = deploy()
    if ok:
        print("\n⚠️  IMPORTANTE: vai su n8n UI e fai Toggle OFF → Toggle ON")
        print("   per reinizializzare lo Schedule Trigger!")
    sys.exit(0 if ok else 1)
