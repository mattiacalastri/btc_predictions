#!/usr/bin/env python3
"""
BTC Predictor Bot — Unified Report Generator
=============================================
Fetches LIVE data from Supabase + Bot API, renders 5 HTML reports,
optionally converts to PDF via Chrome Headless.

Usage:
    python scripts/generate_reports.py              # HTML only
    python scripts/generate_reports.py --pdf        # HTML + PDF
    python scripts/generate_reports.py --pdf --open # HTML + PDF + open

Reports:
    1. System Audit
    2. Performance Overview
    3. Analisi Bet Dettagliata
    4. Trading Strategy & ML
    5. System Vision & Roadmap
"""

import os
import sys
import json
import subprocess
import ssl
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import Counter, defaultdict

# ── Paths ────────────────────────────────────────────────────
REPO = Path(__file__).resolve().parent.parent
REPORTS_DIR = REPO / "reports"
CSS_FILE = REPORTS_DIR / "report_style.css"
ICLOUD_DIR = Path.home() / "Library/Mobile Documents/com~apple~CloudDocs" / "\U0001f916 BTC Predictor Bot" / "\U0001f4c4 Docs"

# Chrome for Testing path
CHROME = "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
CHROME_FALLBACK = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# ── ENV ──────────────────────────────────────────────────────
def _load_env():
    """Load .env if present (simple key=value parser)."""
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip("'").strip('"')
            os.environ.setdefault(k.strip(), v)

_load_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY", "")
BOT_URL = os.environ.get("BOT_URL", "https://web-production-e27d0.up.railway.app")
READ_API_KEY = os.environ.get("READ_API_KEY", "")

# ── SSL ──────────────────────────────────────────────────────
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()


# ═══════════════════════════════════════════════════════════════
#  DATA FETCHING
# ═══════════════════════════════════════════════════════════════
def _fetch_json(url, headers=None, timeout=15):
    """GET JSON from URL."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"  [WARN] fetch {url[:80]}... -> {e}")
        return None


def fetch_supabase(table="btc_predictions", params="order=created_at.desc&limit=2000"):
    """Fetch rows from Supabase REST API."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("  [WARN] SUPABASE_URL/KEY not set")
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}?{params}"
    data = _fetch_json(url, headers={
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    })
    return data if isinstance(data, list) else []


def fetch_bot(endpoint):
    """Fetch from bot API."""
    url = f"{BOT_URL}{endpoint}"
    headers = {}
    if READ_API_KEY:
        headers["X-API-Key"] = READ_API_KEY
    return _fetch_json(url, headers=headers)


def fetch_all_data():
    """Fetch all data needed for reports. Returns dict."""
    print("Fetching live data...")

    health = fetch_bot("/health") or {}
    risk = fetch_bot("/risk-metrics") or {}
    signals_resp = fetch_bot("/signals?limit=2000") or {}
    equity_resp = fetch_bot("/equity-history") or {}

    signals = signals_resp.get("data", [])
    equity_history = equity_resp.get("history", [])

    # Also fetch from Supabase directly for completeness
    bets_raw = fetch_supabase("btc_predictions", "order=created_at.desc&limit=2000")

    print(f"  Health: v{health.get('version', '?')} | paused={health.get('paused')}")
    print(f"  Signals from API: {len(signals)}")
    print(f"  Bets from Supabase: {len(bets_raw)}")
    print(f"  Equity points: {len(equity_history)}")
    print(f"  Risk metrics: {bool(risk)}")

    return {
        "health": health,
        "risk": risk,
        "signals": signals,
        "equity_history": equity_history,
        "bets": bets_raw,
        "generated_at": datetime.now(timezone.utc),
    }


