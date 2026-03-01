#!/usr/bin/env python3
"""
BTC Predictor Bot — Multi-Clone Orchestrator v2 (Hardened)
==========================================================
Launches 6 specialized Claude Code clones in 2 phases:
  Phase A (read-heavy):  C3 Security, C4 Compliance, C5 R&D, C6 Trading
  Phase B (write-heavy): C1 Full Stack, C2 Blockchain

Uses `claude -p` headless mode with:
  - --output-format stream-json for real-time streaming
  - --allowedTools for safe tool whitelisting (no --dangerously-skip-permissions)
  - --max-budget-usd per clone
  - --max-turns per clone

Dashboard: single terminal with rich library for unified monitoring.

Usage:
  cd ~/btc_predictions && python3 scripts/orchestrator.py
  cd ~/btc_predictions && python3 scripts/orchestrator.py --dry-run
  cd ~/btc_predictions && python3 scripts/orchestrator.py --phase-a-only
"""

import subprocess
import threading
import json
import time
import os
import sys
import signal
import argparse
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REPO_DIR = Path.home() / "btc_predictions"
PROMPTS_DIR = REPO_DIR / "scripts" / "prompts"
RESULTS_DIR = REPO_DIR / "scripts" / "results"
ICLOUD_DIR = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "\U0001f916 BTC Predictor Bot"  # robot emoji
    / "\U0001f5c2\ufe0f Claude Sessions"  # folder emoji
)

HEARTBEAT_TIMEOUT_SEC = 300  # 5 min — alert if no output

# Supabase cockpit push (reads from env vars, falls back to .mcp.json)
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")

if not SUPABASE_URL or not SUPABASE_KEY:
    try:
        _mcp = json.loads((REPO_DIR / ".mcp.json").read_text())
        _sb_env = _mcp.get("mcpServers", {}).get("supabase", {}).get("env", {})
        if not SUPABASE_URL:
            SUPABASE_URL = "https://oimlamjilivrcnhztwvj.supabase.co"
        if not SUPABASE_KEY:
            SUPABASE_KEY = _sb_env.get("SUPABASE_SERVICE_ROLE_KEY", "")
    except Exception:
        pass


