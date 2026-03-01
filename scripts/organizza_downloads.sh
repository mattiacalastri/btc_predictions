#!/bin/bash
# =============================================================
# Organizza ~/Downloads in cartelle per cliente/categoria
# NESSUN FILE VIENE ELIMINATO — solo spostato
# Esegui: bash ~/Desktop/organizza_downloads.sh
# =============================================================
set -euo pipefail
shopt -s nullglob nocaseglob  # case-insensitive + no empty glob

DL="$HOME/Downloads"
moved=0

move_it() {
    local src="$1" dest="$2"
    if [ -e "$src" ] && [ ! -e "$dest/$(basename "$src")" ]; then
        mv "$src" "$dest/" 2>/dev/null && moved=$((moved + 1)) && return 0
    fi
    return 1
}

echo ""
echo "========================================"
echo "  ORGANIZZAZIONE DOWNLOADS"
echo "  $(date '+%d %b %Y — %H:%M')"
echo "========================================"
echo ""

# --- 1. STRUTTURA CARTELLE ---
echo "1. Creo struttura cartelle..."

mkdir -p "$DL/👤 Clienti Astra/🐾 Super Animali"
mkdir -p "$DL/👤 Clienti Astra/🏠 Locanda Tre Vie"
mkdir -p "$DL/👤 Clienti Astra/💇 Revi Hair"
mkdir -p "$DL/👤 Clienti Astra/🏗 Kongline"
mkdir -p "$DL/👤 Clienti Astra/🧘 Hamsa"
mkdir -p "$DL/👤 Clienti Astra/🏋️ Hyperspace"
mkdir -p "$DL/👤 Clienti Astra/⛵ Vela Azzurra"
mkdir -p "$DL/👤 Clienti Astra/📚 Moodle"
mkdir -p "$DL/👤 Clienti Astra/🧴 LVY Cosmetics"
mkdir -p "$DL/👤 Clienti Astra/🔐 Meta Escape Room"
mkdir -p "$DL/👤 Clienti Astra/👗 Stilosophy"
mkdir -p "$DL/👤 Clienti Astra/🏡 Leorato Comfort"
mkdir -p "$DL/👤 Clienti Astra/🏋️ Centro Fitness"
mkdir -p "$DL/👤 Clienti Astra/⚡ Axer"
mkdir -p "$DL/👤 Clienti Astra/🚗 QuiGo"
mkdir -p "$DL/👤 Clienti Astra/🍷 Zardo Wines"
mkdir -p "$DL/👤 Clienti Astra/⏰ TimeGate"
mkdir -p "$DL/👤 Clienti Astra/📈 TraderBuddy"
mkdir -p "$DL/👤 Clienti Astra/🎬 Veronica & Video"
mkdir -p "$DL/👤 Clienti Astra/📁 Altri Clienti"

mkdir -p "$DL/🏢 Astra (interno)"
mkdir -p "$DL/🙋 Personale Mattia"
mkdir -p "$DL/💾 App & Installer"
mkdir -p "$DL/📦 Archivio Vario"

echo "   OK"
echo ""

# --- 2. CLIENTI ASTRA ---
echo "2. Sposto file clienti..."

