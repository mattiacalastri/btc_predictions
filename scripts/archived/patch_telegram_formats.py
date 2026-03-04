#!/usr/bin/env python3
"""
Patch wf02 (Trade Checker) + wf15 (Daily Report) Telegram message formats.
Chirurgico: modifica SOLO i nodi di formatting + appendAttribution.
"""
import os
import json
import requests
import certifi

N8N_HOST = os.environ.get("N8N_HOST", "")
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")

if not N8N_HOST or not N8N_API_KEY:
    print("ERROR: N8N_HOST and N8N_API_KEY must be set")
    import sys; sys.exit(1)

if not N8N_HOST.startswith("http"):
    N8N_HOST = f"https://{N8N_HOST}"

HEADERS = {"X-N8N-API-KEY": N8N_API_KEY, "Content-Type": "application/json"}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NEW TRADE CLOSE FORMAT (wf02 — "Channel Result Format")
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRADE_CLOSE_JS = r"""const r = $json;
const pnl = Number(r.pnl_usd ?? 0);
const isWin = r.correct === true;
const isLoss = r.correct === false;
const direction = (r.direction || '').toUpperCase();
const betId = r.id ? '#' + r.id : '';

const entryRaw = r.entry_fill_price ?? r.entry_price_used;
const exitRaw = r.btc_price_exit ?? r.exit_fill_price;
const entryP = entryRaw ? '$' + Number(entryRaw).toLocaleString('en', {maximumFractionDigits: 0}) : '—';
const exitP = exitRaw ? '$' + Number(exitRaw).toLocaleString('en', {maximumFractionDigits: 0}) : '—';

const result = isWin ? '✅' : isLoss ? '❌' : '⏳';
const pnlEmoji = pnl >= 0 ? '📈' : '📉';
const pnlSign = pnl >= 0 ? '+' : '';
const pnlStr = `${pnlSign}$${Math.abs(pnl).toFixed(2)}`;

// Duration from prediction row
let durStr = '';
try {
  const pred = $('Get Prediction Row').first().json;
  if (pred.created_at) {
    const mins = Math.round((Date.now() - new Date(pred.created_at).getTime()) / 60000);
    if (mins > 0) durStr = ` · ${mins}min`;
  }
} catch(e) {}

// Close reason — subtle inline
const closeMap = {
  stop_loss: ' · SL',
  take_profit: ' · TP',
  timeout: '',
  manual_close: ' · manual',
  normal: '',
  dry_timeout: '',
};
const closeSuffix = closeMap[r.close_reason] ?? '';

const txHash = r.onchain_resolve_tx ?? r.onchain_commit_tx ?? null;

const lines = [
  `<b>${betId} ${direction} ${result}</b>`,
  '',
  `${entryP} → ${exitP}`,
  `${pnlEmoji} ${pnlStr}${durStr}${closeSuffix}`,
];

if (txHash) {
  lines.push(`⛓ <a href="https://polygonscan.com/tx/${txHash.startsWith('0x') ? txHash : '0x' + txHash}">On-chain ✓</a>`);
}

lines.push('', '🔗 btcpredictor.io/dashboard');

const text = lines.join('\n');
return [{ json: { text, chatId: '-1003762450968' } }];"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# NEW DAILY REPORT FORMAT (wf15 — "Format Report")
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DAILY_REPORT_JS = r"""const stats  = $('Fetch Trading Stats').first().json;
const health = $('Fetch Health').first().json;
const sigs   = $('Fetch Signals').first().json;
const pos    = $('Fetch Position').first().json;

const d = stats.data || stats;

// ── Date ─────────────────────────────────────────────────
const now = new Date();
const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const dateStr = `${now.getUTCDate()} ${months[now.getUTCMonth()]} ${now.getUTCFullYear()}`;

// ── Performance ──────────────────────────────────────────
const totalPnl   = parseFloat(d.total_pnl_usd ?? d.total_pnl ?? 0);
const wr         = parseFloat(d.win_rate_pct ?? d.win_rate ?? 0);
const totalBets  = parseInt(d.total_bets ?? 0);
const wins       = parseInt(d.wins ?? 0);
const losses     = parseInt(d.losses ?? 0);
const equity     = parseFloat(health.wallet_equity || health.equity) || 100;
const worstTrade = parseFloat(d.worst_trade ?? 0);
const bestTrade  = parseFloat(d.best_trade ?? 0);
const roi        = ((equity - 100) / 100 * 100).toFixed(1);

// ── System ───────────────────────────────────────────────
const botStr  = health.bot_paused ? 'standby' : 'live';
const confStr = ((health.confidence_threshold ?? 0.56) * 100).toFixed(0) + '%';
const dbStr   = health.supabase_ok !== false ? '✅' : '❌';
const cleanBets = parseInt(health.xgb_clean_bets ?? 0);
const minBets   = parseInt(health.xgb_min_bets ?? 100);
const xgbStr    = cleanBets >= minBets ? 'attivo' : `in training (${cleanBets}/${minBets})`;

// ── Position ─────────────────────────────────────────────
let posLine = 'Nessuna posizione attiva';
const openList = pos.open_positions ?? (pos.side ? [pos] : []);
if (openList.length > 0) {
  const p = openList[0];
  const side = (p.side || '').toLowerCase();
  posLine = `${side === 'long' ? '🟢 LONG' : '🔴 SHORT'} @ $${parseFloat(p.entry_price ?? 0).toFixed(0)}`;
}

