#!/usr/bin/env bash
# Usage: ./start.sh        start worker (8002) and API (8001) in the background
#        ./start.sh stop   stop both
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/venv/bin/python"
LOGS="$ROOT/logs"

stop() {
    pkill -f "[b]rowser_worker.py" || true
    pkill -f "[r]un.py" || true
    sleep 2
}

wait_for() {
    for _ in $(seq 1 30); do
        curl -fs "$1" >/dev/null && return 0
        sleep 1
    done
    return 1
}

if [ "$1" = "stop" ]; then
    stop
    echo "Stopped"
    exit 0
fi

stop
mkdir -p "$LOGS"

cd "$ROOT/worker"
DISPLAY=:99 nohup "$PY" browser_worker.py >"$LOGS/worker.log" 2>&1 &
if wait_for http://127.0.0.1:8002/worker/health; then
    echo "Worker running on 8002"
else
    echo "Worker did not start, see $LOGS/worker.log"
    exit 1
fi

cd "$ROOT/project"
nohup "$PY" run.py >"$LOGS/api.log" 2>&1 &
if wait_for http://127.0.0.1:8001/health; then
    echo "API running on 8001"
else
    echo "API did not start, see $LOGS/api.log"
    exit 1
fi

echo "Logs: tail -f $LOGS/worker.log $LOGS/api.log"
