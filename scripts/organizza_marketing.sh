#!/bin/bash
# =============================================================
# Organizza "Marketing & Clienti" su iCloud Drive
# Esegui: bash ~/Desktop/organizza_marketing.sh
# =============================================================
set -euo pipefail
shopt -s nullglob  # glob senza match → array vuoto (no errori)

ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs"

# --- Auto-discover: cerca la cartella Marketing ---
BASE=""
for candidate in \
    "$ICLOUD/Marketing & Clienti" \
    "$ICLOUD/Marketing e Clienti" \
    "$ICLOUD/Marketing" \
    "$ICLOUD/Marketing&Clienti" \
; do
    if [ -d "$candidate" ]; then
        BASE="$candidate"
        break
    fi
done

# Se non trovata, cerca per pattern
if [ -z "$BASE" ]; then
    FOUND=$(find "$ICLOUD" -maxdepth 1 -type d -iname "*marketing*" 2>/dev/null | head -1)
    [ -n "$FOUND" ] && BASE="$FOUND"
fi

if [ -z "$BASE" ]; then
    echo "ERRORE: cartella Marketing non trovata in iCloud Drive."
    echo ""
    echo "Cartelle disponibili in iCloud Drive:"
    ls -1 "$ICLOUD/" 2>/dev/null || echo "  (iCloud Drive non accessibile)"
    echo ""
    echo "Se il nome e' diverso, modificare lo script o passarlo come argomento:"
    echo "  bash $0 '/percorso/completo/della/cartella'"
    exit 1
fi

# Permetti override da argomento
[ -n "${1:-}" ] && BASE="$1"

echo "=== Organizzazione Marketing & Clienti ==="
echo "Cartella: $BASE"
echo ""

# --- Anteprima: mostra cosa c'e' prima ---
echo "--- FILE SCIOLTI ATTUALI ---"
LOOSE_FILES=()
LOOSE_DIRS=()
for f in "$BASE"/*; do
    name=$(basename "$f")
    # Salta le sottocartelle gia' organizzate
    case "$name" in
        "Super Animali"|"Contenuti Digitali"|"Shooting"|"Bozze creative"|"Idee Web Design"|"Reels Mattia Calastri"|"Testimonianze Clienti"|"Eleonora"*) continue ;;
    esac
    if [ -d "$f" ]; then
        LOOSE_DIRS+=("$name")
        echo "  [DIR] $name"
    elif [ -f "$f" ]; then
        LOOSE_FILES+=("$name")
        echo "  [FILE] $name"
    fi
done
echo ""
echo "Totale: ${#LOOSE_FILES[@]} file + ${#LOOSE_DIRS[@]} cartelle da organizzare"
echo ""

# --- 1. Crea le nuove sottocartelle ---
echo "1. Creo sottocartelle..."
mkdir -p "$BASE/Clienti/Proposta & NDA"
mkdir -p "$BASE/Clienti/Landing Page"
mkdir -p "$BASE/Clienti/Deliverable"
mkdir -p "$BASE/Formazione & Ricerca"
mkdir -p "$BASE/Amministrazione"
mkdir -p "$BASE/Personale"
mkdir -p "$BASE/Archivio App"
echo "   OK"

moved=0

# Helper: sposta file/cartella con log
move_it() {
    local src="$1" dest="$2"
    if [ -e "$src" ]; then
        mv "$src" "$dest/" && echo "   -> $(basename "$src")" && moved=$((moved + 1))
    fi
}

# --- 2. Landing Page → Clienti/Landing Page ---
echo "2. Sposto Landing Page..."
for f in "$BASE"/Landing*; do
    move_it "$f" "$BASE/Clienti/Landing Page"
done

# --- 3. Proposte e NDA → Clienti/Proposta & NDA ---
echo "3. Sposto Proposte & NDA..."
for f in "$BASE"/Proposta* "$BASE"/NDA*; do
    move_it "$f" "$BASE/Clienti/Proposta & NDA"
done

# --- 4. Optin / Menu / Sito → Clienti/Deliverable ---
echo "4. Sposto deliverable clienti..."
for f in "$BASE"/Optin* "$BASE"/Menu* "$BASE"/Sito\ Shopif* "$BASE"/Sito\ shopif*; do
    move_it "$f" "$BASE/Clienti/Deliverable"
done

# --- 5. Ricerca / Tesi / Infobusiness / Scopri → Formazione & Ricerca ---
echo "5. Sposto materiale formazione & ricerca..."
for f in "$BASE"/Ricerca* "$BASE"/Tesi* "$BASE"/Infobusine* "$BASE"/Scopri*; do
    move_it "$f" "$BASE/Formazione & Ricerca"
done

# --- 6. INAIL → Amministrazione ---
echo "6. Sposto documenti amministrativi..."
for f in "$BASE"/INAIL*; do
    move_it "$f" "$BASE/Amministrazione"
done

# --- 7. Viaggio / Vaporwave → Personale ---
echo "7. Sposto file personali..."
for f in "$BASE"/Viaggio* "$BASE"/Vaporwave*; do
    move_it "$f" "$BASE/Personale"
done

# --- 8. Registrazioni audio/zoom → Contenuti Digitali ---
echo "8. Sposto registrazioni..."
for f in "$BASE"/Registrazio*; do
    move_it "$f" "$BASE/Contenuti Digitali"
done
[ -d "$BASE/Registrazioni Zoom" ] && move_it "$BASE/Registrazioni Zoom" "$BASE/Contenuti Digitali"

# --- 9. App leftovers → Archivio App ---
echo "9. Sposto residui app..."
for name in Obsidian TextEdit Shortcuts; do
    [ -e "$BASE/$name" ] && move_it "$BASE/$name" "$BASE/Archivio App"
done
for f in "$BASE"/Scrivania*; do
    move_it "$f" "$BASE/Archivio App"
done

# --- 10. Shooting → Shooting ---
echo "10. Sposto shooting..."
for f in "$BASE"/Shooting\ *; do
    # Non spostare la cartella Shooting stessa dentro se stessa
    [ "$(basename "$f")" = "Shooting" ] && continue
    move_it "$f" "$BASE/Shooting"
done

# --- Risultato finale ---
echo ""
echo "=== FATTO! $moved elementi spostati ==="
echo ""
echo "--- STRUTTURA FINALE ---"
ls -1 "$BASE"
echo ""

# Controlla file rimasti sciolti
REMAINING=()
for f in "$BASE"/*; do
    [ -d "$f" ] && continue
    REMAINING+=("$(basename "$f")")
done
if [ ${#REMAINING[@]} -gt 0 ]; then
    echo "File ancora sciolti (da sistemare a mano):"
    for r in "${REMAINING[@]}"; do
        echo "   $r"
    done
else
    echo "Nessun file sciolto rimasto. Tutto organizzato!"
fi
