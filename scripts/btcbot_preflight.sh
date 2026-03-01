#!/bin/bash
# ============================================================
# BTC Prediction Bot — Pre-flight Stress Test
# Eseguire PRIMA del go-live per verificare 100% funzionalità
# ============================================================

BOT_API_KEY="${BOT_API_KEY:?Set BOT_API_KEY env var before running}"
BASE_URL="https://web-production-e27d0.up.railway.app"
N8N_URL="https://n8n.srv1432354.hstgr.cloud"
N8N_KEY=$(python3 -c "import json; d=json.load(open('/Users/mattiacalastri/btc_predictions/.mcp.json')); print(d['mcpServers']['n8n']['env']['N8N_API_KEY'])" 2>/dev/null)

PASS=0; FAIL=0
check() {
  local label="$1"; local result="$2"; local expected="$3"
  if echo "$result" | grep -q "$expected"; then
    echo "  ✅ $label"
    ((PASS++))
  else
    echo "  ❌ $label → got: $(echo $result | cut -c1-80)"
    ((FAIL++))
  fi
}

echo "================================================"
echo " BTC Bot Pre-flight Check — $(date -u '+%Y-%m-%d %H:%M UTC')"
echo "================================================"
echo ""

echo "[ Railway / Flask ]"
H=$(curl -sk "$BASE_URL/health")
check "/health status=ok"           "$H" '"status": "ok"'
check "/health bot_paused=false"    "$H" '"bot_paused": false'
check "/health conf=0.65"           "$H" '"confidence_threshold": 0.65'
check "/health xgb_bypass"         "$H" '"xgb_gate_active": false'
check "/health dry_run=false"       "$H" '"dry_run": false'
check "/health wallet>50"          "$(echo $H | python3 -c 'import json,sys;h=json.load(sys.stdin);print("ok" if h.get("wallet_equity",0)>50 else "fail')" )" "ok"

echo ""
echo "[ Posizioni Kraken ]"
POS=$(curl -sk "$BASE_URL/position" -H "X-API-Key: $BOT_API_KEY")
check "/position raggiungibile"     "$POS" '"status"'
check "/position flat"              "$POS" '"flat"'

echo ""
echo "[ Segnali DB ]"
SIG=$(curl -sk "$BASE_URL/signals?limit=10" -H "X-API-Key: $BOT_API_KEY")
check "/signals raggiungibile"      "$SIG" '{'
DB_COUNT=$(echo "$SIG" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d.get("signals",[])))' 2>/dev/null)
echo "  ℹ️  Segnali Day-0 in DB: $DB_COUNT (tutti SKIP attesi)"

echo ""
echo "[ n8n Workflows ]"
for wf_id in E2LdFbQHKfMTVPOI Fjk7M3cOEcL1aAVf nzMMmMC6Q9eysUBP NnjfpzgdIyleMVBO; do
  WF=$(curl -sk "$N8N_URL/api/v1/workflows/$wf_id" -H "X-N8N-API-KEY: $N8N_KEY")
  NAME=$(echo "$WF" | python3 -c 'import json,sys; w=json.load(sys.stdin); print(w["name"])' 2>/dev/null)
  check "$NAME ACTIVE" "$WF" '"active": true'
done

echo ""
echo "================================================"
if [ $FAIL -eq 0 ]; then
  echo " ✅ ALL SYSTEMS GO — $PASS/$((PASS+FAIL)) checks passed"
  echo " 🚀 Bot pronto per go-live"
else
  echo " ⚠️  $FAIL check falliti su $((PASS+FAIL))"
  echo " 🛑 Risolvere prima del go-live"
fi
echo "================================================"
