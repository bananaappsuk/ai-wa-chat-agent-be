# AI WhatsApp Chat Agent — Backend

FastAPI + MongoDB + Redis/RQ + Twilio WhatsApp + OpenAI. Serves the React frontend in `../fe`.

## Run locally

Prereqs: Python 3.12+, MongoDB (local or Atlas), Redis (local or hosted), a Twilio WhatsApp number/sandbox, an OpenAI API key.

```bash
cd be
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in the values below
```

Run the API and worker in two terminals:

```bash
# terminal 1 — API
source .venv/bin/activate
uvicorn app.main:app --reload --port 8000

# terminal 2 — RQ worker
source .venv/bin/activate
python worker.py
```

API: http://localhost:8000 · Health: http://localhost:8000/health · Docs: http://localhost:8000/docs

The first user to sign up via the FE automatically becomes `admin`.

## Environment variables

Edit `.env` (see `.env.example`):

| Name | Required | Example |
| --- | --- | --- |
| `MONGO_URI` | yes | `mongodb+srv://user:pass@cluster.mongodb.net` |
| `MONGO_DB` | yes | `ai_wa_chat_agent` |
| `JWT_SECRET` | yes | long random string |
| `JWT_EXPIRE_MIN` | no | `10080` (7 days) |
| `CORS_ORIGINS` | yes | `http://localhost:8080,https://your-fe.vercel.app` |
| `REDIS_URL` | yes | `redis://localhost:6379/0` or your hosted URL |
| `PUBLIC_BASE_URL` | yes | `https://your-be.onrender.com` (for Twilio signature validation) |
| `TWILIO_ACCOUNT_SID` | yes | `ACxxxxxxxx` |
| `TWILIO_AUTH_TOKEN` | yes | from Twilio console |
| `TWILIO_WHATSAPP_FROM` | yes | `whatsapp:+14155238886` (sandbox) or your number |
| `TWILIO_VALIDATE_SIGNATURE` | no | `true` in prod, `false` for local webhook testing |
| `OPENAI_API_KEY` | yes | `sk-...` |
| `OPENAI_MODEL` | no | `gpt-4o-mini` |
| `OPENAI_MAX_HISTORY` | no | `20` |

## Test the WhatsApp webhook locally

Twilio needs a public URL. Easiest path is `ngrok`:

```bash
ngrok http 8000
# then in Twilio Console set the WhatsApp inbound webhook to:
#   https://<your-ngrok>.ngrok-free.app/api/webhook/whatsapp   (POST)
```

Set `TWILIO_VALIDATE_SIGNATURE=false` if signature validation fails locally (ngrok URL mismatch).

## Project layout

```
be/
  app/
    main.py            FastAPI app + lifespan + CORS
    config.py          pydantic-settings
    db/mongo.py        Motor client + indexes
    middleware/auth.py JWT (python-jose) + bcrypt
    models/            pydantic schemas
    services/          twilio, openai, leads, messages, ws_manager
    routes/            auth, profile, leads, messages, agents, campaigns,
                       admin, blacklist, dashboard, webhook, ws
    workers/queue.py   RQ + Redis init
    workers/tasks.py   sync tasks: outbound send, AI reply, blast send
  worker.py            RQ worker entrypoint
  Dockerfile
  requirements.txt
  .env.example
```

## Deploy (Render — Docker)

You need **3 services**:

1. **Web service** — Docker, source = this repo, health check `/health`. Don't set `PORT` (Render injects it; the Dockerfile honors `$PORT`).
2. **Background worker** — same Dockerfile, override start command to `python worker.py`.
3. **Redis (Key Value)** — wire its connection string into `REDIS_URL` on both services above.

Set every required env var on **both** the web service and the worker. Set the Twilio webhook URL to `https://<your-be>.onrender.com/api/webhook/whatsapp`.

> MongoDB Atlas: add `0.0.0.0/0` to the IP allowlist (Render Starter has no static egress).
