#!/usr/bin/env python3
"""Deploy wf08 Brain Monitor v1 to n8n.

Usage:
    source .env && N8N_HOST=$N8N_HOST N8N_API_KEY=$N8N_API_KEY python3 scripts/deploy_wf08_brain_monitor.py
"""
import json, os, sys, urllib.request, ssl, certifi

N8N_HOST = os.environ.get("N8N_HOST", "")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")
WF_ID = "Fjk7M3cOEcL1aAVf"

if not N8N_HOST or not N8N_API_KEY:
    print("ERROR: N8N_HOST and N8N_API_KEY must be set")
    sys.exit(1)

BOT_API_KEY = os.environ.get("BOT_API_KEY", "")
BASE_URL = os.environ.get("RAILWAY_URL", "https://web-production-e27d0.up.railway.app")

if not BOT_API_KEY:
    print("ERROR: BOT_API_KEY must be set")
    sys.exit(1)

# ── Brain Narrator JS Code ───────────────────────────────────────────────────
NARRATOR_CODE = r"""
const data = $input.first().json;
const state = $getWorkflowStaticData('global');
const now = new Date();

if (!state.lastRun) {
  state.lastRun = now.toISOString();
  state.lastEquity = data.equity;
  state.lastPosition = null;
  state.messagesSent = 0;
  state.lastMessageTime = 0;
  state.lastRoutineTime = 0;
}

const msSinceRoutine = now.getTime() - (state.lastRoutineTime || 0);
const events = [];

// Position change
const hadPos = !!state.lastPosition;
const hasPos = !!data.position;
if (!hadPos && hasPos) {
  const p = data.position;
  events.push({ type: 'CRITICAL', emoji: '🎯', text: `NUOVA POSIZIONE: ${(p.side||'?').toUpperCase()} ${p.size} BTC @ $${Math.round(p.price).toLocaleString()}` });
} else if (hadPos && !hasPos) {
  events.push({ type: 'CRITICAL', emoji: '🏁', text: 'POSIZIONE CHIUSA' });
} else if (hasPos && hadPos && data.position.side !== state.lastPosition.side) {
  events.push({ type: 'CRITICAL', emoji: '🔄', text: `FLIP: ${state.lastPosition.side.toUpperCase()} → ${data.position.side.toUpperCase()}` });
}

// Equity delta
const eqDelta = (data.equity || 0) - (state.lastEquity || data.equity || 0);
if (Math.abs(eqDelta) > 2) {
  events.push({ type: 'ALERT', emoji: eqDelta > 0 ? '📈' : '📉', text: `Equity ${eqDelta > 0?'+':''}$${eqDelta.toFixed(2)}` });
}

// Ghost (trigger only — detailed block built below)
const gh = data.ghost || {};
if (gh.evaluated > 0) {
  events.push({ type: 'INFO', emoji: '👻', text: `Ghost: ${gh.evaluated} giudicati` });
}

// Bias
const bias = data.direction_bias || {};
if (bias.total > 5) {
  const upPct = Math.round((bias.up / bias.total) * 100);
  if (upPct > 80) events.push({ type: 'INFO', emoji: '🐂', text: `Strong UP bias: ${upPct}%` });
  if (upPct < 20) events.push({ type: 'INFO', emoji: '🐻', text: `Strong DOWN bias: ${100-upPct}%` });
}

// Macro
if (data.macro_events && data.macro_events.length > 0) {
  const m = data.macro_events[0];
  events.push({ type: 'ALERT', emoji: '⚠️', text: `Macro: ${m.title} in ${m.minutes_away}min` });
}

// System
if (data.paused) events.push({ type: 'CRITICAL', emoji: '⏸️', text: 'BOT IN PAUSA!' });
if (!data.btc_price) events.push({ type: 'ALERT', emoji: '🔴', text: 'Kraken offline!' });

// Decide
let shouldSend = false, category = 'WHISPER';
if (events.some(e => e.type === 'CRITICAL')) { shouldSend = true; category = 'CRITICAL'; }
else if (events.some(e => e.type === 'ALERT')) { shouldSend = true; category = 'ALERT'; }
else if (msSinceRoutine > 15 * 60 * 1000) { shouldSend = true; category = 'ROUTINE'; }

// Build message
const t = now.toISOString().slice(11, 16);
const lines = [];
if (category === 'CRITICAL') lines.push(`🚨 <b>BRAIN ALERT — ${t} UTC</b>`);
else if (category === 'ALERT') lines.push(`⚡ <b>Bot Brain — ${t} UTC</b>`);
else lines.push(`🧠 <b>Bot Brain — ${t} UTC</b>`);
lines.push('');

const px = data.btc_price ? `$${Math.round(data.btc_price).toLocaleString()}` : '???';
const eq = data.equity ? `$${data.equity.toFixed(2)}` : '???';
const delta = Math.abs(eqDelta) > 0.01 ? ` ${eqDelta>0?'▲':'▼'}${Math.abs(eqDelta).toFixed(2)}` : '';
lines.push(`₿ <b>${px}</b> | 💰 ${eq}${delta}`);
lines.push('');

for (const e of events) lines.push(`${e.emoji} ${e.text}`);
if (events.length) lines.push('');

if (data.position) {
  const p = data.position;
  lines.push(`${p.side==='long'?'🟢':'🔴'} <b>${p.side.toUpperCase()}</b> ${p.size} @ $${Math.round(p.price).toLocaleString()}`);
} else {
  lines.push('💤 Flat — nessuna posizione');
}
lines.push('');

if (bias.total > 0) {
  const b = Math.min(10, Math.max(0, Math.round((bias.up/bias.total)*10)));
  lines.push(`📊 UP ${'█'.repeat(b)}${'░'.repeat(10-b)} DOWN (${bias.up}/${bias.down})`);
}
if (data.avg_confidence) {
  const s = data.streak || {};
  lines.push(`🎯 Conf: ${data.avg_confidence} | Streak: ${s.count||0}× ${s.direction||'?'}`);
}
const perf = data.performance;
if (perf) lines.push(`📈 WR(10): ${perf.wr_10}% | PnL(5): ${perf.pnl_5>=0?'+':''}$${perf.pnl_5}`);
const xgb = data.xgb_gate || {};
lines.push(`🤖 XGB: ${xgb.active ? '✅ ON' : `⏳ ${xgb.clean_bets}/${xgb.min_bets}`}`);

// ── Ghost Evaluator Block ──────────────────────────────────────────────────
const ghostSigs = (data.signals_6h || []).filter(s => s.ghost_evaluated_at && s.bet_taken === false);
if (ghostSigs.length > 0) {
  lines.push('');
  lines.push('┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈');
  lines.push('👻 <b>GHOST EVALUATOR</b>');
  lines.push('<i>Segnali filtrati — verdetto a T+30min</i>');
  lines.push('');

  const maxShow = 5;
  for (const s of ghostSigs.slice(0, maxShow)) {
    const hit = s.ghost_correct;
    const dir = (s.direction || '?').toUpperCase();
    const conf = s.confidence ? parseFloat(s.confidence).toFixed(2) : '—';
    const verdict = hit ? 'Azzeccato' : 'Sbagliato';
    lines.push(`${hit ? '✅' : '❌'} ${dir.padEnd(4)} ${conf} → <i>${verdict}</i>`);
  }
  if (ghostSigs.length > maxShow) {
    lines.push(`   <i>+${ghostSigs.length - maxShow} altri…</i>`);
  }

  lines.push('');
  const wr = gh.wr !== null ? gh.wr : 0;
  const filled = Math.round(wr / 10);
  const bar = '\u2593'.repeat(filled) + '\u2591'.repeat(10 - filled);
  lines.push(`\u2693 <b>${gh.correct}/${gh.evaluated}</b> ${bar} <b>${wr}%</b>`);
  if (gh.pending > 0) lines.push(`\u23f3 ${gh.pending} ancora in mare aperto`);

  lines.push('');
  const q = [];
  if (wr >= 75) {
    q.push('Il mare parla a chi sa ascoltare. Oggi parla chiaro.');
    q.push('Vento in poppa. Chi ha il coraggio di salpare, raccoglie.');
    q.push('Le correnti ci obbediscono. Non per fortuna \u2014 per studio.');
  } else if (wr >= 60) {
    q.push('Non serve prevedere ogni onda \u2014 basta cavalcare quelle giuste.');
    q.push('La bussola punta bene. Il tesoro si avvicina.');
    q.push('Acque fertili. Il capitano che studia le correnti trova l\'oro.');
  } else if (wr >= 50) {
    q.push('Anche il miglior navigatore incontra correnti avverse. La rotta resta.');
    q.push('Mare incerto \u2014 ma chi ricalibra le vele non affonda mai.');
    q.push('Met\u00e0 delle onde sono nostre. L\'altra met\u00e0 la studieremo.');
  } else if (wr >= 40) {
    q.push('Tempesta. Ma \u00e8 nella tempesta che si distingue il capitano dal passeggero.');
    q.push('I mari peggiori forgiano i migliori navigatori.');
    q.push('Porto vicino. Chi sa quando fermarsi, sa anche quando ripartire.');
  } else {
    q.push('Nebbia fitta. Il capitano studia \u2014 chi sa aspettare, conquista.');
    q.push('I mari si calmano. E quando lo fanno, saremo i primi a salpare.');
    q.push('Anche Barbanera sapeva quando restare in porto.');
  }
  const quote = q[gh.evaluated % q.length];
  lines.push(`\ud83c\udff4\u200d\u2620\ufe0f <i>"${quote}"</i>`);
} else if (gh.pending > 0) {
  lines.push('');
  lines.push(`👻 \u23f3 ${gh.pending} segnali in mare aperto \u2014 verdetto in arrivo`);
}

state.lastRun = now.toISOString();
state.lastEquity = data.equity;
state.lastPosition = data.position;
if (shouldSend) {
  state.messagesSent = (state.messagesSent || 0) + 1;
  state.lastMessageTime = now.getTime();
  if (category === 'ROUTINE') state.lastRoutineTime = now.getTime();
}

return [{ json: { send: shouldSend, message: lines.join('\n'), category } }];
""".strip()

