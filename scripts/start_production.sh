#!/bin/bash
# Repryntt Production Startup Script
# Thin wrapper around: repryntt start
#
# Usage:
#   ./start_production.sh                # start default stack (blockchain off unless enabled)
#   ./start_production.sh --no-trading   # skip trading pipeline
#   ./start_production.sh --no-llm       # skip local LLM
#   ./start_production.sh --no-evolution # skip evolution loop
#   ./start_production.sh --with-blockchain # opt in to blockchain checks
#   ./start_production.sh --help         # show all options
#
# Or use the CLI directly:
#   repryntt start [--no-llm] [--no-trading] [--no-evolution] [--with-blockchain]
#   repryntt stop
#   repryntt services list
#   repryntt services status
#   repryntt services start <name>
#   repryntt services stop <name>

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Ensure repryntt is importable
if ! python -c "import repryntt" 2>/dev/null; then
    echo "Installing repryntt in editable mode..."
    pip install -e "$REPO_DIR" --quiet
fi

exec python -m repryntt start "$@"
