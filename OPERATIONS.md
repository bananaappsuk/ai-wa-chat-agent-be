# Operations

## Health

```bash
curl -fsS https://<api>/health   # process up (lightweight)
curl -fsS https://<api>/ready    # Mongo + Redis ping
```

`/ready` returns **503** if a dependency is down.

## Metrics

```bash
curl -fsS -H "X-Metrics-Token: $METRICS_TOKEN" https://<api>/metrics
```

If `METRICS_TOKEN` is empty, `/metrics` is open (only use on private networks).

## Logs

- Staging/production: JSON lines (`LOG_FORMAT=json`)
- Fields include `request_id`, `route`, `status_code`, `user_id` (when set), never passwords/tokens/message bodies
- Correlate via `X-Request-ID` response header

## RQ worker

```bash
# Restart worker service on Render / re-run container with start-worker.sh
```

Failed jobs live in Redis failed registry (TTL `WORKER_FAILURE_TTL`). Inspect with an RQ dashboard or Redis CLI — do not log job args that contain message text.

## Campaign scheduler

Run **one** `scheduler.py` instance. It uses Redis lock `SCHEDULER_LOCK_KEY`. If two run, only the lock holder enqueues due campaigns.

To pause outbound campaigns before maintenance: use the Campaigns UI **Pause** / **Cancel**, or stop the worker temporarily (queued jobs remain in Redis).

## Sentry

Set `SENTRY_DSN`. Empty DSN = disabled. Expected 4xx are filtered. Tags: `service`, request/job context.

## Common failures

| Symptom | Check |
| --- | --- |
| Messages stuck `queued` | Worker running? Redis reachable? Only one stale worker? |
| Inbound missing | Twilio webhook URL + `PUBLIC_BASE_URL` + tunnel |
| Status stays queued | Status callback URL public HTTPS? Signature validation? |
| `/ready` 503 | Mongo/Redis timeouts or credentials |
| Duplicate campaign ticks | Inline scheduler + external scheduler both on — set `RUN_INLINE_SCHEDULER=false` |
| Cold start drops first WhatsApp | Use always-on plan; avoid free-tier sleep |

## Indexes

```bash
python -m scripts.init_indexes
```

Idempotent; also runs on API startup.
