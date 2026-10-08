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

# --- Combined mode: workers (background) + API, supervise all ----------------
# WORKER_PROCESSES workers drain the queues in parallel — an AI reply mostly waits on
# OpenAI, so one worker manages only ~6 replies a minute. If ANY process exits, take
# the rest down and exit non-zero so Render restarts the whole container — a dead
# worker must never linger silently.
WORKER_PROCESSES="${WORKER_PROCESSES:-3}"
PIDS=""
i=0
while [ "$i" -lt "$WORKER_PROCESSES" ]; do
  python worker.py &
  PIDS="$PIDS $!"
  i=$((i + 1))
done

uvicorn app.main:app \
  --host 0.0.0.0 --port "$PORT" --workers "$WORKERS" \
  --timeout-keep-alive "$KEEPALIVE" --timeout-graceful-shutdown "$GRACEFUL" \
  --proxy-headers --forwarded-allow-ips='*' &
PIDS="$PIDS $!"

trap 'kill $PIDS 2>/dev/null; exit 0' INT TERM

all_alive() {
  for pid in $PIDS; do
    kill -0 "$pid" 2>/dev/null || return 1
  done
}
while all_alive; do
  sleep 5
done

kill $PIDS 2>/dev/null
exit 1