// ── Last 5 signals ───────────────────────────────────────
const sigList = Array.isArray(sigs) ? sigs : (sigs.signals ?? sigs.data ?? []);
let sigBlock = '—';
if (sigList.length > 0) {
  sigBlock = sigList.slice(0, 5).map(s => {
    const dir     = (s.classification ?? s.direction ?? 'UP') === 'UP' ? '⬆' : '⬇';
    const outcome = s.correct === true ? '✅' : s.correct === false ? '❌' : '⏳';
    const conf    = s.confidence ? `${(parseFloat(s.confidence) * 100).toFixed(0)}%` : '?';
    return `${dir} ${conf} ${outcome}`;
  }).join(' · ');
}

// ── Assemble ─────────────────────────────────────────────
const pnlSign = totalPnl >= 0 ? '+' : '';

const message = [
  `🐙 <b>DAILY REPORT</b>`,
  `${dateStr} · 09:00 UTC`,
  '',
  `💰 Equity $${equity.toFixed(2)} · ROI ${roi}%`,
  `PnL totale ${pnlSign}$${totalPnl.toFixed(2)}`,
  `Win Rate ${wr.toFixed(0)}% (${wins}W / ${losses}L su ${totalBets})`,
  `Best +$${bestTrade.toFixed(2)} · Worst -$${Math.abs(worstTrade).toFixed(2)}`,
  '',
  `📍 ${posLine}`,
  '',
  `🔮 Ultimi 5`,
  sigBlock,
  '',
  `⚙️ Bot ${botStr} · DB ${dbStr} · Soglia ${confStr}`,
  `Modello ${xgbStr}`,
  '',
  `🔗 btcpredictor.io/dashboard`,
  `🔍 On-chain: polygonscan.com/address/0xe4661...833a55`,
].join('\n');

return [{ json: { message } }];"""


def fetch_workflow(wf_id):
    r = requests.get(
        f"{N8N_HOST}/api/v1/workflows/{wf_id}",
        headers=HEADERS,
        verify=certifi.where(),
    )
    r.raise_for_status()
    return r.json()


def update_workflow(wf_id, wf_data):
    # n8n PUT requires only these fields — strip everything else
    allowed = {"name", "nodes", "connections", "settings"}
    payload = {k: v for k, v in wf_data.items() if k in allowed}
    # Clean settings — strip fields not accepted by PUT API
    if "settings" in payload and payload["settings"]:
        allowed_settings = {
            "executionOrder", "errorWorkflow", "callerPolicy",
            "saveDataErrorExecution", "saveDataSuccessExecution",
            "saveManualExecutions", "timezone", "executionTimeout",
        }
        payload["settings"] = {
            k: v for k, v in payload["settings"].items()
            if k in allowed_settings
        }
    r = requests.put(
        f"{N8N_HOST}/api/v1/workflows/{wf_id}",
        headers=HEADERS,
        json=payload,
        verify=certifi.where(),
    )
    if r.status_code >= 400:
        print(f"  ⚠️ HTTP {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
    return r.json()


def patch_node_js(nodes, node_name, new_js):
    """Replace jsCode in a specific code node."""
    for n in nodes:
        if n["name"] == node_name:
            old_js = n.get("parameters", {}).get("jsCode", "")
            n["parameters"]["jsCode"] = new_js
            print(f"  ✅ Patched '{node_name}' ({len(old_js)} → {len(new_js)} chars)")
            return True
    print(f"  ❌ Node '{node_name}' not found!")
    return False


def patch_telegram_attribution(nodes, node_name):
    """Set appendAttribution=false on a Telegram node."""
    for n in nodes:
        if n["name"] == node_name:
            af = n.get("parameters", {}).get("additionalFields", {})
            had = af.get("appendAttribution")
            af["appendAttribution"] = False
            n["parameters"]["additionalFields"] = af
            print(f"  ✅ Set appendAttribution=false on '{node_name}' (was: {had})")
            return True
    print(f"  ❌ Node '{node_name}' not found!")
    return False


def main():
    print("=" * 60)
    print("PATCH TELEGRAM FORMATS — chirurgico")
    print("=" * 60)

    # ── wf02: Trade Checker ──────────────────────────────
    print("\n📦 Fetching wf02 (02_BTC_Trade_Checker)...")
    wf02 = fetch_workflow("NnjfpzgdIyleMVBO")
    print(f"  Got {len(wf02['nodes'])} nodes")

    ok1 = patch_node_js(wf02["nodes"], "Channel Result Format", TRADE_CLOSE_JS)

    if ok1:
        print("  Deploying wf02...")
        update_workflow("NnjfpzgdIyleMVBO", wf02)
        print("  ✅ wf02 deployed")
    else:
        print("  ⛔ Skipping wf02 deploy")

    # ── wf15: Daily Report ───────────────────────────────
    print("\n📦 Fetching wf15 (15_BTC_Daily_Report)...")
    wf15 = fetch_workflow("mrymTPqSEQDYYIpj")
    print(f"  Got {len(wf15['nodes'])} nodes")

    ok2 = patch_node_js(wf15["nodes"], "Format Report", DAILY_REPORT_JS)
    ok3 = patch_telegram_attribution(wf15["nodes"], "Send to Channel")
    ok4 = patch_telegram_attribution(wf15["nodes"], "Send to DM")

    if ok2:
        print("  Deploying wf15...")
        update_workflow("mrymTPqSEQDYYIpj", wf15)
        print("  ✅ wf15 deployed")
    else:
        print("  ⛔ Skipping wf15 deploy")

    # ── Summary ──────────────────────────────────────────
    print("\n" + "=" * 60)
    print("RISULTATO:")
    print(f"  wf02 Channel Result Format: {'✅' if ok1 else '❌'}")
    print(f"  wf15 Format Report:         {'✅' if ok2 else '❌'}")
    print(f"  wf15 appendAttribution off: {'✅' if ok3 and ok4 else '❌'}")
    print("=" * 60)


if __name__ == "__main__":
    main()
