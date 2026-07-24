#!/usr/bin/env sh
# Run the API and the RQ worker in one container.
# If EITHER process dies, bring the other down and exit non-zero so Render
# restarts the whole container — a dead worker must never go unnoticed.

python worker.py &
WORKER_PID=$!

uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
API_PID=$!

# Forward Render's shutdown signal to both children.
trap 'kill "$WORKER_PID" "$API_PID" 2>/dev/null; exit 0' INT TERM

# Poll: as soon as one child is gone, take the other down and exit to trigger restart.
while kill -0 "$WORKER_PID" 2>/dev/null && kill -0 "$API_PID" 2>/dev/null; do
  sleep 5
done

kill "$WORKER_PID" "$API_PID" 2>/dev/null
exit 1
