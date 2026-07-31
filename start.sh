#!/usr/bin/env sh
# Production API entrypoint — does NOT start the RQ worker.
# Run worker and scheduler as separate services.
set -eu

PORT="${PORT:-8000}"
KEEPALIVE="${KEEP_ALIVE_SECONDS:-75}"
GRACEFUL="${GRACEFUL_SHUTDOWN_SECONDS:-30}"
WORKERS="${WEB_CONCURRENCY:-1}"

# WebSockets: prefer a single worker unless sticky sessions are configured.
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "$PORT" \
  --workers "$WORKERS" \
  --timeout-keep-alive "$KEEPALIVE" \
  --timeout-graceful-shutdown "$GRACEFUL" \
  --proxy-headers \
  --forwarded-allow-ips='*'
