#!/usr/bin/env python3
"""
fix_ghost_evaluate.py
Aggiunge 'Ghost Evaluate SKIP' branch a wf08 (08_BTC_Position_Monitor).
Il nuovo nodo chiama /ghost-evaluate ogni 5 min, indipendente dai bet aperti.

Uso:
    N8N_API_KEY=<key> python3 scripts/fix_ghost_evaluate.py
"""

import json
import os
import ssl
import urllib.request
import urllib.error

# Mac Python ha SSL certs non configurati — bypass per chiamate interne
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

N8N_BASE = "https://n8n.srv1432354.hstgr.cloud"
N8N_KEY = os.environ.get("N8N_API_KEY", "")
WF_ID = "Fjk7M3cOEcL1aAVf"  # 08_BTC_Position_Monitor
RAILWAY_URL = "https://web-production-e27d0.up.railway.app"

if not N8N_KEY:
    print("ERROR: N8N_API_KEY env var non impostata")
    print("Uso: N8N_API_KEY=<key> python3 scripts/fix_ghost_evaluate.py")
    exit(1)


def n8n_get(path):
    req = urllib.request.Request(
        f"{N8N_BASE}/api/v1{path}",
        headers={"X-N8N-API-KEY": N8N_KEY}
    )
    with urllib.request.urlopen(req, context=_ssl_ctx) as r:
        return json.loads(r.read())


def n8n_put(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{N8N_BASE}/api/v1{path}",
        data=body,
        headers={
            "X-N8N-API-KEY": N8N_KEY,
            "Content-Type": "application/json",
        },
        method="PUT"
    )
    with urllib.request.urlopen(req, context=_ssl_ctx) as r:
        return json.loads(r.read())


def clean_settings(s):
    """Rimuove chiavi non ammesse dall'oggetto settings (evita 400)."""
    allowed = {
        "executionOrder", "saveManualExecutions", "saveExecutionProgress",
        "saveDataSuccessExecution", "saveDataErrorExecution",
        "timezone", "errorWorkflow", "callerPolicy", "callerIds"
    }
    return {k: v for k, v in (s or {}).items() if k in allowed}


# ── 1. Scarica wf08 corrente ───────────────────────────────────────────────
print(f"Fetching wf08 ({WF_ID})...")
wf = n8n_get(f"/workflows/{WF_ID}")

nodes = wf.get("nodes", [])
connections = wf.get("connections", {})

# Trova lo schedule trigger
sched_node = next(
    (n for n in nodes if n.get("id") == "sched-001" or
     n.get("type") == "n8n-nodes-base.scheduleTrigger"),
    None
)
if not sched_node:
    print("ERROR: Nodo scheduler non trovato in wf08")
    exit(1)

sched_name = sched_node["name"]
print(f"  Scheduler: '{sched_name}' (id={sched_node.get('id')})")

# Controlla se il nodo esiste già
existing = next((n for n in nodes if n.get("id") == "ghost-eval-wf08-001"), None)
if existing:
    print("  Ghost Evaluate SKIP già presente in wf08. Nessuna modifica.")
    exit(0)

# ── 2. Nuovo nodo HTTP Ghost Evaluate ─────────────────────────────────────
new_node = {
    "id": "ghost-eval-wf08-001",
    "name": "Ghost Evaluate SKIP",
    "type": "n8n-nodes-base.httpRequest",
    "typeVersion": 4.2,
    "position": [240, 420],
    "continueOnFail": True,
    "parameters": {
        "method": "POST",
        "url": f"{RAILWAY_URL}/ghost-evaluate",
        "options": {},
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [
                {
                    "name": "X-API-Key",
                    "value": "={{ $vars.BOT_API_KEY }}"
                }
            ]
        }
    }
}

nodes.append(new_node)
print(f"  Nodo aggiunto: '{new_node['name']}'")

# ── 3. Aggiorna connections: Schedule → Ghost Evaluate SKIP ───────────────
if sched_name not in connections:
    connections[sched_name] = {"main": [[]]}

main_conn = connections[sched_name].get("main", [[]])

# Aggiungi il nuovo nodo come output 0 del scheduler
target = {"node": "Ghost Evaluate SKIP", "type": "main", "index": 0}

# Cerca se esiste già un array su output[0], altrimenti crea
if len(main_conn) == 0:
    main_conn.append([target])
else:
    if not isinstance(main_conn[0], list):
        main_conn[0] = []
    main_conn[0].append(target)

connections[sched_name]["main"] = main_conn
print(f"  Connection aggiunta: {sched_name} → Ghost Evaluate SKIP")

# ── 4. PUT wf08 aggiornato ─────────────────────────────────────────────────
payload = {
    "name": wf["name"],
    "nodes": nodes,
    "connections": connections,
    "settings": clean_settings(wf.get("settings", {})),
    "staticData": wf.get("staticData"),
}

print("Sending PUT to n8n...")
try:
    result = n8n_put(f"/workflows/{WF_ID}", payload)
    print(f"  ✅ PUT OK — workflow '{result.get('name')}' aggiornato")
    print(f"  Nodi totali: {len(result.get('nodes', []))}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"  ❌ HTTP {e.code}: {body[:300]}")
    exit(1)

print("\nDone. Ghost Evaluate SKIP ora gira ogni 5 min in wf08.")
print("PROSSIMO STEP: esegui la SQL migration in Supabase (vedi output sotto)\n")
print("─" * 60)
print("SQL DA ESEGUIRE IN SUPABASE SQL EDITOR:")
print("─" * 60)
print("""
ALTER TABLE btc_predictions
  ADD COLUMN IF NOT EXISTS ghost_exit_price  numeric,
  ADD COLUMN IF NOT EXISTS ghost_correct     boolean,
  ADD COLUMN IF NOT EXISTS ghost_evaluated_at timestamptz;
""")
