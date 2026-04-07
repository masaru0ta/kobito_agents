#!/bin/bash

PORT=8200

if lsof -i :$PORT -sTCP:LISTEN -t >/dev/null 2>&1; then
    echo "Server already running on port $PORT"
    open "http://localhost:$PORT"
    exit 0
fi

echo "Starting kobito_agents on port $PORT..."
cd "$(dirname "$0")/src"

FIRST=1

while true; do
    if [ "$FIRST" = "1" ]; then
        FIRST=0
        (sleep 3 && open "http://localhost:$PORT") &
    fi
    echo "[$(date)] Server starting..."
    uvicorn server.app:app --host 0.0.0.0 --port $PORT
    echo "[$(date)] Server exited. Restarting in 2 seconds..."
    sleep 2
done
