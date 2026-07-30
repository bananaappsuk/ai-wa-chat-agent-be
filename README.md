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

## Stripe billing

Payments-only module: **Checkout (subscription + trial)**, **Customer Portal**, **signed webhooks**, **invoices**. Plan entitlements are defined but **not enforced** yet.

### Environments (`STRIPE_MODE`)

| Mode | When | Keys / prices |
| --- | --- | --- |
| `test` | default for `dev` / `test` / `staging` | `STRIPE_TEST_*` |
| `live` | **required** when `APP_ENV=production` | `STRIPE_LIVE_*` |

- Production **fails startup** if `STRIPE_MODE` is not `live`, or redirect URLs use localhost/tunnels, or live Price IDs are missing.
- Development **rejects** `sk_live_` unless `STRIPE_ALLOW_LIVE_IN_DEV=true`.
- Legacy `STRIPE_SECRET_KEY` / `STRIPE_PRICE_*` still work as fallbacks (deprecated).

Frontend may expose only the **publishable** key (optional for hosted Checkout redirects). `VITE_STRIPE_PUBLISHABLE_KEY` is optional when using hosted Checkout.

### Create Products / Prices

```bash
python -m scripts.setup_stripe_products --mode test --dry-run
python -m scripts.setup_stripe_products --mode test
python -m scripts.setup_stripe_products --mode live --confirm-live
```

Paste printed `STRIPE_TEST_PRICE_*` or `STRIPE_LIVE_PRICE_*` into `.env`. Never commit real IDs into `.env.example`.

### Indexes / integrity

```bash
python -m scripts.report_duplicate_stripe_customers   # before unique indexes on dirty data
python -m scripts.init_indexes
python -m scripts.backfill_billing_fields --dry-run
```

### Local webhooks (CLI secret ≠ Dashboard secret)

```bash
stripe listen --forward-to localhost:8000/api/billing/webhook
```

Put the CLI `whsec_…` into `STRIPE_TEST_WEBHOOK_SECRET` (or legacy `STRIPE_WEBHOOK_SECRET`).

**Production webhook:** `https://<api-host>/api/billing/webhook` with **live** signing secret.

Subscribe at least to:

- `checkout.session.completed`, `checkout.session.expired`
- `customer.subscription.created|updated|deleted|trial_will_end`
- `invoice.finalized`, `invoice.paid`, `invoice.payment_failed`, `invoice.payment_action_required`

### Customer Portal (Dashboard)

Settings → Billing → Customer portal — enable:

- Payment method update
- Invoice history / receipts
- Cancel subscription (prefer **at period end**)
- Switch plans among Starter / Professional / Business products
- Configure proration as desired

### Manual UAT

1. `STRIPE_MODE=test`, restart API + FE.
2. Run Stripe CLI forwarder; use CLI webhook secret.
3. Sign up → Billing → Choose Professional → test card `4242…`.
4. Confirm Mongo user `plan` / `subscription_status` after webhooks.
5. Manage Subscription → change plan / cancel at period end.
6. Trigger failed payment in Stripe test clock / bad card; confirm `past_due` UI warning.

### Key rotation

Rotate secret + webhook secret in Stripe Dashboard, update env, restart API. Never reuse CLI webhook secrets in production. Verify production never has `sk_test_` / `pk_test_`.

### Endpoints

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/billing/plans` | Public catalog + `stripe_mode` |
| GET | `/api/billing/subscription` | Current plan / status (JWT) |
| GET | `/api/billing/invoices` | Payment history (JWT, paginated) |
| POST | `/api/billing/checkout` | `{ "plan": "professional" }` → Checkout URL |
| POST | `/api/billing/portal` | Customer Portal URL |
| POST | `/api/billing/webhook` | Stripe signature required |

Frontend: `/billing` and landing `#pricing`. Admin paid-plan override requires `manual_override=true` and does **not** create Stripe subscriptions.

### Admin plan override

`POST /api/admin/users/{id}/plan` with body `{ "plan": "professional", "manual_override": true }` for Stripe-backed plans. Prefer Checkout/Portal for real billing.

## Minimum environment variables

See `.env.example`. Critical: `MONGO_URI`, `MONGO_DB`, `JWT_SECRET`, `REDIS_URL`, `CORS_ORIGINS`, Twilio vars, `PUBLIC_BASE_URL` (staging/prod), `OPENAI_API_KEY` when AI enabled.

## Docs

- [DEPLOYMENT.md](DEPLOYMENT.md) — staging/production
- [OPERATIONS.md](OPERATIONS.md) — runbooks
- [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md) — release safety