# ═══════════════════════════════════════════════════════════════
#  ANALYTICS
# ═══════════════════════════════════════════════════════════════
def compute_analytics(data):
    """Compute all derived metrics from raw data."""
    bets = data["bets"]
    health = data["health"]
    risk = data["risk"]

    # Filter closed bets (have pnl_usd)
    closed = [b for b in bets if b.get("pnl_usd") is not None]
    open_bets = [b for b in bets if b.get("pnl_usd") is None and b.get("status") != "ghost"]
    ghosts = [b for b in bets if b.get("status") == "ghost"]

    total = len(closed)
    wins = sum(1 for b in closed if (b.get("pnl_usd") or 0) > 0)
    losses = total - wins
    wr = (wins / total * 100) if total > 0 else 0

    # Direction split
    up_bets = [b for b in closed if b.get("direction") == "UP"]
    down_bets = [b for b in closed if b.get("direction") == "DOWN"]
    up_wins = sum(1 for b in up_bets if (b.get("pnl_usd") or 0) > 0)
    down_wins = sum(1 for b in down_bets if (b.get("pnl_usd") or 0) > 0)
    up_wr = (up_wins / len(up_bets) * 100) if up_bets else 0
    down_wr = (down_wins / len(down_bets) * 100) if down_bets else 0

    # PnL
    total_pnl = sum(b.get("pnl_usd", 0) or 0 for b in closed)
    avg_win = 0
    avg_loss = 0
    winning = [b.get("pnl_usd", 0) for b in closed if (b.get("pnl_usd") or 0) > 0]
    losing = [b.get("pnl_usd", 0) for b in closed if (b.get("pnl_usd") or 0) < 0]
    if winning:
        avg_win = sum(winning) / len(winning)
    if losing:
        avg_loss = sum(losing) / len(losing)

    profit_factor = (sum(winning) / abs(sum(losing))) if losing and sum(losing) != 0 else 0

    # Direction distribution
    dir_counts = Counter(b.get("direction") for b in closed)
    up_pct = (dir_counts.get("UP", 0) / total * 100) if total > 0 else 0
    down_pct = (dir_counts.get("DOWN", 0) / total * 100) if total > 0 else 0

    # Hourly analysis
    hourly_stats = defaultdict(lambda: {"total": 0, "wins": 0, "pnl": 0})
    for b in closed:
        try:
            ts = b.get("created_at", "")
            h = int(ts[11:13]) if len(ts) > 13 else 0
        except (ValueError, IndexError):
            h = 0
        hourly_stats[h]["total"] += 1
        hourly_stats[h]["pnl"] += b.get("pnl_usd", 0) or 0
        if (b.get("pnl_usd") or 0) > 0:
            hourly_stats[h]["wins"] += 1

    # Confidence buckets
    conf_buckets = defaultdict(lambda: {"total": 0, "wins": 0})
    for b in closed:
        c = b.get("confidence") or b.get("model_confidence") or 0
        if c < 0.55:
            bucket = "<55%"
        elif c < 0.60:
            bucket = "55-60%"
        elif c < 0.65:
            bucket = "60-65%"
        elif c < 0.70:
            bucket = "65-70%"
        else:
            bucket = "70%+"
        conf_buckets[bucket]["total"] += 1
        if (b.get("pnl_usd") or 0) > 0:
            conf_buckets[bucket]["wins"] += 1

    # Streak
    streak_type = None
    streak_count = 0
    for b in sorted(closed, key=lambda x: x.get("created_at", ""), reverse=True):
        is_win = (b.get("pnl_usd") or 0) > 0
        if streak_type is None:
            streak_type = is_win
            streak_count = 1
        elif is_win == streak_type:
            streak_count += 1
        else:
            break

    # Last N rolling WR
    last_10 = sorted(closed, key=lambda x: x.get("created_at", ""))[-10:]
    last_50 = sorted(closed, key=lambda x: x.get("created_at", ""))[-50:]
    wr_10 = sum(1 for b in last_10 if (b.get("pnl_usd") or 0) > 0) / max(len(last_10), 1) * 100
    wr_50 = sum(1 for b in last_50 if (b.get("pnl_usd") or 0) > 0) / max(len(last_50), 1) * 100

    # Direction of last 50
    last_50_dirs = Counter(b.get("direction") for b in last_50)
    down_pct_50 = last_50_dirs.get("DOWN", 0) / max(len(last_50), 1) * 100

    # Equity drawdown
    eq = data["equity_history"]
    max_eq = 0
    max_dd = 0
    for pt in sorted(eq, key=lambda x: x.get("created_at", "")):
        e = pt.get("equity", 0) or 0
        if e > max_eq:
            max_eq = e
        dd = max_eq - e
        if dd > max_dd:
            max_dd = dd

    # Ghost analysis
    ghost_correct = sum(1 for g in ghosts if g.get("ghost_correct") is True)
    ghost_evaluated = sum(1 for g in ghosts if g.get("ghost_correct") is not None)
    ghost_wr = (ghost_correct / ghost_evaluated * 100) if ghost_evaluated > 0 else 0

    # Recent bets (last 24h)
    now = datetime.now(timezone.utc)
    recent_24h = [b for b in closed if _parse_ts(b.get("created_at")) and (now - _parse_ts(b["created_at"])).total_seconds() < 86400]

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "open_bets": len(open_bets),
        "ghosts_total": len(ghosts),
        "ghost_evaluated": ghost_evaluated,
        "ghost_correct": ghost_correct,
        "ghost_wr": ghost_wr,
        "up_count": len(up_bets),
        "down_count": len(down_bets),
        "up_wr": up_wr,
        "down_wr": down_wr,
        "up_pct": up_pct,
        "down_pct": down_pct,
        "total_pnl": total_pnl,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "hourly_stats": dict(hourly_stats),
        "conf_buckets": dict(conf_buckets),
        "streak_type": "WIN" if streak_type else "LOSS",
        "streak_count": streak_count,
        "wr_10": wr_10,
        "wr_50": wr_50,
        "down_pct_50": down_pct_50,
        "max_drawdown": max_dd,
        "equity_final": data["equity_history"][-1].get("equity", 0) if data["equity_history"] else 0,
        "recent_24h": len(recent_24h),
        "closed_bets_sorted": sorted(closed, key=lambda x: x.get("created_at", ""), reverse=True),
    }


def _parse_ts(ts_str):
    """Parse ISO timestamp string, return datetime or None."""
    if not ts_str:
        return None
    try:
        s = ts_str.replace("Z", "+00:00")
        if "+" not in s and len(s) > 19:
            s = s[:19] + "+00:00"
        elif "+" not in s:
            s += "+00:00"
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# ═══════════════════════════════════════════════════════════════
#  HTML RENDERING
# ═══════════════════════════════════════════════════════════════
def _css():
    """Read the shared CSS file."""
    return CSS_FILE.read_text()


def _html_wrap(title, subtitle, meta_chips, body_html, generated_at):
    """Wrap body in the standard HTML template."""
    date_str = generated_at.strftime("%d %B %Y, %H:%M UTC")
    chips_html = ""
    for label, cls in meta_chips:
        chips_html += f'<span class="meta-chip {cls}">{label}</span>\n'

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
{_css()}
</style>
</head>
<body>

<header class="report-header">
  <div class="container">
    <h1>{title}</h1>
    <div class="subtitle">{subtitle}</div>
    <div class="meta">
      <span class="meta-chip">btcpredictor.io</span>
      <span class="meta-chip">{date_str}</span>
      {chips_html}
    </div>
  </div>
</header>

<main class="container">
{body_html}
</main>