# Super Animali (149 items)
echo "   🐾 Super Animali..."
for f in "$DL"/*[Ss]uper*[Aa]nimali* "$DL"/*super_animali* "$DL"/*super-animali*; do
    move_it "$f" "$DL/👤 Clienti Astra/🐾 Super Animali" && echo "      $(basename "$f")" || true
done

# Locanda Tre Vie (100 items)
echo "   🏠 Locanda Tre Vie..."
for f in "$DL"/*[Ll]ocanda* "$DL"/*tre*vie* "$DL"/*TRE*VIE*; do
    move_it "$f" "$DL/👤 Clienti Astra/🏠 Locanda Tre Vie" && echo "      $(basename "$f")" || true
done

# Revi Hair (91 items)
echo "   💇 Revi Hair..."
for f in "$DL"/*[Rr]evi* "$DL"/*REVI*; do
    move_it "$f" "$DL/👤 Clienti Astra/💇 Revi Hair" && echo "      $(basename "$f")" || true
done

# Kongline (85 items)
echo "   🏗 Kongline..."
for f in "$DL"/*[Kk]ongline* "$DL"/*KONGLINE*; do
    move_it "$f" "$DL/👤 Clienti Astra/🏗 Kongline" && echo "      $(basename "$f")" || true
done

# Hamsa (79 items)
echo "   🧘 Hamsa..."
for f in "$DL"/*[Hh]amsa* "$DL"/*HAMSA*; do
    move_it "$f" "$DL/👤 Clienti Astra/🧘 Hamsa" && echo "      $(basename "$f")" || true
done

# Hyperspace (75 items)
echo "   🏋️ Hyperspace..."
for f in "$DL"/*[Hh]yperspace* "$DL"/*HYPERSPACE*; do
    move_it "$f" "$DL/👤 Clienti Astra/🏋️ Hyperspace" && echo "      $(basename "$f")" || true
done

# Vela Azzurra (36 items)
echo "   ⛵ Vela Azzurra..."
for f in "$DL"/*[Vv]ela*[Aa]zzurra* "$DL"/*VELA*AZZURRA* "$DL"/*velazzurra* "$DL"/*VELAZZURRA*; do
    move_it "$f" "$DL/👤 Clienti Astra/⛵ Vela Azzurra" && echo "      $(basename "$f")" || true
done

# Moodle (31 items)
echo "   📚 Moodle..."
for f in "$DL"/*[Mm]oodle* "$DL"/*MOODLE*; do
    move_it "$f" "$DL/👤 Clienti Astra/📚 Moodle" && echo "      $(basename "$f")" || true
done

# LVY Cosmetics (26 items)
echo "   🧴 LVY..."
for f in "$DL"/*[Ll][Vv][Yy]* "$DL"/*LVY*; do
    move_it "$f" "$DL/👤 Clienti Astra/🧴 LVY Cosmetics" && echo "      $(basename "$f")" || true
done

# Meta Escape Room (25 items)
echo "   🔐 Meta Escape Room..."
for f in "$DL"/*[Ee]scape* "$DL"/*META*ESCAPE* "$DL"/*meta*escape*; do
    move_it "$f" "$DL/👤 Clienti Astra/🔐 Meta Escape Room" && echo "      $(basename "$f")" || true
done

# Stilosophy (21 items)
echo "   👗 Stilosophy..."
for f in "$DL"/*[Ss]tilosophy* "$DL"/*STILOSOPHY*; do
    move_it "$f" "$DL/👤 Clienti Astra/👗 Stilosophy" && echo "      $(basename "$f")" || true
done

# Leorato Comfort (19 items)
echo "   🏡 Leorato..."
for f in "$DL"/*[Ll]eorato* "$DL"/*LEORATO* "$DL"/*domotica*; do
    move_it "$f" "$DL/👤 Clienti Astra/🏡 Leorato Comfort" && echo "      $(basename "$f")" || true
done

# Centro Fitness (12 items)
echo "   🏋️ Centro Fitness..."
for f in "$DL"/*centro*fitness* "$DL"/*CENTRO*FITNESS*; do
    move_it "$f" "$DL/👤 Clienti Astra/🏋️ Centro Fitness" && echo "      $(basename "$f")" || true
done

# Axer (10 items)
echo "   ⚡ Axer..."
for f in "$DL"/*[Aa]xer* "$DL"/*AXER*; do
    move_it "$f" "$DL/👤 Clienti Astra/⚡ Axer" && echo "      $(basename "$f")" || true
done

# TraderBuddy (10 items)
echo "   📈 TraderBuddy..."
for f in "$DL"/*[Tt]rader*[Bb]uddy* "$DL"/*traderbuddy*; do
    move_it "$f" "$DL/👤 Clienti Astra/📈 TraderBuddy" && echo "      $(basename "$f")" || true
done

# QuiGo (9 items)
echo "   🚗 QuiGo..."
for f in "$DL"/*[Qq]ui[Gg]o* "$DL"/*QuiGo* "$DL"/*QUIGO*; do
    move_it "$f" "$DL/👤 Clienti Astra/🚗 QuiGo" && echo "      $(basename "$f")" || true
done

# TimeGate (9 items)
echo "   ⏰ TimeGate..."
for f in "$DL"/*[Tt]ime*[Gg]ate* "$DL"/*TIMEGATE*; do
    move_it "$f" "$DL/👤 Clienti Astra/⏰ TimeGate" && echo "      $(basename "$f")" || true
done

# Zardo Wines (5 items)
echo "   🍷 Zardo..."
for f in "$DL"/*[Zz]ardo* "$DL"/*ZARDO*; do
    move_it "$f" "$DL/👤 Clienti Astra/🍷 Zardo Wines" && echo "      $(basename "$f")" || true
done

# Veronica / Filippo / Rava / Colivers / Leonardo / FootgolfPark / Due Nani / FXDD
echo "   🎬 Veronica & altri..."
for f in "$DL"/*[Vv]eronica* "$DL"/*[Ff]ilippo*[Ss]ignorelli* "$DL"/*[Rr]ava* "$DL"/*[Cc]olivers* "$DL"/*[Ll]eonardo* "$DL"/*[Ff]ootgolf* "$DL"/*[Dd]ue*[Nn]ani* "$DL"/*DUE*NANI* "$DL"/*FXDD* "$DL"/*fxdd*; do
    move_it "$f" "$DL/👤 Clienti Astra/📁 Altri Clienti" && echo "      $(basename "$f")" || true
done

echo ""

# --- 3. ASTRA INTERNO ---
echo "3. Sposto file Astra (interni)..."
for f in "$DL"/*[Aa]stra* "$DL"/*ASTRA* "$DL"/*discovery*call* "$DL"/*company*profile*; do
    move_it "$f" "$DL/🏢 Astra (interno)" && echo "      $(basename "$f")" || true
done

echo ""

# --- 4. PERSONALE ---
echo "4. Sposto file personali..."
for f in "$DL"/*[Nn]utrizione* "$DL"/*PIANO*NUTRIZIONE* "$DL"/*[Vv]iaggio* "$DL"/*[Vv]aporwave* "$DL"/*[Tt]esi*[Ll]au* "$DL"/*[Tt]elemaco* "$DL"/*CONDIZIONI*TELEMACO* "$DL"/*NDA*; do
    move_it "$f" "$DL/🙋 Personale Mattia" && echo "      $(basename "$f")" || true
done

echo ""

# --- 5. APP & INSTALLER ---
echo "5. Sposto app & installer..."
for f in "$DL"/*.dmg "$DL"/*.pkg; do
    move_it "$f" "$DL/💾 App & Installer" && echo "      $(basename "$f")" || true
done
# .app bundles
for f in "$DL"/*.app "$DL"/Visual\ Studio\ Code.app; do
    move_it "$f" "$DL/💾 App & Installer" && echo "      $(basename "$f")" || true
done

echo ""

# --- 6. WETRANSFER / DRIVE / SWISSTRANSFER non gia' spostati ---
echo "6. Sposto trasferimenti generici..."
for f in "$DL"/wetransfer_* "$DL"/drive-download-* "$DL"/swisstransfer_*; do
    move_it "$f" "$DL/📦 Archivio Vario" && echo "      $(basename "$f")" || true
done

echo ""

# --- RISULTATO ---
echo "========================================"
echo "  FATTO! $moved elementi spostati"
echo "========================================"
echo ""
echo "--- STRUTTURA ---"
echo ""

# Show folder structure with counts
for d in "$DL/👤 Clienti Astra"/*; do
    [ -d "$d" ] || continue
    count=$(ls -1 "$d" 2>/dev/null | wc -l | tr -d ' ')
    name=$(basename "$d")
    [ "$count" -gt 0 ] && printf "  %-35s %3s items\n" "$name" "$count"
done

echo ""
for d in "$DL/🏢 Astra (interno)" "$DL/🙋 Personale Mattia" "$DL/💾 App & Installer" "$DL/📦 Archivio Vario"; do
    [ -d "$d" ] || continue
    count=$(ls -1 "$d" 2>/dev/null | wc -l | tr -d ' ')
    name=$(basename "$d")
    printf "  %-35s %3s items\n" "$name" "$count"
done

echo ""
remaining=$(ls -1 "$DL" | grep -v "^👤\|^🏢\|^🙋\|^💾\|^📦" | wc -l | tr -d ' ')
echo "  File ancora sciolti: $remaining"
echo ""
