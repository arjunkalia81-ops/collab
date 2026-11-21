#!/usr/bin/env bash
# TherapyNotes worker in the foreground, for PM2. Run one instance only.
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/worker"
export DISPLAY=:99 PYTHONUNBUFFERED=1
exec "$ROOT/venv/bin/python" browser_worker.py
