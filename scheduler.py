"""Standalone campaign due-scheduler with Redis lock (single active instance)."""
from __future__ import annotations

import logging
import os
import signal
import time
import uuid

from app.config import settings
from app.observability.logging_setup import configure_logging
from app.observability.sentry import capture_exception, init_sentry
from app.workers.queue import close_redis, enqueue, get_redis
from app.workers import campaign_tasks

logger = logging.getLogger("app.scheduler")
_STOP = False
_OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _handle_signal(signum, frame):  # noqa: ARG001
    global _STOP
    logger.info("scheduler received signal=%s owner=%s", signum, _OWNER)
    _STOP = True


def _acquire_lock(r) -> bool:
    key = settings.SCHEDULER_LOCK_KEY
    ttl = max(30, int(settings.SCHEDULER_LOCK_TTL_SECONDS))
    # SET NX EX — only one scheduler owns the lock
    return bool(r.set(key, _OWNER, nx=True, ex=ttl))


def _refresh_lock(r) -> bool:
    key = settings.SCHEDULER_LOCK_KEY
    ttl = max(30, int(settings.SCHEDULER_LOCK_TTL_SECONDS))
    # Refresh only if we still own it
    current = r.get(key)
    if current is None:
        return _acquire_lock(r)
    if isinstance(current, bytes):
        current = current.decode("utf-8", errors="ignore")
    if current != _OWNER:
        return False
    r.expire(key, ttl)
    return True


def _release_lock(r) -> None:
    key = settings.SCHEDULER_LOCK_KEY
    try:
        current = r.get(key)
        if isinstance(current, bytes):
            current = current.decode("utf-8", errors="ignore")
        if current == _OWNER:
            r.delete(key)
    except Exception:
        logger.exception("failed to release scheduler lock")


def run_forever() -> None:
    configure_logging()
    init_sentry(service="scheduler")
    settings.validate_for_startup() if settings.is_production_like else None

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    r = get_redis()
    interval = max(15, int(settings.SCHEDULER_INTERVAL_SECONDS))
    logger.info("scheduler starting owner=%s interval=%ss", _OWNER, interval)

    while not _STOP:
        try:
            if not _refresh_lock(r):
                logger.debug("scheduler waiting for lock owner=%s", _OWNER)
                time.sleep(min(10, interval))
                continue
            enqueue(campaign_tasks.process_due_scheduled_campaigns)
            logger.info("enqueued process_due_scheduled_campaigns")
            try:
                from app.services.reconciliation import reconcile_stale_messages

                result = reconcile_stale_messages()
                if result.get("repaired"):
                    logger.info("reconciliation repaired=%s", result.get("repaired"))
            except Exception:
                logger.exception("reconciliation tick failed")
            try:
                from app.observability.metrics import set_queue_depth
                from app.services.throughput import queue_depth

                for qn in (
                    settings.RQ_HIGH_QUEUE_NAME,
                    settings.RQ_DEFAULT_QUEUE_NAME,
                    settings.RQ_BULK_QUEUE_NAME,
                ):
                    set_queue_depth(qn, queue_depth(qn))
            except Exception:
                pass
        except Exception as exc:
            logger.exception("scheduler tick failed")
            capture_exception(exc, service="scheduler")
        # Sleep in small slices so shutdown is responsive
        slept = 0
        while slept < interval and not _STOP:
            time.sleep(1)
            slept += 1
            try:
                if slept % 20 == 0:
                    _refresh_lock(r)
            except Exception:
                pass

    _release_lock(r)
    close_redis()
    logger.info("scheduler stopped owner=%s", _OWNER)


if __name__ == "__main__":
    run_forever()