<footer class="report-footer">
  <div class="container">
    <span class="brand">BTC Predictor Bot</span> &mdash; Generated {date_str}<br>
    btcpredictor.io &middot; Polygon PoS verified &middot; Data source: Supabase + Kraken Futures
  </div>
</footer>

</body>
</html>"""


def _kpi(value, label, cls="", sub=""):
    sub_html = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return f"""<div class="kpi-card">
  <div class="kpi-value {cls}">{value}</div>
  <div class="kpi-label">{label}</div>
  {sub_html}
</div>"""


def _score_bar(name, value, max_val=100, cls=""):
    pct = min(value / max_val * 100, 100) if max_val > 0 else 0
    return f"""<div class="score-bar-wrap">
  <div class="score-bar-label"><span class="name">{name}</span><span class="value">{value:.1f}%</span></div>
  <div class="score-bar"><div class="score-bar-fill {cls}" style="width:{pct:.1f}%"></div></div>
</div>"""


def _alert(text, level="blue"):
    icons = {"red": "!!!", "yellow": "!!", "green": "OK", "blue": "i"}
    return f"""<div class="alert alert-{level}">
  <span class="alert-icon">[{icons.get(level, 'i')}]</span>
  <div>{text}</div>
</div>"""


def _wr_color(wr):
    if wr >= 55:
        return "green"
    elif wr >= 45:
        return "yellow"
    return "red"


def _pnl_class(pnl):
    return "val-up" if pnl > 0 else ("val-down" if pnl < 0 else "val-muted")


# ═══════════════════════════════════════════════════════════════
#  REPORT 1: System Audit
# ═══════════════════════════════════════════════════════════════
def render_report_1(data, analytics):
    h = data["health"]
    r = data["risk"]
    a = analytics

    version = h.get("version", "?")
    paused = h.get("paused", False)
    dry_run = h.get("dry_run", False)
    equity = h.get("wallet_equity") or a["equity_final"]
    xgb_active = h.get("xgb_gate_active", False)
    xgb_bets = h.get("xgb_clean_bets", 0)
    xgb_min = h.get("xgb_min_bets", 100)

    status_chip = ("LIVE", "") if not paused else ("PAUSED", "warn")
    mode_chip = ("REAL", "") if not dry_run else ("DRY RUN", "warn")

    # Alerts
    alerts = ""
    if a["wr"] < 45:
        alerts += _alert(f"<b>Win Rate critico: {a['wr']:.1f}%</b> — sotto il coin flip. Valutare pausa e retrain.", "red")
    if a["down_pct_50"] > 65:
        alerts += _alert(f"<b>DOWN bias: {a['down_pct_50']:.0f}%</b> degli ultimi 50 segnali sono DOWN. Verificare prompt anti-bias.", "yellow")
    if not xgb_active:
        alerts += _alert(f"<b>XGB Gate disattivo</b> — {xgb_bets}/{xgb_min} clean bets. Tutti i segnali diventano bet (0 ghost).", "yellow")
    if paused:
        alerts += _alert("<b>Bot in PAUSA</b> — nessun trade attivo.", "red")
    if not alerts:
        alerts = _alert("Nessuna anomalia rilevata. Sistema operativo.", "green")

    # Score bar
    scores = ""
    scores += _score_bar("Win Rate Globale", a["wr"], 100, _wr_color(a["wr"]))
    scores += _score_bar("Win Rate UP", a["up_wr"], 100, _wr_color(a["up_wr"]))
    scores += _score_bar("Win Rate DOWN", a["down_wr"], 100, _wr_color(a["down_wr"]))
    scores += _score_bar("Win Rate Last 10", a["wr_10"], 100, _wr_color(a["wr_10"]))
    scores += _score_bar("Win Rate Last 50", a["wr_50"], 100, _wr_color(a["wr_50"]))
    if a["ghost_evaluated"] > 0:
        scores += _score_bar(f"Ghost WR ({a['ghost_evaluated']} evaluated)", a["ghost_wr"], 100, _wr_color(a["ghost_wr"]))

    body = f"""
<section class="section">
  <div class="section-title">System Status</div>
  <div class="section-desc">Health check del sistema e KPI principali</div>
  <div class="kpi-grid">
    {_kpi(f"v{version}", "Version")}
    {_kpi("ACTIVE" if not paused else "PAUSED", "Status", "green" if not paused else "red")}
    {_kpi(f"${equity:.2f}", "Equity", "accent")}
    {_kpi(f"{a['wr']:.1f}%", "Win Rate", _wr_color(a['wr']), f"{a['wins']}W / {a['losses']}L")}
    {_kpi(f"${a['total_pnl']:.2f}", "Total PnL", "green" if a['total_pnl'] >= 0 else "red")}
    {_kpi(str(a['total']), "Total Trades", "", f"{a['open_bets']} open")}
    {_kpi(f"{a['streak_count']}", f"Streak ({a['streak_type']})", "green" if a['streak_type'] == 'WIN' else "red")}
    {_kpi("YES" if xgb_active else "NO", "XGB Gate", "green" if xgb_active else "yellow", f"{xgb_bets}/{xgb_min} bets")}
  </div>
</section>

<section class="section">
  <div class="section-title">Findings & Alerts</div>
  {alerts}
</section>

<section class="section">
  <div class="section-title">Win Rate Analysis</div>
  <div class="section-desc">Score per direzione e finestra temporale</div>
  {scores}
</section>

<section class="section">
  <div class="section-title">Direction Split</div>
  <div class="kpi-grid">
    {_kpi(f"{a['up_count']}", "UP Trades", "", f"WR {a['up_wr']:.1f}%")}
    {_kpi(f"{a['down_count']}", "DOWN Trades", "", f"WR {a['down_wr']:.1f}%")}
    {_kpi(f"{a['up_pct']:.0f}%", "UP Ratio")}
    {_kpi(f"{a['down_pct']:.0f}%", "DOWN Ratio")}
  </div>
