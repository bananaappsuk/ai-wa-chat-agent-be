# Deployment

## Services (required)

1. **Backend API** — Docker image `CMD start.sh` (or Render web)
2. **RQ worker** — same image, `start-worker.sh` (scale as needed)
3. **Campaign scheduler** — same image, `start-scheduler.sh` (**one** replica)
4. **MongoDB** Atlas (or equivalent)
5. **Redis** with TLS in production (`rediss://`)
6. **Frontend** — Vercel / static host (`ai-wa-chat-agent-fe`)
7. **Media** — local volume or future object storage; `PUBLIC_BASE_URL` must be HTTPS and reachable by Twilio
8. **Sentry** — optional (`SENTRY_DSN`)

## Staging vs production

| Concern | Staging | Production |
| --- | --- | --- |
| `APP_ENV` | `staging` | `production` |
| Mongo DB name | separate DB | production DB |
| Redis | separate instance/DB index | production Redis |
| Twilio | sandbox or test sender | production sender |
| `PUBLIC_BASE_URL` | staging API HTTPS | production API HTTPS |
| `CORS_ORIGINS` | staging FE origin | production FE origin |
| `RUN_INLINE_SCHEDULER` | `false` | `false` |
| `TWILIO_VALIDATE_SIGNATURE` | `true` | `true` |

Never share production credentials or customer data with staging.

## Render

Use `render.yaml` (API + worker + scheduler). Set all `sync: false` secrets in the dashboard.

## Docker

```bash
docker build -t ai-wa-api .
docker run --env-file .env -p 8000:8000 ai-wa-api
docker run --env-file .env ai-wa-api sh start-worker.sh
docker run --env-file .env ai-wa-api sh start-scheduler.sh
```

Do not copy `.env` into the image (see `.dockerignore`).

## Twilio webhooks

- Inbound: `https://<PUBLIC_BASE_URL>/api/webhook/whatsapp` (POST)
- Status callbacks are attached automatically from `PUBLIC_BASE_URL` on outbound sends

## Frontend

Build with `VITE_API_URL=https://<api-host>` (no secrets in the FE build). Deploy via Vercel (`vercel.json`) or any static host.

## Process env highlights

- `WEB_CONCURRENCY=1` recommended for WebSockets unless sticky sessions exist
- `KEEP_ALIVE_SECONDS`, `GRACEFUL_SHUTDOWN_SECONDS` for uvicorn
- `METRICS_TOKEN` to protect `/metrics`
- `SENTRY_DSN` optional
