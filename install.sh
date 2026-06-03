#!/usr/bin/env bash
#
# Repryntt installer (macOS / Linux convenience wrapper).
#
# Calls the cross-platform Python installer (install.py) and then auto-launches
# the in-browser setup wizard. On Windows, run:
#   python install.py
#   python -m repryntt.setup
#
#   ./install.sh
#
# Skip the wizard auto-launch:  REPRYNTT_NO_WIZARD=1 ./install.sh
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "Python 3.10+ not found. Install it, then run:  python install.py"
  exit 1
fi

# 1. Install
"$PY" "$ROOT/install.py" "$@"

# 2. Launch setup wizard (unless suppressed)
if [ "${REPRYNTT_NO_WIZARD:-}" = "1" ]; then
  echo ""
  echo "Wizard skipped. Start it later with:"
  echo "  cd $ROOT && .venv/bin/python -m repryntt.setup"
  exit 0
fi

# Prefer the venv python so we use the installed deps.
VENV_PY="$ROOT/.venv/bin/python"
if [ ! -x "$VENV_PY" ] && [ -x "$ROOT/.venv/Scripts/python.exe" ]; then
  VENV_PY="$ROOT/.venv/Scripts/python.exe"
fi
if [ ! -x "$VENV_PY" ]; then
  VENV_PY="$PY"
fi

echo ""
echo "Launching the in-browser setup wizard -> http://localhost:9090"
echo "  (Add LLM keys + optionally pair this install to repryntt.com)"
echo ""
exec "$VENV_PY" -m repryntt.setup
