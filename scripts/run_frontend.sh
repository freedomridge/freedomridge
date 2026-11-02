#!/bin/bash
# Auto-restart frontend if it crashes.
# Resolves FRONTEND_DIR relative to this script so the same file works on any
# machine / checkout location.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="${FRONTEND_DIR:-$SCRIPT_DIR/../frontend}"
LOG_FILE="${FRIDGE_FRONTEND_LOG:-/tmp/freedomridge-frontend.log}"

echo "Starting Freedom Ridge frontend with auto-restart..."
echo "Logs: $LOG_FILE"
echo "Press Ctrl+C to stop"

while true; do
    echo "[$(date)] Starting frontend..." | tee -a $LOG_FILE
    cd $FRONTEND_DIR
    npm run dev 2>&1 | tee -a $LOG_FILE
    
    # If we get here, the process crashed
    echo "[$(date)] Frontend crashed! Restarting in 3 seconds..." | tee -a $LOG_FILE
    sleep 3
done
