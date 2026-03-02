#!/usr/bin/env python3
"""Patch wf00 (00_Error_Notifier) → Error Intelligence Hub.

Replaces the 2-node workflow (Error Trigger → Telegram) with a full
error classification, dedup, logging, severity-based notification, and
auto-recovery pipeline.

Architecture:
  Error Trigger → Error Classifier (Code) → Dedup Query (Supabase) →
  Dedup Check (Code) →
    IF duplicate: Log as Duplicate (Supabase) → Stop
    IF new: Log Error (Supabase) → Switch Severity →
      P0: Telegram Critical + Auto-Pause Bot
      P1: Telegram Warning
      P2: Telegram Info
      P3: (log only, no notification)

Requires: bot_errors table on Supabase (see create_bot_errors_table.py)

Usage:
    cd btc_predictions
    python3 scripts/patch_wf00_error_intelligence.py
"""

import ssl
import urllib.request
import json
import os
import uuid
import certifi
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ctx = ssl.create_default_context(cafile=certifi.where())
n8n_host = os.environ['N8N_HOST']
n8n_key = os.environ['N8N_API_KEY']

WF_ID = 'Yg0o2MaBZBHYq7Wc'
CHAT_ID = '368092324'

# ── Credentials (from existing wf00/wf01B) ──
TELEGRAM_CREDS = {"telegramApi": {"id": "DUBgkzRL1ONUstm5", "name": "Telegram account (BTC Commander)"}}
SUPABASE_CREDS = {"supabaseApi": {"id": "xaGS2AzVGYaV8WR8", "name": "Supabase account"}}

# ── Fetch current workflow ──
print(f"Fetching wf00 ({WF_ID})...")
url = f'https://{n8n_host}/api/v1/workflows/{WF_ID}'
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': n8n_key})
data = json.loads(urllib.request.urlopen(req, context=ctx, timeout=15).read())

print(f"  Name: {data['name']}")
print(f"  Nodes: {len(data['nodes'])}")
print(f"  Active: {data.get('active', False)}")

# ── Idempotency check ──
node_names = [n['name'] for n in data['nodes']]
if 'Error Classifier' in node_names:
    print("\n✅ Already patched (Error Classifier node exists). No changes needed.")
    exit(0)

changes = []

# ══════════════════════════════════════════════════════════════
# NODE DEFINITIONS
# ══════════════════════════════════════════════════════════════

# Keep the Error Trigger node as-is
error_trigger = None
for n in data['nodes']:
    if n['type'] == 'n8n-nodes-base.errorTrigger':
        error_trigger = n
        break

if not error_trigger:
    print("❌ Error Trigger node not found!")
    exit(1)

# Base X position for the flow (Error Trigger is at [0, 0])
X_BASE = 300
Y_BASE = 0

