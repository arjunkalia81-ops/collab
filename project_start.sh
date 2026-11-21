#!/usr/bin/env bash
# API in the foreground, for PM2. Host and port come from .env.
ROOT="$(cd "$(dirname "$0")" && pwd)"
HOST=$(grep -E '^HOST=' "$ROOT/.env" | cut -d= -f2 | tr -d '\r')
PORT=$(grep -E '^PORT=' "$ROOT/.env" | cut -d= -f2 | tr -d '\r')
cd "$ROOT/project"
export PYTHONUNBUFFERED=1
exec "$ROOT/venv/bin/uvicorn" app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8001}"
