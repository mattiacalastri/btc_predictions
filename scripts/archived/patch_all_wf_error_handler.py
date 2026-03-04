#!/usr/bin/env python3
"""Set wf00 (Error Intelligence Hub) as errorWorkflow on all BTC Bot workflows.

This ensures that ANY unhandled error in any workflow triggers wf00's
Error Intelligence pipeline (classify → log → dedup → notify → recover).

Target workflows (core BTC bot):
  01A, 01B, 02, 03, 04, 05, 06, 07, 08, 09A, 10_Retrain, 10_Sentry,
  11, 12, 13, 14, 15

Excluded: wf00 itself (can't be its own error handler)

Usage:
    cd btc_predictions
    python3 scripts/patch_all_wf_error_handler.py
"""

import ssl
import urllib.request
import json
import os
import certifi
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

ctx = ssl.create_default_context(cafile=certifi.where())
n8n_host = os.environ['N8N_HOST']
n8n_key = os.environ['N8N_API_KEY']

WF00_ID = 'Yg0o2MaBZBHYq7Wc'

# ── Fetch all active workflows ──
print("Fetching all active workflows...")
url = f'https://{n8n_host}/api/v1/workflows?active=true'
req = urllib.request.Request(url, headers={'X-N8N-API-KEY': n8n_key})
resp = json.loads(urllib.request.urlopen(req, context=ctx, timeout=15).read())

workflows = resp.get('data', [])
print(f"  Found {len(workflows)} active workflows\n")

changes = []
skipped = []
errors = []

for wf in workflows:
    wf_id = wf['id']
    wf_name = wf['name']

    # Skip wf00 itself
    if wf_id == WF00_ID:
        skipped.append(f"{wf_name} (self — skip)")
        continue

    # Fetch full workflow to check current settings
    try:
        detail_url = f'https://{n8n_host}/api/v1/workflows/{wf_id}'
        detail_req = urllib.request.Request(detail_url, headers={'X-N8N-API-KEY': n8n_key})
        detail = json.loads(urllib.request.urlopen(detail_req, context=ctx, timeout=15).read())
    except Exception as e:
        errors.append(f"{wf_name} ({wf_id}): fetch failed — {e}")
        continue

    settings = detail.get('settings', {})
    current_error_wf = settings.get('errorWorkflow', '')

    # Already set?
    if current_error_wf == WF00_ID:
        skipped.append(f"{wf_name} (already set)")
        continue

    # Build updated settings
    allowed_settings = {}
    for k in ('executionOrder', 'saveManualExecutions', 'callerPolicy',
              'errorWorkflow', 'timezone', 'saveExecutionProgress'):
        if k in settings:
            allowed_settings[k] = settings[k]

    # Set errorWorkflow
    allowed_settings['errorWorkflow'] = WF00_ID

    payload = json.dumps({
        'name': detail['name'],
        'nodes': detail['nodes'],
        'connections': detail['connections'],
        'settings': allowed_settings,
    }).encode()

    try:
        save_url = f'https://{n8n_host}/api/v1/workflows/{wf_id}'
        save_req = urllib.request.Request(
            save_url, data=payload, method='PUT',
            headers={'X-N8N-API-KEY': n8n_key, 'Content-Type': 'application/json'}
        )
        save_resp = urllib.request.urlopen(save_req, context=ctx, timeout=30)
        result = json.loads(save_resp.read())
        old = current_error_wf or '(none)'
        changes.append(f"{wf_name} — errorWorkflow: {old} → {WF00_ID}")
        print(f"  ✅ {wf_name}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        errors.append(f"{wf_name} ({wf_id}): save failed {e.code} — {body[:200]}")
        print(f"  ❌ {wf_name}: {e.code}")

# ── Summary ──
print(f"\n{'='*50}")
print(f"SUMMARY")
print(f"{'='*50}")

if changes:
    print(f"\n✅ Updated ({len(changes)}):")
    for c in changes:
        print(f"   {c}")

if skipped:
    print(f"\n⏭️  Skipped ({len(skipped)}):")
    for s in skipped:
        print(f"   {s}")

if errors:
    print(f"\n❌ Errors ({len(errors)}):")
    for e in errors:
        print(f"   {e}")

total = len(changes) + len(skipped)
print(f"\n🚀 Done! {len(changes)} updated, {len(skipped)} skipped, {len(errors)} errors")
print(f"   All errors from {total} workflows now route to wf00 Error Intelligence Hub")