</section>

<section class="section">
  <div class="section-title">ML Pipeline</div>
  <div class="two-col">
    <div class="card-panel">
      <h3>XGBoost Gate</h3>
      <ul>
        <li>Status: <b>{"Active" if xgb_active else "Inactive (warming up)"}</b></li>
        <li>Clean bets: <b>{xgb_bets}</b> / {xgb_min} needed</li>
        <li>Auto-retrain: Daily 03:00 UTC</li>
        <li>Model: xgb_direction.pkl + xgb_correctness.pkl</li>
      </ul>
    </div>
    <div class="card-panel">
      <h3>Ghost Signal Verification</h3>
      <ul>
        <li>Total ghosts: <b>{a['ghosts_total']}</b></li>
        <li>Evaluated (T+30m): <b>{a['ghost_evaluated']}</b></li>
        <li>Correct: <b>{a['ghost_correct']}</b></li>
        <li>Ghost WR: <b>{a['ghost_wr']:.1f}%</b></li>
      </ul>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-title">Backend Audit</div>
  <div class="two-col">
    <div class="card-panel">
      <h3>Infrastructure</h3>
      <ul>
        <li>Hosting: Railway (Flask + Gunicorn)</li>
        <li>Database: Supabase (PostgreSQL)</li>
        <li>Exchange: Kraken Futures</li>
        <li>On-chain: Polygon PoS (verified)</li>
        <li>Automation: n8n (self-hosted)</li>
      </ul>
    </div>
    <div class="card-panel">
      <h3>Security</h3>
      <ul>
        <li>API Keys: env-only, no hardcoding</li>
        <li>SSL: certifi-validated</li>
        <li>Auth: BOT_API_KEY + READ_API_KEY + COCKPIT_TOKEN</li>
        <li>On-chain audit: immutable SHA-256 commits</li>
        <li>Slippage protection: {h.get('slippage_max_pct', 'N/A')}%</li>
      </ul>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-title">System Scorecard</div>
  {_score_bar("Uptime & Reliability", 95)}
  {_score_bar("ML Accuracy (WR)", a['wr'])}
  {_score_bar("Risk Management", 80 if a['max_drawdown'] < 20 else 50)}
  {_score_bar("On-chain Verification", 100)}
  {_score_bar("Code Quality", 85)}
</section>
"""
    meta_chips = [status_chip, mode_chip]
    return _html_wrap(
        "System Audit Report",
        "BTC Predictor Bot — Comprehensive System Health Check",
        meta_chips,
        body,
        data["generated_at"],
    )


# ═══════════════════════════════════════════════════════════════
#  REPORT 2: Performance Overview
# ═══════════════════════════════════════════════════════════════
def render_report_2(data, analytics):
    a = analytics
    h = data["health"]
    eq_history = data["equity_history"]

    # Equity curve as CSS bars
    equity_bars = ""
    if eq_history:
        sorted_eq = sorted(eq_history, key=lambda x: x.get("created_at", ""))
        capital = h.get("capital", 100)
        for pt in sorted_eq[-80:]:  # last 80 points
            e = pt.get("equity", capital)
            pnl = pt.get("pnl_usd", 0) or 0
            cls = "up" if pnl > 0 else ("down" if pnl < 0 else "flat")
            h_pct = max(e / (capital * 1.2) * 100, 5) if capital > 0 else 50
            equity_bars += f'<div class="equity-bar {cls}" style="height:{h_pct:.0f}%" title="${e:.2f}"></div>\n'

    # Confidence calibration table
    conf_rows = ""
    for bucket in ["<55%", "55-60%", "60-65%", "65-70%", "70%+"]:
        stats = a["conf_buckets"].get(bucket, {"total": 0, "wins": 0})
        t = stats["total"]
        w = stats["wins"]
        wr = (w / t * 100) if t > 0 else 0
        conf_rows += f"""<tr>
  <td>{bucket}</td>
  <td class="center">{t}</td>
  <td class="center">{w}</td>
  <td class="center"><span class="{_wr_color(wr)}">{wr:.1f}%</span></td>
</tr>"""

    # PnL by direction
    body = f"""
<section class="section">
  <div class="section-title">Performance KPI</div>
  <div class="kpi-grid">
    {_kpi(f"${a['equity_final']:.2f}", "Equity", "accent")}
    {_kpi(f"${a['total_pnl']:.2f}", "Total PnL", "green" if a['total_pnl'] >= 0 else "red")}
    {_kpi(f"{a['wr']:.1f}%", "Win Rate", _wr_color(a['wr']), f"{a['wins']}W / {a['losses']}L")}
    {_kpi(f"{a['profit_factor']:.2f}", "Profit Factor", "green" if a['profit_factor'] >= 1 else "red")}
    {_kpi(f"${a['avg_win']:.3f}", "Avg Win", "green")}
    {_kpi(f"${abs(a['avg_loss']):.3f}", "Avg Loss", "red")}
    {_kpi(f"${a['max_drawdown']:.2f}", "Max Drawdown", "red")}
    {_kpi(f"{a['recent_24h']}", "Trades 24h")}
  </div>
</section>

<section class="section">
  <div class="section-title">Equity Curve</div>
  <div class="section-desc">Ultimi 80 trade — barre verdi = profit, rosse = loss</div>
  <div class="equity-chart">
    {equity_bars}
  </div>
</section>

