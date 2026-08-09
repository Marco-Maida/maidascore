#!/bin/bash
# Script installazione dipendenze di sistema per MaidaScore
# Testato su Debian 12 (Bookworm). Adattare per altre distribuzioni.

set -e

echo "=== Installazione dipendenze di sistema MaidaScore ==="

# xvfb — virtual framebuffer per MuseScore headless
echo "[1/5] xvfb (virtual framebuffer)..."
sudo apt-get update -qq && sudo apt-get install -y -qq xvfb

# MuseScore 4 — export SVG da .mscz
echo "[2/5] MuseScore 4..."
if ! command -v mscore &> /dev/null; then
    echo "  ATTENZIONE: MuseScore 4 non trovato. Installarlo da https://musescore.org/"
    echo "  Su Debian/Ubuntu: scaricare il .deb da musescore.org e: sudo dpkg -i musescore-*.deb"
else
    echo "  MuseScore trovato: $(mscore --version 2>/dev/null || echo 'versione non disponibile')"
fi

# Font Atkinson Hyperlegible — font accessibile per dislessici
echo "[3/5] Font Atkinson Hyperlegible..."
if ! fc-list | grep -qi "atkinson hyperlegible"; then
    echo "  Scaricando il font..."
    mkdir -p /tmp/atkinson
    wget -q "https://github.com/google/fonts/raw/main/ofl/atkinsonhyperlegible/AtkinsonHyperlegible%5Bopsz%2Cwght%5D.ttf" -O /tmp/atkinson/AtkinsonHyperlegible.ttf 2>/dev/null || \
    echo "  ATTENZIONE: download font fallito. Installare manualmente da https://fonts.google.com/specimen/Atkinson+Hyperlegible"
    if [ -f /tmp/atkinson/AtkinsonHyperlegible.ttf ]; then
        sudo mkdir -p /usr/share/fonts/truetype/atkinson-hyperlegible
        sudo cp /tmp/atkinson/*.ttf /usr/share/fonts/truetype/atkinson-hyperlegible/
        sudo fc-cache -f
        echo "  Font installato."
    fi
else
    echo "  Font già installato."
fi

# Dipendenze Python
echo "[4/5] Dipendenze Python..."
echo "  Nota: su Debian 12 con PEP 668, usare un virtualenv:"
echo "    python3 -m venv venv && source venv/bin/activate"
pip install -r requirements.txt

echo ""
echo "[5/5] Verifica..."
echo "  Comando: python3 generate_maidascore.py input.mscz output_prefix 0"
echo ""
echo "=== Installazione completata ==="
