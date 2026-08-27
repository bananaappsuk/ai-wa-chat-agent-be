#!/usr/bin/env sh
# Runs the API and — by default — the RQ worker in the SAME container, so a single
# Render service processes everything (webhook + AI replies + sends + campaigns).
#
# To run API-ONLY (e.g. the render.yaml blueprint with a dedicated worker service),
# set WORKER_IN_API=false on that service.
set -eu

PORT="${PORT:-8000}"
KEEPALIVE="${KEEP_ALIVE_SECONDS:-75}"
GRACEFUL="${GRACEFUL_SHUTDOWN_SECONDS:-30}"
WORKERS="${WEB_CONCURRENCY:-1}"

# --- API-only mode -----------------------------------------------------------
if [ "${WORKER_IN_API:-true}" != "true" ]; then
  exec uvicorn app.main:app \
    --host 0.0.0.0 --port "$PORT" --workers "$WORKERS" \
    --timeout-keep-alive "$KEEPALIVE" --timeout-graceful-shutdown "$GRACEFUL" \
    --proxy-headers --forwarded-allow-ips='*'
fi

# --- Combined mode: worker (background) + API, supervise both ----------------
# If EITHER exits, take the other down and exit non-zero so Render restarts the
# whole container — a dead worker must never linger silently.
python worker.py &
WORKER_PID=$!

uvicorn app.main:app \
  --host 0.0.0.0 --port "$PORT" --workers "$WORKERS" \
  --timeout-keep-alive "$KEEPALIVE" --timeout-graceful-shutdown "$GRACEFUL" \
  --proxy-headers --forwarded-allow-ips='*' &
API_PID=$!

trap 'kill "$WORKER_PID" "$API_PID" 2>/dev/null; exit 0' INT TERM

while kill -0 "$WORKER_PID" 2>/dev/null && kill -0 "$API_PID" 2>/dev/null; do
  sleep 5
done

kill "$WORKER_PID" "$API_PID" 2>/dev/null
exit 1