# ── Build workflow dict ──────────────────────────────────────────────────────
nodes = [
    {
        "parameters": {
            "triggerTimes": {
                "item": [{"mode": "everyX", "value": 3, "unit": "minutes"}]
            }
        },
        "id": "brain-trigger",
        "name": "Every 3 Minutes",
        "type": "n8n-nodes-base.cron",
        "typeVersion": 1,
        "position": [0, 300],
    },
    {
        "parameters": {
            "method": "POST",
            "url": BASE_URL + "/ghost-evaluate",
            "sendHeaders": True,
            "headerParameters": {
                "parameters": [{"name": "X-API-Key", "value": BOT_API_KEY}]
            },
            "options": {},
        },
        "id": "brain-ghost",
        "name": "Ghost Evaluate",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [260, 300],
        "onError": "continueRegularOutput",
    },
    {
        "parameters": {
            "method": "GET",
            "url": BASE_URL + "/brain-state",
            "options": {},
        },
        "id": "brain-fetch",
        "name": "Fetch Brain State",
        "type": "n8n-nodes-base.httpRequest",
        "typeVersion": 4.2,
        "position": [520, 300],
    },
    {
        "parameters": {"jsCode": NARRATOR_CODE},
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
                        "id": "cond-send",
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
            }
        },
        "id": "brain-if",
        "name": "Should Send?",
        "type": "n8n-nodes-base.if",
        "typeVersion": 2,
        "position": [1040, 300],
    },
    {
        "parameters": {
            "chatId": "368092324",
            "text": "={{ $json.message }}",
            "additionalFields": {
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        },
        "id": "brain-tg",
        "name": "Send to Mattia",
        "type": "n8n-nodes-base.telegram",
        "typeVersion": 1.2,
        "position": [1300, 200],
        "credentials": {
            "telegramApi": {
                "id": "DUBgkzRL1ONUstm5",
                "name": "Telegram account (BTC Commander)",
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
]

connections = {
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
}

workflow = {
    "name": "08_BTC_Brain_Monitor",
    "nodes": nodes,
    "connections": connections,
    "settings": {"executionOrder": "v1"},
}


def deploy():
    url = f"https://{N8N_HOST}/api/v1/workflows/{WF_ID}"
    payload = json.dumps(workflow).encode("utf-8")
    ctx = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(
        url, data=payload, method="PUT",
        headers={"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"},
    )
    print(f"Deploying wf08 Brain Monitor to {url}...")
    print(f"  Nodes: {len(nodes)}, Connections: {len(connections)}")
    try:
        with urllib.request.urlopen(req, context=ctx) as resp:
            d = json.loads(resp.read())
            print(f"✅ Deployed: {d.get('name')} (active={d.get('active')})")
            print(f"   Updated: {d.get('updatedAt')}")
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
        print("\n⚠️  Vai su n8n UI → Toggle OFF → Toggle ON per attivare il trigger!")
    sys.exit(0 if ok else 1)