# ── Node 1: Error Classifier (Code) ──
error_classifier = {
    "parameters": {
        "jsCode": """// ── Error Intelligence Hub: Classifier ──
const error = $input.first().json;
const wfName = error.workflow?.name ?? 'unknown';
const wfId = error.workflow?.id ?? '';
const nodeName = error.execution?.lastNodeExecuted ?? 'unknown';
const errorMsg = error.execution?.error?.message ?? JSON.stringify(error.execution?.error ?? '');
const execId = error.execution?.id ?? '';
const execUrl = error.execution?.url ?? '';

// ── Error fingerprint (dedup key) ──
const fingerprint = `${wfId}:${nodeName}:${errorMsg.slice(0,80).replace(/[^a-zA-Z0-9_:.\\-]/g, '_')}`;

// ── Severity classification ──
let severity = 'P2'; // default medium
let errorType = 'unknown';

// P0 — Critical: bot can't trade
const P0_PATTERNS = [
  { match: /kraken.*api|kraken.*error|exchange.*down/i, type: 'exchange_api_down' },
  { match: /insufficient.*funds|not.*enough.*balance/i, type: 'insufficient_funds' },
  { match: /authentication|api.key|unauthorized|401/i, type: 'auth_failure' },
  { match: /supabase.*connect|supabase.*timeout|supabase.*error/i, type: 'db_connection' },
  { match: /ECONNREFUSED.*railway|railway.*down|ENOTFOUND/i, type: 'railway_down' },
  { match: /circuit.?breaker/i, type: 'circuit_breaker_tripped' },
];

// P1 — High: data integrity at risk
const P1_PATTERNS = [
  { match: /on.?chain|polygon|web3|revert|nonce/i, type: 'onchain_failure' },
  { match: /ghost.*evaluat/i, type: 'ghost_eval_failure' },
  { match: /confidence.*null|direction.*null|null.*critical/i, type: 'null_critical_field' },
  { match: /duplicate.*entry|unique.*constraint|unique.*violation/i, type: 'data_duplicate' },
  { match: /pnl.*null|pnl.*NaN|entry_price.*null|entry.*n\\/a/i, type: 'pnl_data_missing' },
];

// P2 — Medium: degraded but operational
const P2_PATTERNS = [
  { match: /timeout|ETIMEDOUT|ESOCKETTIMEDOUT/i, type: 'timeout' },
  { match: /rate.?limit|429|too.many.requests/i, type: 'rate_limit' },
  { match: /telegram.*send|telegram.*api|telegram.*error/i, type: 'telegram_failure' },
  { match: /retrain|xgboost|model.*load/i, type: 'retrain_failure' },
];

// P3 — Low: informational
const P3_PATTERNS = [
  { match: /no.*position|position.*not.*found/i, type: 'no_position' },
  { match: /skip|noise|filtered|threshold.*not/i, type: 'signal_filtered' },
  { match: /already.*exists|duplicate.*skip|idempotent/i, type: 'idempotent_skip' },
];

for (const p of P0_PATTERNS) {
  if (p.match.test(errorMsg)) { severity = 'P0'; errorType = p.type; break; }
}
if (severity === 'P2') {
  for (const p of P1_PATTERNS) {
    if (p.match.test(errorMsg)) { severity = 'P1'; errorType = p.type; break; }
  }
}
if (severity === 'P2') {
  for (const p of P2_PATTERNS) {
    if (p.match.test(errorMsg)) { errorType = p.type; break; }
  }
}
if (severity === 'P2' && errorType === 'unknown') {
  for (const p of P3_PATTERNS) {
    if (p.match.test(errorMsg)) { severity = 'P3'; errorType = p.type; break; }
  }
}

return [{
  json: {
    severity, errorType, fingerprint,
    workflowId: wfId, workflowName: wfName,
    nodeName, executionId: execId, executionUrl: execUrl,
    errorMessage: errorMsg.slice(0, 2000),
    timestamp: new Date().toISOString(),
    context: {
      workflow: wfName,
      node: nodeName,
      executionId: execId
    }
  }
}];
"""
    },
    "type": "n8n-nodes-base.code",
    "typeVersion": 2,
    "position": [X_BASE, Y_BASE],
    "id": str(uuid.uuid4()),
    "name": "Error Classifier"
}
changes.append("Added Error Classifier node (P0-P3 severity + fingerprint)")

# ── Node 2: Dedup Query (Supabase GET — count recent same fingerprint) ──
dedup_query = {
    "parameters": {
        "operation": "getAll",
        "tableId": "bot_errors",
        "returnAll": False,
        "limit": 10,
        "filters": {
            "conditions": [
                {
                    "keyName": "error_fingerprint",
                    "condition": "eq",
                    "keyValue": "={{ $json.fingerprint }}"
                },
                {
                    "keyName": "created_at",
                    "condition": "gte",
                    "keyValue": "={{ new Date(Date.now() - 15*60*1000).toISOString() }}"
                }
            ]
        }
    },
    "type": "n8n-nodes-base.supabase",
    "typeVersion": 1,
    "position": [X_BASE + 250, Y_BASE],
    "id": str(uuid.uuid4()),
    "name": "Dedup Query",
    "credentials": SUPABASE_CREDS,
    "continueOnFail": True
}
changes.append("Added Dedup Query node (Supabase: recent same fingerprint)")