def _push_cockpit_state(state: "CloneState"):
    """Push agent state to Supabase cockpit_events table for web dashboard."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        import urllib.request
        payload = json.dumps({
            "clone_id": state.config.id,
            "name": state.config.name,
            "role": state.config.role,
            "status": state.status,
            "model": state.config.model,
            "phase": state.config.phase,
            "current_task": state.last_message[:200] if state.last_message else "",
            "last_message": state.last_message[:500] if state.last_message else "",
            "thought": "",
            "cost_usd": state.cost_usd,
            "max_budget": state.config.max_budget,
            "elapsed_sec": (time.time() - state.start_time) if state.start_time else 0,
            "tasks_json": "[]",
            "next_action": "",
            "next_action_time": "",
            "result_summary": state.result_text[:500] if state.result_text else "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/cockpit_events",
            data=payload,
            method="POST",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates",
            },
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Non-blocking: cockpit push failure never stops the orchestrator

@dataclass
class CloneConfig:
    id: str
    name: str
    role: str
    prompt_file: str
    model: str
    max_budget: float
    max_turns: int
    phase: str  # "A" or "B"
    allowed_tools: str = "Read,Edit,Write,Glob,Grep,Bash"

CLONES = [
    # --- Batch: Post Go-Live Audit & Hardening (2 Mar 2026) ---
    # All Phase A (read-heavy audit). C1/C6 use Opus (write code), rest Sonnet (reports).
    CloneConfig("c3", "C3 Security", "Cybersecurity Expert",
                "c3_security.txt", "claude-sonnet-4-6", 5.0, 15, "A",
                "Read,Write,Glob,Grep"),
    CloneConfig("c4", "C4 Compliance", "Legal & Compliance",
                "c4_compliance.txt", "claude-sonnet-4-6", 5.0, 15, "A"),
    CloneConfig("c5", "C5 R&D", "Research & Development",
                "c5_rnd.txt", "claude-sonnet-4-6", 5.0, 15, "A"),
    CloneConfig("c6", "C6 Trading", "Trading & Probabilistic Master",
                "c6_trading.txt", "claude-opus-4-6", 7.0, 20, "A"),
    CloneConfig("c1", "C1 Full Stack", "Full Stack Developer",
                "c1_fullstack.txt", "claude-opus-4-6", 8.0, 20, "A"),
    CloneConfig("c2", "C2 Blockchain", "Crypto & Blockchain Expert",
                "c2_blockchain.txt", "claude-sonnet-4-6", 5.0, 15, "A"),
]

# ---------------------------------------------------------------------------
# Clone State
# ---------------------------------------------------------------------------

@dataclass
class CloneState:
    config: CloneConfig
    status: str = "pending"       # pending | running | done | error
    process: subprocess.Popen = None
    session_id: str = ""
    last_output_time: float = 0.0
    last_message: str = ""
    cost_usd: float = 0.0
    start_time: float = 0.0
    end_time: float = 0.0
    output_lines: list = field(default_factory=list)
    result_text: str = ""
    error: str = ""
    ddl_ready: bool = False       # C2 specific: DDL file created

# ---------------------------------------------------------------------------
# Terminal UI (works without rich — graceful fallback)
# ---------------------------------------------------------------------------

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.layout import Layout
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

def clear_screen():
    os.system("clear" if os.name != "nt" else "cls")

def format_elapsed(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"

def status_icon(status: str) -> str:
    return {"pending": "⏳", "running": "🔄", "done": "✅", "error": "❌"}.get(status, "?")

def render_dashboard_plain(states: dict, start_time: float, alerts: list):
    """Fallback dashboard without rich library."""
    clear_screen()
    elapsed = time.time() - start_time
    total_cost = sum(s.cost_usd for s in states.values())
    total_budget = sum(s.config.max_budget for s in states.values())
    running = sum(1 for s in states.values() if s.status == "running")
    done = sum(1 for s in states.values() if s.status == "done")

    print("=" * 64)
    print(f"  BTC PREDICTOR — ORCHESTRATOR DASHBOARD")
    print(f"  {done}/6 done | {running} running | ${total_cost:.2f}/${total_budget:.2f} | {format_elapsed(elapsed)}")
    print("=" * 64)

    for cid, state in states.items():
        icon = status_icon(state.status)
        phase = f"[Phase {state.config.phase}]"
        cost = f"${state.cost_usd:.2f}/{state.config.max_budget:.2f}"
        msg = state.last_message[:40] if state.last_message else ""
        hb = ""
        if state.status == "running":
            since_output = time.time() - state.last_output_time
            if since_output > HEARTBEAT_TIMEOUT_SEC:
                hb = " ⚠️ NO OUTPUT"
        print(f"  {icon} {state.config.name:<16} {phase} {cost:>12}  {msg} {hb}")

    if alerts:
        print("-" * 64)
        for a in alerts[-5:]:
            print(f"  💡 {a}")
    print("-" * 64)
    print("  Ctrl+C to stop all clones")

def render_dashboard_rich(console: "Console", states: dict, start_time: float, alerts: list):
    """Rich-based dashboard."""
    elapsed = time.time() - start_time
    total_cost = sum(s.cost_usd for s in states.values())
    total_budget = sum(s.config.max_budget for s in states.values())
    running = sum(1 for s in states.values() if s.status == "running")
    done = sum(1 for s in states.values() if s.status == "done")

    table = Table(title=f"BTC PREDICTOR — ORCHESTRATOR  |  {done}/6 done  |  {running} running  |  ${total_cost:.2f}/${total_budget:.2f}  |  {format_elapsed(elapsed)}")
    table.add_column("Clone", style="bold cyan", width=16)
    table.add_column("Phase", width=7)
    table.add_column("Status", width=8)
    table.add_column("Budget", width=14)
    table.add_column("Last Activity", width=40)
    table.add_column("HB", width=4)

    for cid, state in states.items():
        icon = status_icon(state.status)
        phase = state.config.phase
        cost = f"${state.cost_usd:.2f}/{state.config.max_budget:.2f}"
        msg = (state.last_message[:38] + "..") if len(state.last_message) > 40 else state.last_message
        hb = ""
        if state.status == "running":
            since = time.time() - state.last_output_time
            if since > HEARTBEAT_TIMEOUT_SEC:
                hb = "⚠️"
            elif since > 120:
                hb = "🟡"
            else:
                hb = "🟢"
        elif state.status == "done":
            hb = "✅"

        table.add_row(f"{icon} {state.config.name}", phase, state.status, cost, msg, hb)

    alert_text = "\n".join(f"  💡 {a}" for a in alerts[-5:]) if alerts else "  No alerts"

    return Panel.fit(
        table,
        subtitle=alert_text,
        border_style="bright_blue",
    )

# ---------------------------------------------------------------------------
# Clone Launcher
# ---------------------------------------------------------------------------

def launch_clone(state: CloneState, dry_run: bool = False):
    """Launch a single claude -p process and stream its output."""
    prompt_path = PROMPTS_DIR / state.config.prompt_file
    if not prompt_path.exists():
        state.status = "error"
        state.error = f"Prompt file not found: {prompt_path}"
        return

    prompt_text = prompt_path.read_text(encoding="utf-8")

    if dry_run:
        state.status = "done"
        state.last_message = "[DRY RUN] Would launch with prompt"
        state.result_text = f"DRY RUN — prompt: {len(prompt_text)} chars"
        return

    cmd = [
        "claude", "-p", prompt_text,
        "--output-format", "stream-json",
        "--verbose",
        "--model", state.config.model,
        "--max-turns", str(state.config.max_turns),
        "--max-budget-usd", f"{state.config.max_budget:.2f}",
        "--allowedTools", state.config.allowed_tools,
    ]

    state.status = "running"
    state.start_time = time.time()
    state.last_output_time = time.time()
    _push_cockpit_state(state)  # initial push

    try:
        # Clean env: remove CLAUDECODE marker so nested `claude -p` doesn't refuse to start
        clean_env = {k: v for k, v in os.environ.items() if "CLAUDE" not in k.upper()}
        clean_env["HOME"] = os.environ.get("HOME", "")
        clean_env["PATH"] = os.environ.get("PATH", "")
        clean_env["SHELL"] = os.environ.get("SHELL", "/bin/zsh")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(REPO_DIR),
            text=True,
            bufsize=1,
            env=clean_env,
        )
        state.process = proc

        # Stream stdout line by line (stream-json = one JSON per line)
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            state.last_output_time = time.time()
            state.output_lines.append(line)

            # Parse stream-json events
            try:
                event = json.loads(line)
                etype = event.get("type", "")

                if etype == "assistant" and "message" in event:
                    # Extract text content
                    msg = event.get("message", {})
                    content = msg.get("content", [])
                    for block in content:
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                state.last_message = text[:80]
                                # Detect C2 DDL ready
                                if "DDL PRONTO" in text:
                                    state.ddl_ready = True
                                # Push to cockpit every significant update
                                _push_cockpit_state(state)

                elif etype == "result":
                    # Final result — contains session_id, cost, etc.
                    state.session_id = event.get("session_id", "")
                    state.cost_usd = event.get("cost_usd", event.get("total_cost_usd", 0.0))
                    state.result_text = event.get("result", "")
                    # Try to extract cost from nested structure
                    if state.cost_usd == 0 and "usage" in event:
                        usage = event["usage"]
                        # Rough estimate: $15/M input, $75/M output for Opus
                        inp = usage.get("input_tokens", 0)
                        out = usage.get("output_tokens", 0)
                        if "opus" in state.config.model:
                            state.cost_usd = (inp * 15 + out * 75) / 1_000_000
                        else:
                            state.cost_usd = (inp * 3 + out * 15) / 1_000_000

                elif etype == "error":
                    state.error = event.get("error", {}).get("message", str(event))

            except json.JSONDecodeError:
                # Not JSON — raw text output
                if line:
                    state.last_message = line[:80]

        proc.wait()
        state.end_time = time.time()

        if proc.returncode == 0:
            state.status = "done"
            _push_cockpit_state(state)  # final push
        else:
            stderr = proc.stderr.read() if proc.stderr else ""
            state.status = "error"
            state.error = stderr[:200] if stderr else f"Exit code {proc.returncode}"
            _push_cockpit_state(state)

    except FileNotFoundError:
        state.status = "error"
        state.error = "claude CLI not found. Install: npm install -g @anthropic-ai/claude-code"
        _push_cockpit_state(state)
    except Exception as e:
        state.status = "error"
        state.error = str(e)[:200]
        _push_cockpit_state(state)

# ---------------------------------------------------------------------------
# Result Saver
# ---------------------------------------------------------------------------

def save_results(states: dict):
    """Save results to JSON files and optionally to iCloud."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "clones": {},
        "total_cost_usd": 0.0,
    }

    for cid, state in states.items():
        result = {
            "clone_id": cid,
            "name": state.config.name,
            "role": state.config.role,
            "model": state.config.model,
            "phase": state.config.phase,
            "status": state.status,
            "session_id": state.session_id,
            "cost_usd": state.cost_usd,
            "elapsed_sec": (state.end_time - state.start_time) if state.end_time else 0,
            "last_message": state.last_message,
            "result_text": state.result_text[:2000],
            "error": state.error,
            "ddl_ready": state.ddl_ready,
        }

        # Save individual result
        result_path = RESULTS_DIR / f"{cid}_result.json"
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

        summary["clones"][cid] = result
        summary["total_cost_usd"] += state.cost_usd

    # Save integration report
    report_path = RESULTS_DIR / "integration_report.json"
    report_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    # Copy to iCloud if available
    if ICLOUD_DIR.exists():
        icloud_report = ICLOUD_DIR / f"orchestrator_report_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
        icloud_report.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    return summary

