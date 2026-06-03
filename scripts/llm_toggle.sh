#!/usr/bin/env bash
# Toggle a local llama.cpp server for development hosts.

set -euo pipefail

DATA_DIR="${REPRYNTT_DATA_DIR:-$HOME/.repryntt}"
MODELS_DIR="${REPRYNTT_MODELS_DIR:-$DATA_DIR/models}"
LLAMA_BIN="${LLAMA_BIN:-$(command -v llama-server || true)}"
MODEL_NAME="${MODEL_NAME:-}"
PORT="${REPRYNTT_LLM_PORT:-8080}"
PID_FILE="${REPRYNTT_LLAMA_PID_FILE:-$DATA_DIR/pids/llama-local.pid}"
LOG_FILE="${REPRYNTT_LLAMA_LOG_FILE:-$DATA_DIR/logs/llama_server.log}"
DISABLED_FLAG="$DATA_DIR/.llm_disabled"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

find_model() {
    if [ -n "$MODEL_NAME" ]; then
        printf '%s\n' "$MODEL_NAME"
        return
    fi
    find "$MODELS_DIR" -type f -name '*.gguf' 2>/dev/null | sort | head -n 1
}

is_running() {
    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE")"
        if kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
        rm -f "$PID_FILE"
    fi
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1
}

start_llm() {
    if is_running; then
        echo -e "${YELLOW}LLM already running${NC}"
        status_llm
        return 0
    fi
    if [ -z "$LLAMA_BIN" ]; then
        echo -e "${RED}llama-server not found. Set LLAMA_BIN or add it to PATH.${NC}" >&2
        return 1
    fi

    model="$(find_model)"
    if [ -z "$model" ] || [ ! -f "$model" ]; then
        echo -e "${RED}No GGUF model found. Set MODEL_NAME=/path/to/model.gguf or place one under $MODELS_DIR.${NC}" >&2
        return 1
    fi

    mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"
    echo -e "${GREEN}Starting local LLM on port $PORT...${NC}"
    echo "  Binary: $LLAMA_BIN"
    echo "  Model:  $model"

    nohup "$LLAMA_BIN" \
        -m "$model" \
        -ngl "${REPRYNTT_LLAMA_GPU_LAYERS:-0}" \
        -c "${REPRYNTT_LLAMA_CTX:-2048}" \
        --host "${REPRYNTT_LLAMA_HOST:-0.0.0.0}" \
        --port "$PORT" \
        --no-warmup \
        >> "$LOG_FILE" 2>&1 &

    echo $! > "$PID_FILE"
    rm -f "$DISABLED_FLAG"
    echo -e "${GREEN}LLM started (PID $!)${NC}"

    echo -n "Waiting for model load..."
    for _ in $(seq 1 30); do
        sleep 2
        if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
            echo -e " ${GREEN}healthy${NC}"
            return 0
        fi
        echo -n "."
    done
    echo -e " ${YELLOW}not yet responding; check $LOG_FILE${NC}"
}

stop_llm() {
    mkdir -p "$DATA_DIR"
    touch "$DISABLED_FLAG"

    if ! is_running; then
        echo -e "${YELLOW}LLM not running (marked disabled)${NC}"
        return 0
    fi

    if [ -f "$PID_FILE" ]; then
        pid="$(cat "$PID_FILE")"
        kill "$pid" 2>/dev/null || true
        sleep 2
        kill -9 "$pid" 2>/dev/null || true
        rm -f "$PID_FILE"
    fi
    echo -e "${RED}LLM stopped${NC}"
}

status_llm() {
    if is_running; then
        pid="unknown"
        [ -f "$PID_FILE" ] && pid="$(cat "$PID_FILE")"
        echo -e "${GREEN}LLM is ON${NC} (port $PORT, PID $pid)"
    else
        echo -e "${YELLOW}LLM is OFF${NC}"
    fi
}

case "${1:-status}" in
    on|start) start_llm ;;
    off|stop) stop_llm ;;
    status) status_llm ;;
    *) echo "Usage: $0 [on|off|status]" >&2; exit 1 ;;
esac
