#!/usr/bin/env python3
"""
Creates n8n workflow: daily auto-retrain at 03:00 UTC.
Calls POST /auto-retrain on Railway with X-API-Key header.

Usage:
    N8N_API_KEY=<key> python3 scripts/setup_daily_retrain.py
"""

import json
import os
import ssl
import urllib.request
import urllib.error

N8N_BASE = "https://n8n.srv1432354.hstgr.cloud"
N8N_KEY = os.environ.get("N8N_API_KEY", "")
RAILWAY_URL = "https://web-production-e27d0.up.railway.app"

if not N8N_KEY:
    print("ERROR: N8N_API_KEY env var required")
    print("Usage: N8N_API_KEY=<key> python3 scripts/setup_daily_retrain.py")
    exit(1)

_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE


def n8n_api(method, path, data=None):
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        f"{N8N_BASE}/api/v1{path}",
        data=body,
        headers={
            "X-N8N-API-KEY": N8N_KEY,
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, context=_ssl_ctx) as r:
        return json.loads(r.read())


# Check if workflow already exists
print("Checking existing workflows...")
workflows = n8n_api("GET", "/workflows?limit=100")
existing = [w for w in workflows.get("data", []) if "Daily Retrain" in w.get("name", "")]
if existing:
    print(f"  Workflow '{existing[0]['name']}' already exists (id={existing[0]['id']}). Skipping.")
    exit(0)

# Create the workflow
workflow = {
    "name": "10_Daily_Retrain_XGBoost",
    "nodes": [
        {
            "id": "schedule-retrain-001",
            "name": "Daily 03:00 UTC",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [0, 0],
            "parameters": {
                "rule": {
                    "interval": [
                        {
                            "triggerAtHour": 3,
                            "triggerAtMinute": 0,
                        }
                    ]
                }
            },
        },
        {
            "id": "retrain-http-001",
            "name": "POST /auto-retrain",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [220, 0],
            "continueOnFail": True,
            "parameters": {
                "method": "POST",
                "url": f"{RAILWAY_URL}/auto-retrain",
                "options": {},
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {
                            "name": "X-API-Key",
                            "value": "={{ $vars.BOT_API_KEY }}",
                        }
                    ]
                },
            },
        },
    ],
    "connections": {
        "Daily 03:00 UTC": {
            "main": [
                [
                    {
                        "node": "POST /auto-retrain",
                        "type": "main",
                        "index": 0,
                    }
                ]
            ]
        }
    },
    "settings": {
        "executionOrder": "v1",
    },
}

print("Creating workflow...")
try:
    result = n8n_api("POST", "/workflows", workflow)
    wf_id = result.get("id")
    print(f"  Created: '{result.get('name')}' (id={wf_id})")

    # Activate the workflow
    n8n_api("POST", f"/workflows/{wf_id}/activate", {})
    print(f"  Activated!")
    print(f"\nDone. Daily retrain scheduled at 03:00 UTC.")
    print(f"  n8n URL: {N8N_BASE}/workflow/{wf_id}")

except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"  ERROR HTTP {e.code}: {body[:300]}")
    exit(1)