# ---------------------------------------------------------------------------
# Main Orchestrator
# ---------------------------------------------------------------------------

def run_phase(phase: str, states: dict, alerts: list, dry_run: bool, start_time: float):
    """Launch all clones in a phase and monitor them."""
    phase_clones = {cid: s for cid, s in states.items() if s.config.phase == phase}
    threads = []

    for cid, state in phase_clones.items():
        t = threading.Thread(target=launch_clone, args=(state, dry_run), name=f"clone-{cid}", daemon=True)
        threads.append(t)
        t.start()
        alerts.append(f"{state.config.name} launched ({state.config.model})")

    # Monitor loop
    if HAS_RICH:
        console = Console()
        with Live(render_dashboard_rich(console, states, start_time, alerts),
                  console=console, refresh_per_second=1, transient=True) as live:
            while any(t.is_alive() for t in threads):
                # Check heartbeats
                for cid, state in phase_clones.items():
                    if state.status == "running":
                        since = time.time() - state.last_output_time
                        if since > HEARTBEAT_TIMEOUT_SEC:
                            hb_alert = f"⚠️ {state.config.name} — no output for {int(since)}s"
                            if hb_alert not in alerts:
                                alerts.append(hb_alert)

                # Check DDL ready (C2)
                c2_state = states.get("c2")
                if c2_state and c2_state.ddl_ready:
                    ddl_alert = "🔔 C2 DDL PRONTO — esegui scripts/go_live_ddl.sql su Supabase prima del push!"
                    if ddl_alert not in alerts:
                        alerts.append(ddl_alert)

                live.update(render_dashboard_rich(console, states, start_time, alerts))
                time.sleep(1)

            # Final render
            live.update(render_dashboard_rich(console, states, start_time, alerts))
    else:
        while any(t.is_alive() for t in threads):
            render_dashboard_plain(states, start_time, alerts)
            time.sleep(3)
        render_dashboard_plain(states, start_time, alerts)

    for t in threads:
        t.join(timeout=5)