# ── Node 3: Dedup Check (Code — decide if duplicate) ──
dedup_check = {
    "parameters": {
        "jsCode": """// Check if this error is a duplicate (>= 3 same fingerprint in 15min)
const classifierData = $('Error Classifier').first().json;
const recentErrors = $input.all().map(i => i.json);

// If dedup query failed (continueOnFail), treat as non-duplicate
const isDuplicate = Array.isArray(recentErrors) && recentErrors.length >= 3
  && recentErrors[0]?.id != null;
const duplicateOf = isDuplicate ? recentErrors[0]?.id : null;

return [{
  json: {
    ...classifierData,
    isDuplicate,
    duplicateOf,
    recentCount: Array.isArray(recentErrors) ? recentErrors.filter(r => r.id).length : 0
  }
}];
"""
    },
    "type": "n8n-nodes-base.code",
    "typeVersion": 2,
    "position": [X_BASE + 500, Y_BASE],
    "id": str(uuid.uuid4()),
    "name": "Dedup Check"
}
changes.append("Added Dedup Check node (>= 3 same fingerprint in 15min = duplicate)")

# ── Node 4: If Duplicate ──
if_duplicate = {
    "parameters": {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
            "conditions": [
                {
                    "id": str(uuid.uuid4()),
                    "leftValue": "={{ $json.isDuplicate }}",
                    "rightValue": True,
                    "operator": {
                        "type": "boolean",
                        "operation": "equals"
                    }
                }
            ],
            "combinator": "and"
        }
    },
    "type": "n8n-nodes-base.if",
    "typeVersion": 2,
    "position": [X_BASE + 750, Y_BASE],
    "id": str(uuid.uuid4()),
    "name": "Is Duplicate?"
}
changes.append("Added Is Duplicate? node (If)")

# ── Node 5a: Log Duplicate (Supabase INSERT — with duplicate_of) ──
log_duplicate = {
    "parameters": {
        "tableId": "bot_errors",
        "fieldsUi": {
            "fieldValues": [
                {"fieldId": "workflow_id", "fieldValue": "={{ $json.workflowId }}"},
                {"fieldId": "workflow_name", "fieldValue": "={{ $json.workflowName }}"},
                {"fieldId": "node_name", "fieldValue": "={{ $json.nodeName }}"},
                {"fieldId": "execution_id", "fieldValue": "={{ $json.executionId }}"},
                {"fieldId": "severity", "fieldValue": "={{ $json.severity }}"},
                {"fieldId": "error_type", "fieldValue": "={{ $json.errorType }}"},
                {"fieldId": "error_message", "fieldValue": "={{ $json.errorMessage }}"},
                {"fieldId": "error_fingerprint", "fieldValue": "={{ $json.fingerprint }}"},
                {"fieldId": "context", "fieldValue": "={{ JSON.stringify($json.context) }}"},
                {"fieldId": "notification_sent", "fieldValue": "=false"},
                {"fieldId": "duplicate_of", "fieldValue": "={{ $json.duplicateOf }}"},
            ]
        }
    },
    "type": "n8n-nodes-base.supabase",
    "typeVersion": 1,
    "position": [X_BASE + 1000, Y_BASE - 200],
    "id": str(uuid.uuid4()),
    "name": "Log Duplicate",
    "credentials": SUPABASE_CREDS,
    "continueOnFail": True
}
changes.append("Added Log Duplicate node (Supabase INSERT with duplicate_of)")