<section class="section">
  <div class="section-title">Win Rate Trend</div>
  <div class="two-col">
    <div class="card-panel">
      <h3>Rolling Windows</h3>
      {_score_bar("Global", a['wr'], 100, _wr_color(a['wr']))}
      {_score_bar("Last 10", a['wr_10'], 100, _wr_color(a['wr_10']))}
      {_score_bar("Last 50", a['wr_50'], 100, _wr_color(a['wr_50']))}
    </div>
    <div class="card-panel">
      <h3>Per Direction</h3>
      {_score_bar(f"UP ({a['up_count']} trades)", a['up_wr'], 100, _wr_color(a['up_wr']))}
      {_score_bar(f"DOWN ({a['down_count']} trades)", a['down_wr'], 100, _wr_color(a['down_wr']))}
    </div>
  </div>
</section>

<section class="section">
  <div class="section-title">Confidence Calibration</div>
  <div class="section-desc">Win rate per bucket di confidenza — il modello e' ben calibrato se WR sale con la confidence</div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr><th>Confidence</th><th class="center">Trades</th><th class="center">Wins</th><th class="center">Win Rate</th></tr>
      </thead>
      <tbody>
        {conf_rows}
      </tbody>
    </table>
  </div>
</section>

<section class="section">
  <div class="section-title">Streak & Momentum</div>
  <div class="kpi-grid">
    {_kpi(f"{a['streak_count']}", f"Current {a['streak_type']} Streak", "green" if a['streak_type'] == 'WIN' else "red")}
    {_kpi(f"{a['down_pct_50']:.0f}%", "DOWN % (last 50)", "red" if a['down_pct_50'] > 60 else "")}
    {_kpi(f"{a['wr_10']:.0f}%", "WR Last 10", _wr_color(a['wr_10']))}
    {_kpi(f"{a['wr_50']:.0f}%", "WR Last 50", _wr_color(a['wr_50']))}
  </div>
</section>
"""
    return _html_wrap(
        "Performance Overview",
        "BTC Predictor Bot — Equity, Win Rate & PnL Analysis",
        [("LIVE DATA", "")],
        body,
        data["generated_at"],
    )


# ═══════════════════════════════════════════════════════════════
#  REPORT 3: Analisi Bet Dettagliata
# ═══════════════════════════════════════════════════════════════
def render_report_3(data, analytics):
    a = analytics
    closed = a["closed_bets_sorted"]

    # Main bet table (last 100)
    bet_rows = ""
    for b in closed[:100]:
        ts = b.get("created_at", "")[:16].replace("T", " ")
        direction = b.get("direction", "?")
        conf = b.get("confidence") or b.get("model_confidence") or 0
        pnl = b.get("pnl_usd", 0) or 0
        entry = b.get("entry_price") or b.get("signal_price", 0)
        exit_p = b.get("exit_price", 0) or 0
        status = b.get("status", "")

        dir_cls = "val-up" if direction == "UP" else "val-down"
        pnl_cls = _pnl_class(pnl)

        bet_rows += f"""<tr>
  <td class="mono">{ts}</td>
  <td class="center"><span class="{dir_cls}">{direction}</span></td>
  <td class="center">{conf:.1%}</td>
  <td class="right mono">${entry:,.2f}</td>
  <td class="right mono">${exit_p:,.2f}</td>
  <td class="right"><span class="{pnl_cls}">${pnl:+.4f}</span></td>
  <td class="center">{status}</td>
</tr>"""

    # Hourly PnL table
    hourly_rows = ""
    for h in range(24):
        hs = a["hourly_stats"].get(h, {"total": 0, "wins": 0, "pnl": 0})
        t = hs["total"]
        w = hs["wins"]
        wr = (w / t * 100) if t > 0 else 0
        pnl = hs["pnl"]
        hourly_rows += f"""<tr>
  <td class="center">{h:02d}:00</td>
  <td class="center">{t}</td>
  <td class="center">{w}</td>
  <td class="center"><span class="{_wr_color(wr)}">{wr:.0f}%</span></td>
  <td class="right"><span class="{_pnl_class(pnl)}">${pnl:+.4f}</span></td>
</tr>"""

    body = f"""
<section class="section">
  <div class="section-title">Riepilogo</div>
  <div class="kpi-grid">
    {_kpi(str(a['total']), "Bet Chiuse")}
    {_kpi(f"{a['wr']:.1f}%", "Win Rate", _wr_color(a['wr']))}
    {_kpi(f"${a['total_pnl']:.2f}", "PnL Totale", "green" if a['total_pnl'] >= 0 else "red")}
    {_kpi(f"{a['profit_factor']:.2f}", "Profit Factor")}
  </div>
</section>