def main():
    parser = argparse.ArgumentParser(description="BTC Predictor Bot — Multi-Clone Orchestrator")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually launch clones")
    parser.add_argument("--phase-a-only", action="store_true", help="Only run Phase A (read-heavy)")
    parser.add_argument("--phase-b-only", action="store_true", help="Only run Phase B (write-heavy)")
    parser.add_argument("--clone", type=str, help="Run a single clone (e.g., c1, c3)")
    parser.add_argument("--no-dashboard", action="store_true", help="Minimal output, no dashboard")
    args = parser.parse_args()

    # Fix #7: CWD enforcement
    os.chdir(str(REPO_DIR))

    # Verify prompts exist
    for clone in CLONES:
        prompt_path = PROMPTS_DIR / clone.prompt_file
        if not prompt_path.exists():
            print(f"ERROR: Missing prompt file: {prompt_path}")
            sys.exit(1)

    # Verify claude CLI exists
    try:
        subprocess.run(["claude", "--version"], capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("ERROR: claude CLI not found. Install: npm install -g @anthropic-ai/claude-code")
        sys.exit(1)

    # Initialize states
    states = {}
    for clone in CLONES:
        states[clone.id] = CloneState(config=clone)

    # Filter by --clone if specified
    if args.clone:
        cid = args.clone.lower().replace("c", "c") if not args.clone.startswith("c") else args.clone.lower()
        if cid not in states:
            print(f"ERROR: Unknown clone '{args.clone}'. Available: {', '.join(states.keys())}")
            sys.exit(1)
        states = {cid: states[cid]}

    alerts = []
    start_time = time.time()

    # Pre-launch checks
    print("=" * 64)
    print("  BTC PREDICTOR — ORCHESTRATOR v2 (Hardened)")
    print(f"  Repo: {REPO_DIR}")
    print(f"  Clones: {len(states)}")
    print(f"  Total budget: ${sum(s.config.max_budget for s in states.values()):.2f}")
    print(f"  Dry run: {args.dry_run}")
    print("=" * 64)

    # Check rollback readiness
    rollback_sql = REPO_DIR / "scripts" / "rollback_golive.sql"
    if not rollback_sql.exists():
        alerts.append("⚠️ rollback_golive.sql non trovato — esegui il rollback plan prima!")

    # Check git tag
    try:
        tags = subprocess.run(
            ["git", "tag", "-l", "pre-golive-*"],
            capture_output=True, text=True, cwd=str(REPO_DIR), timeout=5
        )
        if not tags.stdout.strip():
            alerts.append("⚠️ Nessun git tag pre-golive trovato — esegui: git tag pre-golive-6clone-v1")
    except Exception:
        pass

    if alerts:
        print("\n  ALERTS:")
        for a in alerts:
            print(f"    {a}")
        print()

    if not args.dry_run and not args.clone:
        print("  Lancio tra 3 secondi... (Ctrl+C per annullare)")
        try:
            time.sleep(3)
        except KeyboardInterrupt:
            print("\n  Annullato.")
            sys.exit(0)

    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print("\n\n  Stopping all clones...")
        for state in states.values():
            if state.process and state.process.poll() is None:
                state.process.terminate()
        # Save partial results
        save_results(states)
        print("  Partial results saved.")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    # --- PHASE A: Read-heavy clones ---
    if not args.phase_b_only:
        phase_a_clones = {k: v for k, v in states.items() if v.config.phase == "A"}
        if phase_a_clones:
            alerts.append("=== PHASE A START (read-heavy: C3, C4, C5, C6) ===")
            run_phase("A", states, alerts, args.dry_run, start_time)

            phase_a_done = sum(1 for s in phase_a_clones.values() if s.status == "done")
            phase_a_errors = sum(1 for s in phase_a_clones.values() if s.status == "error")
            alerts.append(f"=== PHASE A DONE: {phase_a_done} ok, {phase_a_errors} errors ===")

            if phase_a_errors > 0:
                print(f"\n  ⚠️ Phase A had {phase_a_errors} errors. Review before Phase B.")
                for cid, s in phase_a_clones.items():
                    if s.status == "error":
                        print(f"    {s.config.name}: {s.error}")
                if not args.dry_run:
                    print("\n  Continue to Phase B? (y/n) ", end="")
                    try:
                        answer = input().strip().lower()
                        if answer != "y":
                            save_results(states)
                            print("  Results saved. Phase B skipped.")
                            sys.exit(0)
                    except (EOFError, KeyboardInterrupt):
                        save_results(states)
                        sys.exit(0)

    # --- PHASE B: Write-heavy clones ---
    if not args.phase_a_only:
        phase_b_clones = {k: v for k, v in states.items() if v.config.phase == "B"}
        if phase_b_clones:
            alerts.append("=== PHASE B START (write-heavy: C1, C2) ===")
            run_phase("B", states, alerts, args.dry_run, start_time)

            phase_b_done = sum(1 for s in phase_b_clones.values() if s.status == "done")
            alerts.append(f"=== PHASE B DONE: {phase_b_done} ok ===")

    # --- Save results ---
    summary = save_results(states)

    # --- Final report ---
    elapsed = time.time() - start_time
    print("\n" + "=" * 64)
    print("  ORCHESTRATION COMPLETE")
    print(f"  Elapsed: {format_elapsed(elapsed)}")
    print(f"  Total cost: ${summary['total_cost_usd']:.2f}")
    print("=" * 64)

    for cid, result in summary["clones"].items():
        icon = "✅" if result["status"] == "done" else "❌"
        print(f"  {icon} {result['name']:<18} ${result['cost_usd']:.2f}  {result['status']}")
        if result["error"]:
            print(f"     Error: {result['error'][:60]}")
        if result["session_id"]:
            print(f"     Session: {result['session_id']}")

    # Check DDL
    ddl_path = REPO_DIR / "scripts" / "go_live_ddl.sql"
    if ddl_path.exists():
        print(f"\n  🔔 DDL PRONTO: {ddl_path}")
        print("     → Esegui su Supabase SQL Editor PRIMA del push")

    # Integration test reminder
    print(f"\n  📋 PROSSIMI PASSI:")
    print(f"     1. Revisiona i risultati in scripts/results/")
    print(f"     2. Esegui DDL: scripts/go_live_ddl.sql su Supabase")
    print(f"     3. Integration test: python -m pytest tests/ -v")
    print(f"     4. Security check: python scripts/security_audit.py")
    print(f"     5. git add + commit + push → Railway deploy")
    print(f"\n  📁 Report salvato: scripts/results/integration_report.json")

    if ICLOUD_DIR.exists():
        print(f"  ☁️  Copia iCloud: {ICLOUD_DIR}")

    print()

if __name__ == "__main__":
    main()