# ── Node 5b: Log Error (Supabase INSERT — new unique error) ──
log_error = {
    "parameters": {
        "tableId": "bot_errors",
        "fieldsUi": {
            "fieldValues": [
                {"fieldId": "workflow_id", "fieldValue": "={{ $json.workflowId }}"},
                {"fieldId": "workflow_name", "fieldValue": "={{ $json.workflowName }}"},
                {"fieldId": "node_name", "fieldValue": "={{ $json.nodeName }}"},
                {"fieldId": "execution_id", "fieldValue": "={{ $json.executionId }}"},
                {"fieldId": "severity", "fieldValue": "={{ $json.severity }}"},
                {"fieldId": "error_type", "fieldValue": "={{ $json.errorType }}"},
                {"fieldId": "error_message", "fieldValue": "={{ $json.errorMessage }}"},
                {"fieldId": "error_fingerprint", "fieldValue": "={{ $json.fingerprint }}"},
                {"fieldId": "context", "fieldValue": "={{ JSON.stringify($json.context) }}"},
                {"fieldId": "notification_sent", "fieldValue": "=true"},
            ]
        }
    },
    "type": "n8n-nodes-base.supabase",
    "typeVersion": 1,
    "position": [X_BASE + 1000, Y_BASE + 200],
    "id": str(uuid.uuid4()),
    "name": "Log Error",
    "credentials": SUPABASE_CREDS,
    "continueOnFail": True
}
changes.append("Added Log Error node (Supabase INSERT — new unique error)")

# ── Node 6: Switch Severity ──
switch_severity = {
    "parameters": {
        "rules": {
            "values": [
                {
                    "conditions": {
                        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                        "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ $('Dedup Check').first().json.severity }}", "rightValue": "P0", "operator": {"type": "string", "operation": "equals"}}],
                        "combinator": "and"
                    },
                    "renameOutput": True,
                    "outputKey": "P0"
                },
                {
                    "conditions": {
                        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                        "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ $('Dedup Check').first().json.severity }}", "rightValue": "P1", "operator": {"type": "string", "operation": "equals"}}],
                        "combinator": "and"
                    },
                    "renameOutput": True,
                    "outputKey": "P1"
                },
                {
                    "conditions": {
                        "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "strict"},
                        "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ $('Dedup Check').first().json.severity }}", "rightValue": "P2", "operator": {"type": "string", "operation": "equals"}}],
                        "combinator": "and"
                    },
                    "renameOutput": True,
                    "outputKey": "P2"
                },
            ],
            "fallbackOutput": {
                "renameOutput": True,
                "outputKey": "P3"
            }
        }
    },
    "type": "n8n-nodes-base.switch",
    "typeVersion": 3.2,
    "position": [X_BASE + 1250, Y_BASE + 200],
    "id": str(uuid.uuid4()),
    "name": "Switch Severity"
}
changes.append("Added Switch Severity node (P0/P1/P2/P3 routing)")

# ── Node 7a: Telegram P0 Critical ──
telegram_p0 = {
    "parameters": {
        "chatId": CHAT_ID,
        "text": """=🚨 <b>CRITICAL ERROR</b> | {{ $now.setZone('Europe/Rome').toFormat('HH:mm') }}
━━━━━━━━━━━━━━━━━━
⚠️ <code>{{ $('Dedup Check').first().json.errorType }}</code>
📋 {{ $('Dedup Check').first().json.workflowName }} → {{ $('Dedup Check').first().json.nodeName }}
💬 <code>{{ ($('Dedup Check').first().json.errorMessage ?? '').slice(0, 200).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }}</code>

🔴 <b>BOT AUTO-PAUSED</b>
🔗 <a href="{{ $('Dedup Check').first().json.executionUrl }}">→ Execution</a>""",
        "additionalFields": {
            "appendAttribution": False,
            "parse_mode": "HTML"
        }
    },
    "type": "n8n-nodes-base.telegram",
    "typeVersion": 1.2,
    "position": [X_BASE + 1550, Y_BASE - 100],
    "id": str(uuid.uuid4()),
    "name": "Telegram P0 Critical",
    "credentials": TELEGRAM_CREDS,
    "continueOnFail": True
}
changes.append("Added Telegram P0 Critical node")

