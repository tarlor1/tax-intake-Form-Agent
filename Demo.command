#!/bin/bash
# ──────────────────────────────────────────────────────────────────
#  Tax Client Intake Automation — One-Click Demo
#
#  Double-click this file in Finder to run the full demo.
#  No terminal knowledge required. Everything is automatic.
# ──────────────────────────────────────────────────────────────────
set -e

# Navigate to the project directory (wherever this file lives)
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

if [[ -t 1 && -n "${TERM:-}" ]]; then
    clear || true
fi
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   Tax Client Intake Automation — Demo                      ║"
echo "║                                                            ║"
echo "║   A browser will open and process 10 sample client emails. ║"
echo "║   Watch the system read, extract, fill, and verify each.   ║"
echo "║                                                            ║"
echo "║   Intake uses local rules; no external AI requests.         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── 1. Check for Python 3.10+ ────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "❌  Python 3 is not installed."
    echo "   Please install it from https://www.python.org/downloads/"
    echo ""
    echo "Press any key to close..."
    read -n1
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info.major)')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info.minor)')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "❌  Python $PY_VERSION found, but 3.10+ is required."
    echo ""
    echo "Press any key to close..."
    read -n1
    exit 1
fi

echo "✓  Python $PY_VERSION"

# ── 2. Create virtual environment if needed ───────────────────────
if [ ! -f ".venv/bin/python" ]; then
    echo ""
    echo "⏳  First run — setting up the environment (this takes ~1 minute)..."
    echo "   Setup may download dependencies and Chromium."
    echo "   Creating virtual environment..."
    python3 -m venv .venv

    echo "   Installing dependencies..."
    .venv/bin/pip install --quiet -r requirements.txt

    echo "   Installing Chromium browser..."
    .venv/bin/playwright install chromium
    echo ""
    echo "✓  Setup complete!"
else
    echo "✓  Environment ready"
fi

echo ""
echo "🚀  Starting demo... A browser window will open shortly."
echo "   (Close this terminal window when you're done.)"
echo ""

# ── 3. Run the pipeline ──────────────────────────────────────────
status=0
.venv/bin/python main.py --offline --slow-mo 600 || status=$?

echo ""
echo "══════════════════════════════════════════════════════════════"
case "$status" in
    0) echo "Demo complete." ;;
    1) echo "Demo complete with records needing attention." ;;
    *) echo "Demo could not complete. See the message above." ;;
esac
if [ "$status" -le 1 ]; then
    echo "See the report path above. Each run has its own folder in $DIR/outputs/runs/."
fi
echo "══════════════════════════════════════════════════════════════"
echo ""
echo "Press any key to close..."
read -r -n1 || true
exit "$status"

