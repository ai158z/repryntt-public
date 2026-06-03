#!/bin/bash
# Nexus AI Social Network Watchdog
# Monitors port 8089 and auto-restarts if the Flask server dies
# Usage: nohup ./nexus_watchdog.sh &

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="$SCRIPT_DIR/logs/nexus_watchdog.log"
APP_SCRIPT="$SCRIPT_DIR/ai_social_network/app.py"
APP_LOG="$SCRIPT_DIR/logs/nexus_social.log"
CHECK_INTERVAL=30  # seconds between health checks
MAX_FAILURES=3     # consecutive failures before restart
PORT=8089

failure_count=0
restart_count=0

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

restart_nexus() {
    log "RESTART: Attempting restart #$((restart_count + 1))..."
    
    # Kill any stale process on the port
    STALE_PID=$(lsof -ti :$PORT 2>/dev/null)
    if [ -n "$STALE_PID" ]; then
        log "RESTART: Killing stale PID $STALE_PID on port $PORT"
        kill "$STALE_PID" 2>/dev/null
        sleep 2
        kill -9 "$STALE_PID" 2>/dev/null
        sleep 1
    fi
    
    # Also kill by process name
    pkill -f "ai_social_network/app.py" 2>/dev/null
    sleep 2
    
    # Start fresh
    cd "$SCRIPT_DIR"
    source ~/saige_venv/bin/activate 2>/dev/null
    nohup python "$APP_SCRIPT" >> "$APP_LOG" 2>&1 &
    NEW_PID=$!
    log "RESTART: Launched new Nexus process PID=$NEW_PID"
    
    # Update PID file if pattern exists
    for pid_file in /tmp/saige_*Nexus*.pid; do
        if [ -f "$pid_file" ]; then
            echo "$NEW_PID" > "$pid_file"
            log "RESTART: Updated PID file $pid_file"
            break
        fi
    done
    
    restart_count=$((restart_count + 1))
    failure_count=0
    
    # Give it time to start
    sleep 8
}

log "WATCHDOG: Started monitoring port $PORT (check every ${CHECK_INTERVAL}s, restart after $MAX_FAILURES failures)"

while true; do
    # Health check via curl
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "http://localhost:$PORT/" 2>/dev/null)
    
    if [ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 500 ] 2>/dev/null; then
        # Healthy
        if [ "$failure_count" -gt 0 ]; then
            log "WATCHDOG: Recovered after $failure_count failure(s) (HTTP $HTTP_CODE)"
        fi
        failure_count=0
    else
        failure_count=$((failure_count + 1))
        log "WATCHDOG: Health check FAILED ($failure_count/$MAX_FAILURES) - HTTP=$HTTP_CODE"
        
        if [ "$failure_count" -ge "$MAX_FAILURES" ]; then
            log "WATCHDOG: $MAX_FAILURES consecutive failures - triggering restart"
            restart_nexus
        fi
    fi
    
    sleep "$CHECK_INTERVAL"
done