# ── Node 7b: Telegram P1 Warning ──
telegram_p1 = {
    "parameters": {
        "chatId": CHAT_ID,
        "text": """=⚠️ <b>ERROR</b> | {{ $now.setZone('Europe/Rome').toFormat('HH:mm') }}
{{ $('Dedup Check').first().json.workflowName }} → {{ $('Dedup Check').first().json.nodeName }}
<code>{{ $('Dedup Check').first().json.errorType }}</code>: {{ ($('Dedup Check').first().json.errorMessage ?? '').slice(0, 150).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }}
🔗 <a href="{{ $('Dedup Check').first().json.executionUrl }}">→ Execution</a>""",
        "additionalFields": {
            "appendAttribution": False,
            "parse_mode": "HTML"
        }
    },
    "type": "n8n-nodes-base.telegram",
    "typeVersion": 1.2,
    "position": [X_BASE + 1550, Y_BASE + 100],
    "id": str(uuid.uuid4()),
    "name": "Telegram P1 Warning",
    "credentials": TELEGRAM_CREDS,
    "continueOnFail": True
}
changes.append("Added Telegram P1 Warning node")

# ── Node 7c: Telegram P2 Info ──
telegram_p2 = {
    "parameters": {
        "chatId": CHAT_ID,
        "text": """=ℹ️ {{ $('Dedup Check').first().json.errorType }} | {{ $('Dedup Check').first().json.workflowName }}
{{ ($('Dedup Check').first().json.errorMessage ?? '').slice(0, 100).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') }}""",
        "additionalFields": {
            "appendAttribution": False,
            "parse_mode": "HTML"
        }
    },
    "type": "n8n-nodes-base.telegram",
    "typeVersion": 1.2,
    "position": [X_BASE + 1550, Y_BASE + 300],
    "id": str(uuid.uuid4()),
    "name": "Telegram P2 Info",
    "credentials": TELEGRAM_CREDS,
    "continueOnFail": True
}
changes.append("Added Telegram P2 Info node")

# ── Node 8: Auto-Pause Bot (HTTP POST /pause — P0 only) ──
auto_pause = {
    "parameters": {
        "method": "POST",
        "url": "https://web-production-e27d0.up.railway.app/pause",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [
                {
                    "name": "X-API-Key",
                    "value": "={{ $env.BOT_API_KEY }}"
                }
            ]
        },
        "options": {
            "timeout": 10000
        }
    },
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.3,
    "position": [X_BASE + 1850, Y_BASE - 100],
    "id": str(uuid.uuid4()),
    "name": "Auto-Pause Bot",
    "continueOnFail": True
}
changes.append("Added Auto-Pause Bot node (POST /pause on P0)")

# ── Node 9: Recovery Attempt (Code — P0/P1) ──
recovery_attempt = {
    "parameters": {
        "jsCode": """// ── Recovery attempt for known error patterns ──
const data = $('Dedup Check').first().json;
const type = data.errorType;
let recoveryAction = 'manual_intervention';
let recoveryNote = '';

switch(type) {
  case 'ghost_eval_failure':
    recoveryAction = 'retry_ghost_evaluate';
    recoveryNote = 'Will retry on next wf08 cycle (5 min)';
    break;
  case 'telegram_failure':
    recoveryAction = 'retry_telegram_5s';
    recoveryNote = 'Telegram rate limit — next message will retry';
    break;
  case 'timeout':
  case 'rate_limit':
    recoveryAction = 'wait_next_cycle';
    recoveryNote = 'Transient — next scheduled run will retry';
    break;
  case 'no_position':
  case 'signal_filtered':
  case 'idempotent_skip':
    recoveryAction = 'no_action_needed';
    recoveryNote = 'Expected behavior, no recovery needed';
    break;
  default:
    recoveryAction = 'manual_intervention';
    recoveryNote = `Unknown error type: ${type}. Check execution.`;
}

return [{
  json: {
    errorId: $input.first().json?.id ?? null,
    recoveryAction,
    recoveryNote,
    severity: data.severity,
    errorType: type
  }
}];
"""
    },
    "type": "n8n-nodes-base.code",
    "typeVersion": 2,
    "position": [X_BASE + 1850, Y_BASE + 100],
    "id": str(uuid.uuid4()),
    "name": "Recovery Attempt"
}
changes.append("Added Recovery Attempt node")

