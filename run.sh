#!/usr/bin/env bash
# Start the Reading-with-Chinese web app.
#   ./run.sh            -> http://127.0.0.1:8000
#   HOST=0.0.0.0 PORT=9000 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

echo "Starting Reading with Chinese on http://${HOST}:${PORT}"
exec python3 -m uvicorn app.server:app --host "$HOST" --port "$PORT" "$@"
