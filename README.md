# AI WhatsApp Chat Agent — Backend

FastAPI + MongoDB + Redis/RQ + Twilio WhatsApp + OpenAI.

Supported: **Python 3.12**. Node frontend lives in `../ai-wa-chat-agent-fe` (Node 20).

## Architecture

| Process | Command | Role |
| --- | --- | --- |
| API | `sh start.sh` / `uvicorn app.main:app` | HTTP + WebSockets |
| Worker | `sh start-worker.sh` / `python worker.py` | RQ jobs (send, AI, campaigns) |
| Scheduler | `sh start-scheduler.sh` / `python scheduler.py` | Due campaigns (Redis lock) |

**Never** run API + worker in the same production process.

## Local startup

```bash
# Optional local Mongo + Redis
docker compose up -d

python -m venv .venv
# Windows: .\.venv\Scripts\activate
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill values

# Terminal 1 — API (dev reload OK locally only)
uvicorn app.main:app --reload --port 8000

# Terminal 2 — Worker
python worker.py

# Terminal 3 — optional; in APP_ENV=dev the API runs an inline scheduler by default
# python scheduler.py
```

Health: `GET /health` · Ready: `GET /ready` · Metrics: `GET /metrics` (token if `METRICS_TOKEN` set)

## Minimum environment variables

See `.env.example`. Critical: `MONGO_URI`, `MONGO_DB`, `JWT_SECRET`, `REDIS_URL`, `CORS_ORIGINS`, Twilio vars, `PUBLIC_BASE_URL` (staging/prod), `OPENAI_API_KEY` when AI enabled.

## Docs

- [DEPLOYMENT.md](DEPLOYMENT.md) — staging/production
- [OPERATIONS.md](OPERATIONS.md) — runbooks
- [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) — release safety
