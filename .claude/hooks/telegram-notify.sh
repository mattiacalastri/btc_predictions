#!/bin/bash
# Claude Code → Telegram notification hook
# Fires on Stop event (ogni volta che Claude finisce di rispondere)
#
# Setup: copia .config.example → .config e inserisci le credenziali
# Non committare .config (già in .gitignore)

CONFIG_FILE="$(dirname "$0")/.config"

if [ ! -f "$CONFIG_FILE" ]; then
  exit 0  # silenzio se non configurato
fi

source "$CONFIG_FILE"

if [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ]; then
  exit 0
fi

# Leggi input JSON da stdin
INPUT=$(cat)

# Evita loop infiniti (stop_hook_active = true quando il hook stesso triggera uno stop)
STOP_HOOK_ACTIVE=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('stop_hook_active','false'))" 2>/dev/null)
if [ "$STOP_HOOK_ACTIVE" = "True" ] || [ "$STOP_HOOK_ACTIVE" = "true" ]; then
  exit 0
fi

# Invia notifica
curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  -d "text=🤖 *Claude Code* — task completato%0A%0A📁 btc\_predictions" \
  -d "parse_mode=Markdown" \
  > /dev/null 2>&1

exit 0
