#!/usr/bin/env python3
"""Monitor for orphaned bets in Supabase and n8n."""

import requests
import json
from datetime import datetime, timedelta
import sys

# Supabase config from .mcp.json
SUPABASE_URL = "https://oimlamjilivrcnhztwvj.supabase.co"
SUPABASE_KEY = "REDACTED_SUPABASE_ANON_KEY"

# n8n config
N8N_API_URL = "https://n8n.srv1432354.hstgr.cloud"
N8N_API_KEY = "REDACTED_N8N_API_KEY"

WORKFLOW_ID = "NnjfpzgdIyleMVBO"  # wf02 BTC_Trade_Checker VPS
ALERTS_FILE = "/Users/mattiacalastri/.claude/projects/-Users-mattiacalastri-btc-predictions/memory/alerts.md"
ORPHAN_THRESHOLD_HOURS = 2


def get_open_bets():
    """Query Supabase for open bets (bet_taken=true AND correct IS NULL)."""
    headers = {
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    url = (f"{SUPABASE_URL}/rest/v1/btc_predictions"
           "?select=id,created_at,direction,confidence"
           "&bet_taken=eq.true&correct=is.null&entry_fill_price=not.is.null"
           "&order=created_at.desc&limit=10")

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"Error querying Supabase: {e}")
        return []


def check_orphaned_bets(open_bets):
    """Check which open bets are older than ORPHAN_THRESHOLD_HOURS."""
    orphaned = []
    now = datetime.utcnow()
    threshold = now - timedelta(hours=ORPHAN_THRESHOLD_HOURS)

    for bet in open_bets:
        created_at = datetime.fromisoformat(bet["created_at"].replace("Z", "+00:00"))
        if created_at < threshold:
            age_hours = (now - created_at).total_seconds() / 3600
            orphaned.append({
                "id": bet["id"],
                "created_at": bet["created_at"],
                "age_hours": age_hours,
                "direction": bet["direction"],
                "confidence": bet["confidence"]
            })

    return orphaned


def check_workflow_executions():
    """Check n8n workflow for active or waiting executions."""
    headers = {"X-N8N-API-KEY": N8N_API_KEY}
    url = f"{N8N_API_URL}/api/v1/executions?filter={json.dumps({'workflowId': WORKFLOW_ID})}"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        executions = resp.json().get("data", [])

        # Check for active or waiting executions
        active = [e for e in executions if e.get("status") in ["running", "waiting", "new"]]
        return active
    except Exception as e:
        print(f"Error querying n8n: {e}")
        return []


def write_alert(orphaned_bets, workflow_active):
    """Write alert to alerts.md if orphaned bets found."""
    if not orphaned_bets:
        # All good - no orphaned bets
        return False

    timestamp = datetime.now().isoformat()
    alert = f"\n## {timestamp}\n"
    alert += f"**Found {len(orphaned_bets)} orphaned bets:**\n"

    for bet in orphaned_bets:
        alert += f"- ID {bet['id']}: {bet['direction']} @ {bet['confidence']:.2f}% conf, age {bet['age_hours']:.1f}h\n"

    if workflow_active:
        alert += f"\nWorkflow {WORKFLOW_ID} has {len(workflow_active)} active executions.\n"
    else:
        alert += f"\n⚠️ Workflow {WORKFLOW_ID} has NO active executions (may be stuck).\n"

    try:
        with open(ALERTS_FILE, "a") as f:
            f.write(alert)
        return True
    except Exception as e:
        print(f"Error writing alerts: {e}")
        return False


def main():
    """Main monitoring function."""
    print("Checking for orphaned bets...")

    # Get open bets
    open_bets = get_open_bets()
    print(f"Found {len(open_bets)} open bets")

    # Check for orphaned
    orphaned = check_orphaned_bets(open_bets)
    print(f"Found {len(orphaned)} orphaned bets (>{ORPHAN_THRESHOLD_HOURS}h old)")

    # Check workflow
    workflow_active = check_workflow_executions()
    print(f"Workflow has {len(workflow_active)} active/waiting executions")

    # Write alert if needed
    if orphaned:
        if write_alert(orphaned, workflow_active):
            print(f"Alert written to {ALERTS_FILE}")
        else:
            print("Failed to write alert")
            sys.exit(1)
    else:
        print("✓ No orphaned bets found - all good!")


if __name__ == "__main__":
    main()