# ══════════════════════════════════════════════════════════════
# BUILD NEW NODES LIST
# ══════════════════════════════════════════════════════════════

# Keep Error Trigger, remove old Notify Error
new_nodes = [error_trigger]
new_nodes.extend([
    error_classifier,
    dedup_query,
    dedup_check,
    if_duplicate,
    log_duplicate,
    log_error,
    switch_severity,
    telegram_p0,
    telegram_p1,
    telegram_p2,
    auto_pause,
    recovery_attempt,
])

data['nodes'] = new_nodes
changes.append("Removed old 'Notify Error' node")

# ══════════════════════════════════════════════════════════════
# BUILD CONNECTIONS
# ══════════════════════════════════════════════════════════════

data['connections'] = {
    # Error Trigger → Error Classifier
    "Error Trigger": {
        "main": [[{"node": "Error Classifier", "type": "main", "index": 0}]]
    },
    # Error Classifier → Dedup Query
    "Error Classifier": {
        "main": [[{"node": "Dedup Query", "type": "main", "index": 0}]]
    },
    # Dedup Query → Dedup Check
    "Dedup Query": {
        "main": [[{"node": "Dedup Check", "type": "main", "index": 0}]]
    },
    # Dedup Check → Is Duplicate?
    "Dedup Check": {
        "main": [[{"node": "Is Duplicate?", "type": "main", "index": 0}]]
    },
    # Is Duplicate? → TRUE: Log Duplicate | FALSE: Log Error
    "Is Duplicate?": {
        "main": [
            # Output 0 = TRUE (duplicate)
            [{"node": "Log Duplicate", "type": "main", "index": 0}],
            # Output 1 = FALSE (new error)
            [{"node": "Log Error", "type": "main", "index": 0}]
        ]
    },
    # Log Duplicate → (end, no notification)
    # Log Error → Switch Severity
    "Log Error": {
        "main": [[{"node": "Switch Severity", "type": "main", "index": 0}]]
    },
    # Switch Severity → P0: Telegram Critical | P1: Telegram Warning | P2: Telegram Info | P3: (end)
    "Switch Severity": {
        "main": [
            # Output 0 = P0
            [{"node": "Telegram P0 Critical", "type": "main", "index": 0}],
            # Output 1 = P1
            [{"node": "Telegram P1 Warning", "type": "main", "index": 0}],
            # Output 2 = P2
            [{"node": "Telegram P2 Info", "type": "main", "index": 0}],
            # Output 3 = P3 (fallback) — no notification, just logged
            []
        ]
    },
    # Telegram P0 → Auto-Pause Bot
    "Telegram P0 Critical": {
        "main": [[{"node": "Auto-Pause Bot", "type": "main", "index": 0}]]
    },
    # Telegram P1 → Recovery Attempt
    "Telegram P1 Warning": {
        "main": [[{"node": "Recovery Attempt", "type": "main", "index": 0}]]
    },
}
changes.append("Rewired all connections (13-node pipeline)")

# ══════════════════════════════════════════════════════════════
# SAVE WORKFLOW
# ══════════════════════════════════════════════════════════════

# Preserve safe settings
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

print(f"\n📋 Changes ({len(changes)}):")
for c in changes:
    print(f"  ✅ {c}")

print(f"\nSaving workflow ({len(new_nodes)} nodes)...")
save_req = urllib.request.Request(
    f'https://{n8n_host}/api/v1/workflows/{WF_ID}',
    data=payload, method='PUT',
    headers={'X-N8N-API-KEY': n8n_key, 'Content-Type': 'application/json'}
)

try:
    resp = urllib.request.urlopen(save_req, context=ctx, timeout=30)
    result = json.loads(resp.read())
    print(f"\n🚀 SAVED! Updated: {result.get('updatedAt', '?')}")
    print(f"   Nodes: {len(result.get('nodes', []))}")
    print(f"   Active: {result.get('active', '?')}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"\n❌ Error {e.code}: {body[:500]}")
    exit(1)