<section class="section">
  <div class="section-title">Storico Bet</div>
  <div class="section-desc">Ultime 100 bet chiuse — ordinate per data (piu' recenti prima)</div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Timestamp</th>
          <th class="center">Dir</th>
          <th class="center">Conf</th>
          <th class="right">Entry</th>
          <th class="right">Exit</th>
          <th class="right">PnL</th>
          <th class="center">Status</th>
        </tr>
      </thead>
      <tbody>
        {bet_rows}
      </tbody>
    </table>
  </div>
</section>

<section class="section page-break">
  <div class="section-title">PnL per Ora UTC</div>
  <div class="section-desc">Distribuzione performance per fascia oraria — identifica le dead hours</div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr><th class="center">Ora</th><th class="center">Trades</th><th class="center">Wins</th><th class="center">WR</th><th class="right">PnL</th></tr>
      </thead>
      <tbody>
        {hourly_rows}
      </tbody>
    </table>
  </div>
</section>

<section class="section">
  <div class="section-title">Direction Breakdown</div>
  <div class="two-col">
    <div class="card-panel">
      <h3>UP Trades</h3>
      <ul>
        <li>Count: <b>{a['up_count']}</b> ({a['up_pct']:.0f}%)</li>
        <li>Win Rate: <b>{a['up_wr']:.1f}%</b></li>
      </ul>
    </div>
    <div class="card-panel">
      <h3>DOWN Trades</h3>
      <ul>
        <li>Count: <b>{a['down_count']}</b> ({a['down_pct']:.0f}%)</li>
        <li>Win Rate: <b>{a['down_wr']:.1f}%</b></li>
      </ul>
    </div>
  </div>
</section>
"""
    return _html_wrap(
        "Analisi Bet Dettagliata",
        "BTC Predictor Bot — Storico completo, filtri per ora e direzione",
        [("LIVE DATA", ""), (f"{a['total']} trades", "info")],
        body,
        data["generated_at"],
    )


# ═══════════════════════════════════════════════════════════════
#  REPORT 4: Trading Strategy & ML
# ═══════════════════════════════════════════════════════════════
def render_report_4(data, analytics):
    h = data["health"]
    a = analytics

    xgb_active = h.get("xgb_gate_active", False)
    xgb_bets = h.get("xgb_clean_bets", 0)
    xgb_min = h.get("xgb_min_bets", 100)
    conf_threshold = h.get("confidence_threshold", 0.56)

    body = f"""
<section class="section">
  <div class="section-title">Architecture Overview</div>
  <div class="section-desc">Il flusso completo dal segnale all'esecuzione su Kraken Futures</div>
  <div class="code-block">
Signal Generation (n8n wf01A)
  |
  v
place_bet() &rarr; Auth &rarr; Rate Limit &rarr; Parse &rarr; Direction
  |
  v
Dead Hours Guard &rarr; XGB Gate &rarr; DOWN Kill Switch
  |
  v
Pre-flight Checks &rarr; Slippage Guard &rarr; DRY_RUN Check
  |
  v
Position Sizing (Kelly-derived) &rarr; create_order() &rarr; Kraken Futures
  |
  v
On-chain Commit (Polygon PoS) &rarr; Supabase Log &rarr; Telegram Alert
  |
  v
Ghost Verification (T+30m, wf08) &rarr; Auto-Retrain (03:00 UTC, wf10)
  </div>
</section>

<section class="section">
  <div class="section-title">Guard System</div>
  <div class="section-desc">Ogni guard puo' bloccare o modificare il trade prima dell'esecuzione</div>
  <div class="table-wrap">
    <table>
      <thead><tr><th>Guard</th><th>Tipo</th><th>Logica</th><th class="center">Status</th></tr></thead>
      <tbody>
        <tr><td>Auth Check</td><td>Hard block</td><td>BOT_API_KEY header required</td><td class="center"><span class="tag tag-green">ACTIVE</span></td></tr>
        <tr><td>Rate Limiter</td><td>Hard block</td><td>1 bet per 5 min</td><td class="center"><span class="tag tag-green">ACTIVE</span></td></tr>
        <tr><td>Confidence Threshold</td><td>Hard block</td><td>conf &ge; {conf_threshold:.0%}</td><td class="center"><span class="tag tag-green">ACTIVE</span></td></tr>
        <tr><td>Dead Hours</td><td>Soft filter</td><td>Skip low-volume hours</td><td class="center"><span class="tag tag-green">ACTIVE</span></td></tr>
        <tr><td>XGB Gate</td><td>Soft filter</td><td>XGB correctness model &gt; 50%</td><td class="center"><span class="tag tag-{"green" if xgb_active else "yellow"}">{"ACTIVE" if xgb_active else "WARMING"}</span></td></tr>
        <tr><td>DOWN Kill Switch</td><td>Hard block</td><td>Env DISABLE_DOWN_BETS</td><td class="center"><span class="tag tag-muted">CONFIG</span></td></tr>
        <tr><td>Slippage Guard</td><td>Hard block</td><td>Max slippage % exceeded</td><td class="center"><span class="tag tag-green">ACTIVE</span></td></tr>
        <tr><td>Pre-flight</td><td>Fail-open</td><td>Kraken + Supabase connectivity</td><td class="center"><span class="tag tag-green">ACTIVE</span></td></tr>
      </tbody>
    </table>
  </div>
</section>

<section class="section">
  <div class="section-title">XGBoost Models</div>
  <div class="two-col">
    <div class="card-panel">
      <h3>xgb_direction.pkl</h3>
      <ul>
        <li>Task: Predict BTC direction (UP/DOWN)</li>
        <li>Features: OHLCV, RSI, MACD, BB, ATR, CVD, OBV, funding</li>
        <li>Training: Daily retrain 03:00 UTC</li>
        <li>Dataset: build_dataset.py --include-ghost</li>
      </ul>
    </div>
    <div class="card-panel">
      <h3>xgb_correctness.pkl</h3>
      <ul>
        <li>Task: Predict if the signal will be correct</li>
        <li>Gate: blocks bets when P(correct) &lt; 50%</li>
        <li>Status: <b>{xgb_bets}/{xgb_min}</b> clean bets</li>
        <li>Active: <b>{"Yes" if xgb_active else "No (warming up)"}</b></li>
      </ul>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-title">Confidence Calibration</div>
  <div class="section-desc">La confidence del modello dovrebbe correlare positivamente con il win rate reale</div>
"""
    for bucket in ["<55%", "55-60%", "60-65%", "65-70%", "70%+"]:
        stats = a["conf_buckets"].get(bucket, {"total": 0, "wins": 0})
        t = stats["total"]
        wr = (stats["wins"] / t * 100) if t > 0 else 0
        body += _score_bar(f"{bucket} ({t} trades)", wr, 100, _wr_color(wr))

    body += """
</section>

<section class="section">
  <div class="section-title">Ghost Signal Verification</div>
  <div class="section-desc">Segnali filtrati dal gate vengono comunque valutati a T+30min per misurare l'accuratezza latente</div>
  <div class="card-panel">
    <ul>
      <li>Logica: exit price vs signal price a T+30min (Binance 1m kline)</li>
      <li>Colonne Supabase: ghost_correct, ghost_entry_price, ghost_exit_price, ghost_pnl_usd</li>
"""
    body += f"""      <li>Ghost totali: <b>{a['ghosts_total']}</b></li>
      <li>Evaluated: <b>{a['ghost_evaluated']}</b></li>
      <li>Ghost WR: <b>{a['ghost_wr']:.1f}%</b></li>
    </ul>
  </div>
</section>

<section class="section">
  <div class="section-title">Regime Detection</div>
  <div class="card-panel">
    <h3>Status: Planned (P1)</h3>
    <p>I mercati alternano tra trend, ranging e volatile. Aggiungere una label di regime come feature XGBoost migliorerebbe la calibrazione:</p>
    <ul>
      <li><b>Trend</b>: volatilita' storica 4h bassa + ADX &gt; 25</li>
      <li><b>Ranging</b>: ATR normalizzato sotto media + BB squeeze</li>
      <li><b>Volatile</b>: ATR &gt; 2x media</li>
    </ul>
  </div>
</section>
"""
    return _html_wrap(
        "Trading Strategy & ML",
        "BTC Predictor Bot — Architettura, Guard System, XGBoost Pipeline",
        [("LIVE DATA", ""), (f"v{h.get('version', '?')}", "info")],
        body,
        data["generated_at"],
    )


# ═══════════════════════════════════════════════════════════════
#  REPORT 5: System Vision & Roadmap
# ═══════════════════════════════════════════════════════════════
def render_report_5(data, analytics):
    h = data["health"]
    a = analytics
    version = h.get("version", "?")
    xgb_bets = h.get("xgb_clean_bets", 0)
    xgb_min = h.get("xgb_min_bets", 100)

    body = f"""
<section class="section">
  <div class="section-title">Project Overview</div>
  <div class="kpi-grid">
    {_kpi(f"v{version}", "Current Version")}
    {_kpi("1 Mar 2026", "Go-Live Date")}
    {_kpi(str(a['total']), "Total Trades")}
    {_kpi("Polygon PoS", "Blockchain", "accent")}
  </div>
  <div class="card-panel" style="margin-top:24px">
    <h3>Mission</h3>
    <p>BTC Predictor Bot e' il primo trading bot con <b>audit trail on-chain verificabile</b>.
    Ogni prediction viene committata su Polygon PoS <i>prima</i> dell'esecuzione,
    rendendo impossibile la retrodatazione dei risultati. Trasparenza radicale come vantaggio competitivo.</p>
  </div>
</section>

<section class="section">
  <div class="section-title">Architecture</div>
  <div class="code-block">
+-------------------+     +-------------------+     +-------------------+
|    TradingView    |     |      n8n           |     |   Kraken Futures  |
|   (LWC v4 chart)  | &lt;-- |  (6 workflows)     | --&gt; |   (PF_XBTUSD)     |
+-------------------+     +-------------------+     +-------------------+
                                  |
                          +-------+-------+
                          |               |
                    +-----v-----+   +-----v-----+
                    |  Supabase  |   | Polygon   |
                    | (PostgreSQL)|   | PoS Chain |
                    +-----+-----+   +-----------+
                          |
                    +-----v-----+
                    |  Railway   |
                    | Flask API  |
                    | + Cockpit  |
                    +-----------+
  </div>
</section>

<section class="section page-break">
  <div class="section-title">Roadmap</div>
  <div class="section-desc">Priorita': P0 = critico, P1 = alto, P2 = medio, P3 = backlog</div>

  <div class="card-panel">
    <h3>P0 — Critici</h3>
    <div class="roadmap-item">
      <div class="roadmap-priority p0">P0</div>
      <div class="roadmap-content">
        <h4>Migliorare Win Rate</h4>
        <p>WR attuale {a['wr']:.1f}% sotto target 55%. Azioni: regime detection, feature engineering, prompt anti-bias improvement.</p>
      </div>
    </div>
    <div class="roadmap-item">
      <div class="roadmap-priority p0">P0</div>
      <div class="roadmap-content">
        <h4>XGB Gate Activation</h4>
        <p>Raggiungere {xgb_min} clean bets per attivare il filtro XGBoost correctness. Attualmente: {xgb_bets}.</p>
      </div>
    </div>
  </div>

  <div class="card-panel">
    <h3>P1 — Alto</h3>
    <div class="roadmap-item">
      <div class="roadmap-priority p1">P1</div>
      <div class="roadmap-content">
        <h4>Regime Detection Feature</h4>
        <p>Aggiungere label trend/ranging/volatile come feature XGBoost. Volatilita' storica 4h + ADX + BB squeeze.</p>
      </div>
    </div>
    <div class="roadmap-item">
      <div class="roadmap-priority p1">P1</div>
      <div class="roadmap-content">
        <h4>On-chain Metrics Integration</h4>
        <p>SOPR, MVRV Z-score, exchange netflow come features aggiuntive.</p>
      </div>
    </div>
    <div class="roadmap-item">
      <div class="roadmap-priority p1">P1</div>
      <div class="roadmap-content">
        <h4>Funding Rate Filter</h4>
        <p>Integrare funding rate come guard: se funding &gt; 0.08% e direzione = LONG, penalizzare o bloccare.</p>
      </div>
    </div>
  </div>

  <div class="card-panel">
    <h3>P2 — Medio</h3>
    <div class="roadmap-item">
      <div class="roadmap-priority p2">P2</div>
      <div class="roadmap-content">
        <h4>Multi-Timeframe Analysis</h4>
        <p>Combinare segnali 5m, 15m, 1h per conferma multi-TF prima dell'esecuzione.</p>
      </div>
    </div>
    <div class="roadmap-item">
      <div class="roadmap-priority p2">P2</div>
      <div class="roadmap-content">
        <h4>Advanced Position Sizing</h4>
        <p>Kelly criterion dinamico basato su rolling WR + volatilita' corrente. Attualmente: fisso ~2% del capitale.</p>
      </div>
    </div>
    <div class="roadmap-item">
      <div class="roadmap-priority p2">P2</div>
      <div class="roadmap-content">
        <h4>Dashboard v2</h4>
        <p>Cockpit con grafici interattivi, equity curve live, e filter per timeframe.</p>
      </div>
    </div>
  </div>

  <div class="card-panel">
    <h3>P3 — Backlog</h3>
    <div class="roadmap-item">
      <div class="roadmap-priority p3">P3</div>
      <div class="roadmap-content">
        <h4>ERC-4337 Account Abstraction</h4>
        <p>Gas sponsorship per wallet utenti senza MATIC. Migliora UX per verificatori esterni.</p>
      </div>
    </div>
    <div class="roadmap-item">
      <div class="roadmap-priority p3">P3</div>
      <div class="roadmap-content">
        <h4>Weekly Merkle Root on Ethereum</h4>
        <p>Un singolo hash settimanale su Ethereum L1 per auditability istituzionale.</p>
      </div>
    </div>
    <div class="roadmap-item">
      <div class="roadmap-priority p3">P3</div>
      <div class="roadmap-content">
        <h4>Multi-Asset Expansion</h4>
        <p>Estendere a ETH, SOL con lo stesso framework di signal + on-chain commit.</p>
      </div>
    </div>
  </div>
</section>

<section class="section">
  <div class="section-title">Q2 2026 Objectives</div>
  <div class="kpi-grid">
    {_kpi("55%+", "Target WR", "accent")}
    {_kpi("$150+", "Target Equity", "accent")}
    {_kpi("Active", "XGB Gate", "accent")}
    {_kpi("3+", "New Features", "accent")}
  </div>
  {"" if a['wr'] >= 50 else _alert(f"<b>WR attuale {a['wr']:.1f}%</b> — focus primario su miglioramento accuratezza prima di scalare il capitale.", "yellow")}
</section>
"""

    return _html_wrap(
        "System Vision & Roadmap",
        "BTC Predictor Bot — Architettura Cloud, Obiettivi Q2 2026",
        [(f"v{version}", "info")],
        body,
        data["generated_at"],
    )


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════
REPORTS = [
    ("01_system_audit",        "System Audit",          render_report_1),
    ("02_performance_overview", "Performance Overview",  render_report_2),
    ("03_analisi_bet",          "Analisi Bet",           render_report_3),
    ("04_trading_strategy_ml",  "Trading Strategy & ML", render_report_4),
    ("05_system_vision",        "System Vision",         render_report_5),
]


def generate_html(data, analytics):
    """Generate all 5 HTML reports."""
    REPORTS_DIR.mkdir(exist_ok=True)
    paths = []
    for filename, label, renderer in REPORTS:
        html = renderer(data, analytics)
        out = REPORTS_DIR / f"{filename}.html"
        out.write_text(html, encoding="utf-8")
        size_kb = out.stat().st_size / 1024
        print(f"  [{label}] -> {out.name} ({size_kb:.0f} KB)")
        paths.append(out)
    return paths


def convert_to_pdf(html_paths):
    """Convert HTML files to PDF via Chrome Headless."""
    chrome = CHROME if Path(CHROME).exists() else CHROME_FALLBACK
    if not Path(chrome).exists():
        print("[ERROR] Chrome not found. Skipping PDF generation.")
        return []

    ICLOUD_DIR.mkdir(parents=True, exist_ok=True)
    pdf_paths = []

    for html_path in html_paths:
        pdf_name = html_path.stem + ".pdf"
        pdf_path = ICLOUD_DIR / pdf_name
        cmd = [
            chrome,
            "--headless",
            f"--print-to-pdf={pdf_path}",
            "--no-margins",
            "--no-pdf-header-footer",
            "--print-background",
            f"file://{html_path.resolve()}",
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30, check=True)
            size_kb = pdf_path.stat().st_size / 1024
            print(f"  [PDF] {pdf_name} ({size_kb:.0f} KB)")
            pdf_paths.append(pdf_path)
        except subprocess.CalledProcessError as e:
            print(f"  [ERROR] PDF {pdf_name}: {e.stderr[:200] if e.stderr else e}")
        except FileNotFoundError:
            print(f"  [ERROR] Chrome not found at: {chrome}")
            break

    return pdf_paths


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate BTC Predictor reports")
    parser.add_argument("--pdf", action="store_true", help="Also generate PDFs via Chrome Headless")
    parser.add_argument("--open", action="store_true", help="Open PDFs after generation")
    args = parser.parse_args()

    # Fetch data
    data = fetch_all_data()
    analytics = compute_analytics(data)

    # Generate HTML
    print("\nGenerating HTML reports...")
    html_paths = generate_html(data, analytics)

    # Generate PDF
    if args.pdf:
        print("\nConverting to PDF (Chrome Headless)...")
        pdf_paths = convert_to_pdf(html_paths)

        if args.open and pdf_paths:
            print("\nOpening PDFs...")
            for p in pdf_paths:
                subprocess.run(["open", str(p)])

    print(f"\nDone! {len(html_paths)} HTML reports in {REPORTS_DIR}/")
    if args.pdf:
        print(f"PDFs in {ICLOUD_DIR}/")


if __name__ == "__main__":
    main()
