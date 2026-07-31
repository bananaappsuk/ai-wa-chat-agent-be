# Release checklist

## Before deploy

- [ ] CI green (backend tests + frontend build)
- [ ] `APP_ENV` / secrets validated for target environment
- [ ] Mongo backup completed (Atlas snapshot or equivalent)
- [ ] Indexes reviewed (`python -m scripts.init_indexes` on a maintenance window if needed)
- [ ] Migrations / backfills identified (none automatic beyond indexes)
- [ ] Twilio webhook URLs prepared for new host
- [ ] Approved template Content SIDs confirmed
- [ ] `PUBLIC_BASE_URL` matches the API host (HTTPS)
- [ ] `CORS_ORIGINS` matches the FE host
- [ ] Worker + scheduler services configured (not only the web dyno)
- [ ] `RUN_INLINE_SCHEDULER=false` in staging/production
- [ ] Pause or drain critical campaigns if cutting over mid-send

## Deploy order

1. Deploy **API**
2. Verify `GET /health` and `GET /ready`
3. Deploy **worker**
4. Deploy **scheduler** (single replica)
5. Deploy **frontend**
6. Point Twilio inbound webhook at the new API URL
7. Smoke tests (below)

## Rollback

1. Pause running campaigns in UI (or stop worker)
2. Redeploy previous API image/build
3. Redeploy previous worker/scheduler
4. Redeploy previous frontend
5. Restore env vars if changed
6. DB rollback only if a data migration was applied (indexes are additive — usually leave in place)

## Post-deploy smoke tests

- [ ] `GET /health` → 200
- [ ] `GET /ready` → 200
- [ ] Login
- [ ] Inbound WhatsApp → Live Chat
- [ ] Manual outbound message
- [ ] Delivery callback updates status
- [ ] AI reply
- [ ] Pause / resume AI
- [ ] Human takeover / handback
- [ ] Media upload + send
- [ ] Approved template send
- [ ] Campaign with one test recipient
- [ ] Campaign analytics update
- [ ] Second tenant cannot see first tenant’s leads (isolation)
